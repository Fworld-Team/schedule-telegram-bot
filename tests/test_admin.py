import warnings
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from telegram import Chat
from telegram.ext import ConversationHandler
from telegram.warnings import PTBUserWarning

from schedule_bot.admin import (
    _is_authorized,
    admin_start,
    build_admin_conversation,
    my_id,
    stop_admin,
)
from schedule_bot.bot import ACTIVE_CHAT_IDS_KEY
from schedule_bot.settings import Settings


class AdminAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path("C:/test")
        self.settings = Settings(
            project_root=root,
            bot_title="Test",
            schedules_directory=root / "schedules",
            maps_directory=root / "maps",
            token_file=root / "token.txt",
            academic_year_start_month=9,
            academic_year_start_day=1,
            utc_offset_hours=3,
            admin_user_ids=frozenset({42}),
        )

    @staticmethod
    def update(user_id: int, chat_type: str):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(type=chat_type),
        )

    def test_allowed_numeric_id_in_private_chat(self) -> None:
        self.assertTrue(
            _is_authorized(self.update(42, Chat.PRIVATE), self.settings)
        )

    def test_other_user_is_rejected(self) -> None:
        self.assertFalse(
            _is_authorized(self.update(99, Chat.PRIVATE), self.settings)
        )

    def test_admin_is_rejected_in_group_chat(self) -> None:
        self.assertFalse(
            _is_authorized(self.update(42, Chat.GROUP), self.settings)
        )

    def test_unauthorized_admin_command_is_silent(self) -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def reply_text(self, text, **kwargs) -> None:
                self.messages.append(text)

        message = FakeMessage()
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=99),
            effective_chat=SimpleNamespace(type=Chat.PRIVATE),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": self.settings})
        )

        result = asyncio.run(admin_start(update, context))

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(message.messages, [])

    def test_my_id_does_not_mention_admin_panel(self) -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def reply_text(self, text, **kwargs) -> None:
                self.messages.append(text)

        message = FakeMessage()
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=99),
            effective_chat=SimpleNamespace(id=99, type=Chat.PRIVATE),
        )

        asyncio.run(my_id(update, SimpleNamespace()))

        response = message.messages[0]
        self.assertIn("Ваш Telegram ID: 99", response)
        self.assertNotIn("ADMIN_USER_IDS", response)
        self.assertNotIn("админ", response.casefold())

    def test_stop_ends_admin_wizard_without_losing_other_chat_states(self) -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.messages: list[str] = []
                self.reply_markups: list[object] = []

            async def reply_text(self, text, reply_markup=None) -> None:
                self.messages.append(text)
                self.reply_markups.append(reply_markup)

        message = FakeMessage()
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=42),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    ACTIVE_CHAT_IDS_KEY: {42, -10099},
                }
            ),
            user_data={"new_semester_name": "Черновик"},
        )

        result = asyncio.run(stop_admin(update, context))

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(context.user_data, {})
        self.assertEqual(
            context.application.bot_data[ACTIVE_CHAT_IDS_KEY],
            {-10099},
        )
        self.assertIn("/start", message.messages[0])

    def test_every_admin_state_handles_exit_and_stop(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", PTBUserWarning)
            conversation = build_admin_conversation()
        for handlers in conversation.states.values():
            self.assertTrue(
                any(handler.callback is stop_admin for handler in handlers)
            )
        self.assertTrue(
            any(handler.callback is stop_admin for handler in conversation.fallbacks)
        )


if __name__ == "__main__":
    unittest.main()
