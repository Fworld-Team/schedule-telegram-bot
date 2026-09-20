import warnings
import asyncio
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from telegram import Chat
from telegram.ext import ConversationHandler
from telegram.warnings import PTBUserWarning

from schedule_bot.admin import (
    ADMIN_MENU,
    ASSESSMENT_ADD_COURSEWORK,
    ASSESSMENT_ADD_NAME,
    ASSESSMENT_ADD_TYPE,
    ASSESSMENT_DELETE_CONFIRM,
    ASSESSMENT_EDIT_MENU,
    ASSESSMENT_EDIT_NAME,
    ASSESSMENT_MENU,
    SELECT_ASSESSMENT_SEMESTER,
    _is_authorized,
    admin_menu_action,
    admin_start,
    assessment_edit_action,
    assessment_menu_action,
    build_admin_conversation,
    cancel_admin,
    confirm_assessment_delete,
    my_id,
    receive_assessment_coursework,
    receive_assessment_edit_name,
    receive_assessment_name,
    receive_assessment_type,
    select_assessment_semester,
    select_assessment_to_delete,
    select_assessment_to_edit,
    stop_admin,
)
from schedule_bot.bot import ACTIVE_CHAT_IDS_KEY
from schedule_bot.settings import Settings
from schedule_bot.storage import BotStorage


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.messages: list[str] = []
        self.reply_markups: list[object] = []

    async def reply_text(self, text, reply_markup=None, **kwargs) -> None:
        self.messages.append(text)
        self.reply_markups.append(reply_markup)


class FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers = 0

    async def answer(self) -> None:
        self.answers += 1


class AdminAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        schedules = root / "schedules"
        maps = root / "maps"
        schedules.mkdir()
        maps.mkdir()
        self.settings = Settings(
            project_root=root,
            bot_title="Test",
            schedules_directory=schedules,
            maps_directory=maps,
            token_file=root / "token.txt",
            academic_year_start_month=9,
            academic_year_start_day=1,
            utc_offset_hours=3,
            admin_user_ids=frozenset({42}),
        )
        self.storage = BotStorage(root / "data", schedules, maps)
        self.storage.initialize(date(2026, 8, 31))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def context(self):
        return SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "settings": self.settings,
                    "storage": self.storage,
                    ACTIVE_CHAT_IDS_KEY: {42},
                }
            ),
            user_data={},
        )

    @staticmethod
    def full_update(*, text: str = "", callback_data: str | None = None):
        return SimpleNamespace(
            effective_message=FakeMessage(text),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42, type=Chat.PRIVATE),
            callback_query=(
                FakeCallbackQuery(callback_data)
                if callback_data is not None
                else None
            ),
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

    def test_authorized_admin_can_open_assessment_semester_menu(self) -> None:
        context = self.context()
        start_update = self.full_update(text="/admin")

        state = asyncio.run(admin_start(start_update, context))

        self.assertEqual(state, ADMIN_MENU)
        main_buttons = [
            button.text
            for row in start_update.effective_message.reply_markups[-1].inline_keyboard
            for button in row
        ]
        self.assertIn("🎓 Экзамены и зачёты", main_buttons)

        section_update = self.full_update(callback_data="adm:assessments")
        state = asyncio.run(admin_menu_action(section_update, context))

        self.assertEqual(state, SELECT_ASSESSMENT_SEMESTER)
        semester_buttons = [
            button.text
            for row in section_update.effective_message.reply_markups[-1].inline_keyboard
            for button in row
        ]
        self.assertTrue(any("активный" in text for text in semester_buttons))

        semester_id = self.storage.active_semester().id
        select_update = self.full_update(
            callback_data=f"adm:asssem:{semester_id}"
        )
        self.assertEqual(
            asyncio.run(select_assessment_semester(select_update, context)),
            ASSESSMENT_MENU,
        )
        self.assertIn(
            "Семестр: Импортированное расписание · активный",
            select_update.effective_message.messages[0],
        )

    def test_assessment_add_wizard_accepts_every_type_and_coursework(self) -> None:
        context = self.context()
        semester_id = self.storage.active_semester().id
        type_cases = (
            ("Алгоритмизация", "credit", False),
            ("Базы данных", "differentiated_credit", False),
            ("Физическая культура", "exam", True),
        )

        for name, assessment_type, has_coursework in type_cases:
            add_update = self.full_update(
                callback_data=f"adm:ass:add:{semester_id}"
            )
            self.assertEqual(
                asyncio.run(assessment_menu_action(add_update, context)),
                ASSESSMENT_ADD_NAME,
            )

            name_update = self.full_update(text=f"  {name}  ")
            self.assertEqual(
                asyncio.run(receive_assessment_name(name_update, context)),
                ASSESSMENT_ADD_TYPE,
            )
            type_callbacks = [
                button.callback_data
                for row in name_update.effective_message.reply_markups[-1].inline_keyboard
                for button in row
            ]
            self.assertIn(f"adm:asstype:{assessment_type}", type_callbacks)

            type_update = self.full_update(
                callback_data=f"adm:asstype:{assessment_type}"
            )
            self.assertEqual(
                asyncio.run(receive_assessment_type(type_update, context)),
                ASSESSMENT_ADD_COURSEWORK,
            )

            coursework_value = "yes" if has_coursework else "no"
            coursework_update = self.full_update(
                callback_data=f"adm:asscourse:{coursework_value}"
            )
            self.assertEqual(
                asyncio.run(
                    receive_assessment_coursework(coursework_update, context)
                ),
                ASSESSMENT_MENU,
            )
            self.assertEqual(context.user_data, {})

        assessments = self.storage.list_semester_assessments(semester_id)
        self.assertEqual(
            [item.assessment_type for item in assessments],
            ["credit", "differentiated_credit", "exam"],
        )
        self.assertFalse(assessments[0].has_coursework)
        self.assertTrue(assessments[-1].has_coursework)

    def test_assessment_can_be_edited_and_temporary_data_is_cleared(self) -> None:
        context = self.context()
        semester_id = self.storage.active_semester().id
        assessment = self.storage.create_semester_assessment(
            semester_id,
            "Математика",
            "exam",
            False,
            user_id=42,
        )

        select_update = self.full_update(
            callback_data=f"adm:assedit:{semester_id}:{assessment.id}"
        )
        self.assertEqual(
            asyncio.run(select_assessment_to_edit(select_update, context)),
            ASSESSMENT_EDIT_MENU,
        )
        field_update = self.full_update(callback_data="adm:assfield:name")
        self.assertEqual(
            asyncio.run(assessment_edit_action(field_update, context)),
            ASSESSMENT_EDIT_NAME,
        )
        name_update = self.full_update(text="  Математическая   статистика ")
        self.assertEqual(
            asyncio.run(receive_assessment_edit_name(name_update, context)),
            ASSESSMENT_MENU,
        )

        updated = self.storage.get_semester_assessment(
            semester_id,
            assessment.id,
        )
        self.assertEqual(updated.subject_name, "Математическая статистика")
        self.assertEqual(context.user_data, {})

    def test_assessment_deletion_requires_confirmation(self) -> None:
        context = self.context()
        semester_id = self.storage.active_semester().id
        assessment = self.storage.create_semester_assessment(
            semester_id,
            "Математическая статистика",
            "exam",
            False,
            user_id=42,
        )
        select_update = self.full_update(
            callback_data=f"adm:assdelete:{semester_id}:{assessment.id}"
        )

        state = asyncio.run(select_assessment_to_delete(select_update, context))

        self.assertEqual(state, ASSESSMENT_DELETE_CONFIRM)
        self.assertIsNotNone(
            self.storage.get_semester_assessment(semester_id, assessment.id)
        )
        self.assertIn(
            "Удалить «Математическая статистика — Экзамен»?",
            select_update.effective_message.messages[0],
        )

        cancel_update = self.full_update(
            callback_data="adm:assdeleteconfirm:no"
        )
        self.assertEqual(
            asyncio.run(confirm_assessment_delete(cancel_update, context)),
            ASSESSMENT_MENU,
        )
        self.assertIsNotNone(
            self.storage.get_semester_assessment(semester_id, assessment.id)
        )

        select_update = self.full_update(
            callback_data=f"adm:assdelete:{semester_id}:{assessment.id}"
        )
        asyncio.run(select_assessment_to_delete(select_update, context))
        confirm_update = self.full_update(
            callback_data="adm:assdeleteconfirm:yes"
        )
        self.assertEqual(
            asyncio.run(confirm_assessment_delete(confirm_update, context)),
            ASSESSMENT_MENU,
        )
        self.assertIsNone(
            self.storage.get_semester_assessment(semester_id, assessment.id)
        )
        self.assertEqual(context.user_data, {})

    def test_cancel_clears_assessment_wizard_data(self) -> None:
        context = self.context()
        context.user_data.update(
            {
                "assessment_semester_id": 1,
                "assessment_subject_name": "Физика",
                "assessment_type": "exam",
                "assessment_has_coursework": True,
            }
        )
        update = self.full_update(text="/cancel")

        result = asyncio.run(cancel_admin(update, context))

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(context.user_data, {})

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
