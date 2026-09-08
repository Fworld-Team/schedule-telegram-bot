import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from schedule_bot.__main__ import main
from schedule_bot.settings import Settings


class HealthcheckTests(unittest.TestCase):
    def test_production_healthcheck_checks_telegram(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "bot.sqlite3"
            database.touch()
            settings = Settings(
                project_root=root,
                bot_title="test",
                schedules_directory=root / "schedules",
                maps_directory=root / "maps",
                token_file=root / "token.txt",
                academic_year_start_month=9,
                academic_year_start_day=1,
                utc_offset_hours=3,
                data_directory=root,
                proxy_url="http://172.30.80.1:1081",
            )
            storage = Mock()
            storage.database_path = database
            storage.quick_check.return_value = True
            telegram_check = AsyncMock()
            with patch("schedule_bot.__main__.Settings.load", return_value=settings), \
                 patch("schedule_bot.__main__.BotStorage", return_value=storage), \
                 patch("schedule_bot.__main__._check_telegram", telegram_check), \
                 patch("sys.argv", ["schedule_bot", "--healthcheck"]), \
                 patch.dict(os.environ, {"HEALTHCHECK_TELEGRAM": "1"}, clear=False):
                self.assertEqual(main(), 0)
            telegram_check.assert_awaited_once_with(settings)
