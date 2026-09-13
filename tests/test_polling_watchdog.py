import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import NetworkError

from schedule_bot.bot import polling_watchdog


class PollingWatchdogTests(unittest.TestCase):
    def test_stops_application_after_persistent_backlog(self):
        application = SimpleNamespace(
            bot=SimpleNamespace(
                get_webhook_info=AsyncMock(
                    return_value=SimpleNamespace(pending_update_count=2)
                )
            ),
            stop_running=Mock(),
        )

        asyncio.run(polling_watchdog(application, interval=0, stall_limit=3))

        self.assertEqual(application.bot.get_webhook_info.await_count, 3)
        application.stop_running.assert_called_once_with()

    def test_transient_api_error_does_not_count_as_stalled_polling(self):
        application = SimpleNamespace(
            bot=SimpleNamespace(
                get_webhook_info=AsyncMock(
                    side_effect=[
                        NetworkError("temporary"),
                        SimpleNamespace(pending_update_count=1),
                        SimpleNamespace(pending_update_count=1),
                    ]
                )
            ),
            stop_running=Mock(),
        )

        with patch("schedule_bot.bot.asyncio.sleep", new=AsyncMock()):
            asyncio.run(polling_watchdog(application, interval=0, stall_limit=2))

        self.assertEqual(application.bot.get_webhook_info.await_count, 3)
        application.stop_running.assert_called_once_with()
