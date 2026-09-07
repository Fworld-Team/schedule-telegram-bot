from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from schedule_bot.calendar import WeekKind
from schedule_bot.media import MediaCatalog


def _write_fake_image(path: Path) -> None:
    suffix = path.suffix.casefold()
    if suffix not in {".jpg", ".jpeg", ".webp", ".png"}:
        path.write_bytes(b"ignored")
        return
    format_name = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
        ".png": "PNG",
    }[suffix]
    Image.new("RGB", (4, 4), "white").save(path, format=format_name)


class MediaCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.schedules = root / "schedules"
        self.maps = root / "maps"
        self.schedules.mkdir()
        self.maps.mkdir()
        self.catalog = MediaCatalog(self.schedules, self.maps)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def touch_schedule(self, name: str) -> None:
        _write_fake_image(self.schedules / name)

    def test_generic_file_is_fallback(self) -> None:
        self.touch_schedule("monday.png")
        self.assertEqual(
            [path.name for path in self.catalog.schedules_for(0, WeekKind.UPPER)],
            ["monday.png"],
        )

    def test_specific_week_file_wins_over_generic(self) -> None:
        self.touch_schedule("monday.png")
        self.touch_schedule("monday_upper.png")
        self.touch_schedule("monday_lower.jpg")
        self.assertEqual(
            [path.name for path in self.catalog.schedules_for(0, WeekKind.UPPER)],
            ["monday_upper.png"],
        )
        self.assertEqual(
            [path.name for path in self.catalog.schedules_for(0, WeekKind.LOWER)],
            ["monday_lower.jpg"],
        )

    def test_russian_names_and_multiple_pages_are_supported(self) -> None:
        self.touch_schedule("среда_верхняя_2.jpg")
        self.touch_schedule("среда_верхняя_1.jpg")
        self.assertEqual(
            [path.name for path in self.catalog.schedules_for(2, WeekKind.UPPER)],
            ["среда_верхняя_1.jpg", "среда_верхняя_2.jpg"],
        )

    def test_unsupported_and_hidden_files_are_ignored(self) -> None:
        self.touch_schedule("tuesday.txt")
        self.touch_schedule(".tuesday.png")
        self.assertEqual(self.catalog.schedules_for(1, WeekKind.UPPER), [])

    def test_maps_are_naturally_sorted(self) -> None:
        for name in ("map_10.jpg", "map_2.png", "notes.txt", ".hidden.png"):
            _write_fake_image(self.maps / name)
        self.assertEqual(
            [path.name for path in self.catalog.maps()],
            ["map_2.png", "map_10.jpg"],
        )

    def test_sorting_handles_names_that_start_with_a_number(self) -> None:
        for name in ("map.png", "2-map.png"):
            _write_fake_image(self.maps / name)
        self.assertEqual(
            [path.name for path in self.catalog.maps()],
            ["2-map.png", "map.png"],
        )

    def test_empty_or_invalid_image_is_ignored(self) -> None:
        (self.schedules / "monday.png").touch()
        (self.schedules / "monday_2.jpg").write_bytes(b"not an image")
        self.assertEqual(self.catalog.schedules_for(0, WeekKind.UPPER), [])

    def test_truncated_file_with_valid_magic_is_ignored(self) -> None:
        (self.schedules / "monday.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"not-a-real-image"
        )
        self.assertEqual(self.catalog.schedules_for(0, WeekKind.UPPER), [])


if __name__ == "__main__":
    unittest.main()
