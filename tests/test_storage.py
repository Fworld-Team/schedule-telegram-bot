from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from schedule_bot.calendar import WeekKind
from schedule_bot.storage import BotStorage, StorageError


def _encoded_image(format_name: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format=format_name)
    return buffer.getvalue()


PNG = _encoded_image("PNG")
JPEG = _encoded_image("JPEG")


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.seed_schedules = root / "seed-schedules"
        self.seed_maps = root / "seed-maps"
        self.seed_schedules.mkdir()
        self.seed_maps.mkdir()
        (self.seed_schedules / "monday.png").write_bytes(PNG)
        (self.seed_maps / "campus.jpg").write_bytes(JPEG)
        self.storage = BotStorage(
            root / "data",
            self.seed_schedules,
            self.seed_maps,
        )
        self.storage.initialize(date(2026, 8, 31))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_run_imports_media_and_is_idempotent(self) -> None:
        active = self.storage.active_semester()
        self.assertEqual(active.status, "active")
        self.assertEqual(
            [path.name for path in self.storage.active_catalog().schedules_for(
                0, WeekKind.UPPER
            )],
            ["monday.png"],
        )
        self.storage.initialize(date(2026, 9, 7))
        self.assertEqual(len(self.storage.list_semesters()), 1)
        self.assertTrue(self.storage.quick_check())

    def test_chat_activity_is_persistent_and_independent(self) -> None:
        self.assertEqual(self.storage.active_chat_ids(), set())

        self.storage.set_chat_active(1125654781, True)
        self.storage.set_chat_active(-1001234567890, True)
        self.assertEqual(
            self.storage.active_chat_ids(),
            {1125654781, -1001234567890},
        )

        self.storage.set_chat_active(1125654781, False)
        reopened = BotStorage(
            self.storage.data_directory,
            self.seed_schedules,
            self.seed_maps,
        )
        reopened.initialize(date(2026, 9, 7))
        self.assertEqual(reopened.active_chat_ids(), {-1001234567890})
        self.assertEqual(len(reopened.list_semesters()), 1)

    def test_failed_first_seed_does_not_commit_initialized_semester(self) -> None:
        root = Path(self.temp.name)
        second = BotStorage(
            root / "failed-data",
            self.seed_schedules,
            self.seed_maps,
        )
        with patch(
            "schedule_bot.storage._copy_supported_images",
            side_effect=OSError("simulated copy failure"),
        ):
            with self.assertRaises(OSError):
                second.initialize(date(2026, 8, 31))
        second.initialize(date(2026, 8, 31))
        self.assertEqual(len(second.list_semesters()), 1)
        self.assertEqual(
            [path.name for path in second.active_catalog().schedules_for(
                0, WeekKind.UPPER
            )],
            ["monday.png"],
        )

    def test_draft_does_not_replace_active_until_activation(self) -> None:
        original = self.storage.active_semester()
        draft = self.storage.create_semester(
            "Весна 2027",
            date(2027, 2, 8),
            WeekKind.LOWER,
            user_id=42,
        )
        self.assertEqual(draft.status, "draft")
        self.assertEqual(self.storage.active_semester().id, original.id)
        active = self.storage.activate_semester(draft.id, user_id=42)
        self.assertEqual(active.id, draft.id)
        self.assertEqual(self.storage.get_semester(original.id).status, "archived")

    def test_manual_correction_becomes_new_alternating_anchor(self) -> None:
        semester = self.storage.create_semester(
            "Осень",
            date(2026, 8, 31),
            WeekKind.UPPER,
            user_id=42,
        )
        self.storage.activate_semester(semester.id, user_id=42)
        target = date(2027, 1, 8)
        before = self.storage.week_kind(target)
        self.storage.set_current_week_kind(target, WeekKind.UPPER, user_id=42)
        self.assertEqual(self.storage.week_kind(target), WeekKind.UPPER)
        self.assertEqual(
            self.storage.week_kind(date(2027, 1, 15)),
            WeekKind.LOWER,
        )
        self.assertTrue(self.storage.clear_current_week_override(target, user_id=42))
        self.assertEqual(self.storage.week_kind(target), before)

    def test_upload_uses_fixed_name_and_archives_previous_file(self) -> None:
        semester = self.storage.create_semester(
            "Новый",
            date(2027, 2, 8),
            WeekKind.UPPER,
            user_id=42,
        )
        first = self.storage.new_upload_path()
        first.write_bytes(PNG)
        target = self.storage.install_schedule_image(
            first, semester.id, 2, "lower", user_id=42
        )
        self.assertEqual(target.name, "wednesday_lower.png")

        second = self.storage.new_upload_path()
        second.write_bytes(JPEG)
        target = self.storage.install_schedule_image(
            second, semester.id, 2, "lower", user_id=42
        )
        self.assertEqual(target.name, "wednesday_lower.jpg")
        self.assertFalse((target.parent / "wednesday_lower.png").exists())
        archived = list(
            (self.storage.archive_directory / f"semester-{semester.id}").rglob(
                "wednesday_lower.png"
            )
        )
        self.assertEqual(len(archived), 1)

    def test_upload_removes_old_numbered_pages_for_the_same_slot(self) -> None:
        active = self.storage.active_semester()
        directory = self.storage.schedule_directory(active.id)
        (directory / "пн_нижняя_1.png").write_bytes(PNG)
        (directory / "monday_lower_2.jpg").write_bytes(JPEG)
        (directory / "monday_upper.png").write_bytes(PNG)
        uploaded = self.storage.new_upload_path()
        uploaded.write_bytes(JPEG)
        self.storage.install_schedule_image(
            uploaded, active.id, 0, "lower", user_id=42
        )
        names = sorted(path.name for path in directory.iterdir())
        self.assertEqual(names, ["monday.png", "monday_lower.jpg", "monday_upper.png"])

    def test_common_upload_supersedes_old_upper_and_lower_files(self) -> None:
        active = self.storage.active_semester()
        directory = self.storage.schedule_directory(active.id)
        (directory / "monday_upper.png").write_bytes(PNG)
        (directory / "monday_lower.jpg").write_bytes(JPEG)
        uploaded = self.storage.new_upload_path()
        uploaded.write_bytes(PNG)
        self.storage.install_schedule_image(
            uploaded, active.id, 0, "common", user_id=42
        )
        names = sorted(path.name for path in directory.iterdir())
        self.assertEqual(names, ["monday.png"])
        catalog = self.storage.active_catalog()
        self.assertEqual(
            [path.name for path in catalog.schedules_for(0, WeekKind.UPPER)],
            ["monday.png"],
        )
        self.assertEqual(
            [path.name for path in catalog.schedules_for(0, WeekKind.LOWER)],
            ["monday.png"],
        )

    def test_invalid_upload_does_not_replace_existing_file(self) -> None:
        active = self.storage.active_semester()
        bad = self.storage.new_upload_path()
        bad.write_bytes(b"not-an-image")
        with self.assertRaises(StorageError):
            self.storage.install_schedule_image(
                bad, active.id, 0, "common", user_id=42
            )
        self.assertTrue(
            (self.storage.schedule_directory(active.id) / "monday.png").exists()
        )

    def test_map_upload_replaces_all_legacy_map_images(self) -> None:
        (self.storage.maps_directory / "map_2.png").write_bytes(PNG)
        uploaded = self.storage.new_upload_path()
        uploaded.write_bytes(PNG)
        target = self.storage.install_map_image(uploaded, user_id=42)
        self.assertEqual(target.name, "campus.png")
        self.assertEqual(
            [path.name for path in self.storage.active_catalog().maps()],
            ["campus.png"],
        )


if __name__ == "__main__":
    unittest.main()
