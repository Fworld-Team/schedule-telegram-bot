import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from schedule_bot.settings import ConfigurationError, Settings, load_bot_token


VALID_CONFIG = {
    "bot_title": "Test bot",
    "schedules_directory": "media/schedules",
    "maps_directory": "media/maps",
    "token_file": "token.txt",
    "academic_year_start": "09-01",
    "utc_offset_hours": 3,
}


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "config.json").write_text(
            json.dumps(VALID_CONFIG), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_loads_and_resolves_relative_paths(self) -> None:
        settings = Settings.load(self.root)
        self.assertEqual(settings.academic_year_start_month, 9)
        self.assertEqual(settings.schedules_directory, self.root / "media/schedules")

    def test_environment_token_has_priority(self) -> None:
        settings = Settings.load(self.root)
        valid = "123456:" + "A" * 30
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": valid}, clear=False):
            self.assertEqual(load_bot_token(settings), valid)

    def test_token_file_is_supported(self) -> None:
        settings = Settings.load(self.root)
        valid = "123456:" + "B" * 30
        settings.token_file.write_text(valid, encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_bot_token(settings), valid)

    def test_token_file_can_come_from_server_secret(self) -> None:
        settings = Settings.load(self.root)
        secret = self.root / "server-secret"
        valid = "123456:" + "C" * 30
        secret.write_text(valid, encoding="utf-8")
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN_FILE": str(secret)},
            clear=True,
        ):
            self.assertEqual(load_bot_token(settings), valid)

    def test_admin_ids_can_come_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"ADMIN_USER_IDS": "123, 456,123"},
            clear=True,
        ):
            settings = Settings.load(self.root)
        self.assertEqual(settings.admin_user_ids, frozenset({123, 456}))

    def test_placeholder_token_is_rejected(self) -> None:
        settings = Settings.load(self.root)
        settings.token_file.write_text(
            "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE", encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                load_bot_token(settings)

    def test_invalid_start_date_is_rejected(self) -> None:
        config = dict(VALID_CONFIG, academic_year_start="13-40")
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            Settings.load(self.root)

    def test_leap_day_cannot_be_a_recurring_anchor(self) -> None:
        config = dict(VALID_CONFIG, academic_year_start="02-29")
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            Settings.load(self.root)


if __name__ == "__main__":
    unittest.main()
