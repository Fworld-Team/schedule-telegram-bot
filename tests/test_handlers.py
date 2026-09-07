from datetime import date
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from telegram.error import TimedOut
from telegram.ext import ApplicationHandlerStop

from schedule_bot.bot import (
    ACTIVE_CHAT_IDS_KEY,
    activity_gate,
    help_command,
    _send_images,
    route_text,
    set_commands,
    start,
    stop_command,
)
from schedule_bot.media import MediaCatalog
from schedule_bot.settings import Settings


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.photos: list[tuple[str, str | None]] = []
        self.documents: list[tuple[str, str | None]] = []
        self.messages: list[str] = []
        self.reply_markups: list[object] = []

    async def reply_photo(
        self,
        photo,
        caption=None,
        reply_markup=None,
        **kwargs,
    ) -> None:
        self.photos.append((Path(photo.name).name, caption))
        self.reply_markups.append(reply_markup)

    async def reply_document(
        self,
        document,
        caption=None,
        reply_markup=None,
        **kwargs,
    ) -> None:
        self.documents.append((Path(document.name).name, caption))
        self.reply_markups.append(reply_markup)

    async def reply_text(self, text, reply_markup=None) -> None:
        self.messages.append(text)
        self.reply_markups.append(reply_markup)


class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        schedules = root / "schedules"
        maps = root / "maps"
        schedules.mkdir()
        maps.mkdir()
        self.settings = Settings(
            project_root=root,
            bot_title="Test bot",
            schedules_directory=schedules,
            maps_directory=maps,
            token_file=root / "token.txt",
            academic_year_start_month=9,
            academic_year_start_day=1,
            utc_offset_hours=3,
        )
        self.catalog = MediaCatalog(schedules, maps)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def context(self):
        return SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "settings": self.settings,
                    "catalog": self.catalog,
                    ACTIVE_CHAT_IDS_KEY: set(),
                }
            ),
            user_data={},
        )

    def run_button(self, text: str, today: date) -> FakeMessage:
        message = FakeMessage(text)
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=100),
        )
        with patch("schedule_bot.bot._today", return_value=today):
            asyncio.run(route_text(update, self.context()))
        return message

    def test_day_button_sends_local_file(self) -> None:
        Image.new("RGB", (4, 4), "white").save(
            self.settings.schedules_directory / "monday.png",
            format="PNG",
        )
        message = self.run_button("Пн", date(2026, 9, 3))
        self.assertEqual(message.photos[0][0], "monday.png")
        self.assertIn("Понедельник, 31.08.2026", message.photos[0][1])
        self.assertIn("верхняя", message.photos[0][1])

    def test_same_day_photo_is_captioned_with_lower_week(self) -> None:
        Image.new("RGB", (4, 4), "white").save(
            self.settings.schedules_directory / "monday.png",
            format="PNG",
        )
        message = self.run_button("Пн", date(2026, 9, 10))
        self.assertEqual(message.photos[0][0], "monday.png")
        self.assertIn("нижняя", message.photos[0][1])

    def test_today_on_weekend_does_not_send_photo(self) -> None:
        message = self.run_button("📅 Сегодня", date(2026, 9, 5))
        self.assertEqual(message.photos, [])
        self.assertIn("занятия на этот день не настроены", message.messages[0])

    def test_map_button_sends_all_maps_in_natural_order(self) -> None:
        Image.new("RGB", (4, 4), "white").save(
            self.settings.maps_directory / "map_2.jpg",
            format="JPEG",
        )
        Image.new("RGB", (4, 4), "white").save(
            self.settings.maps_directory / "map_1.jpg",
            format="JPEG",
        )
        message = self.run_button("🗺️ Карта", date(2026, 9, 3))
        self.assertEqual(
            [name for name, _ in message.photos],
            ["map_1.jpg", "map_2.jpg"],
        )
        self.assertEqual(message.photos[0][1], "Карта кампуса")
        self.assertIsNone(message.photos[1][1])

    def test_connect_timeout_is_retried_with_reopened_file(self) -> None:
        class ConnectTimeout(Exception):
            pass

        class FlakyMessage(FakeMessage):
            def __init__(self) -> None:
                super().__init__("Вт")
                self.attempts = 0

            async def reply_photo(self, photo, **kwargs) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    try:
                        raise ConnectTimeout("simulated connect timeout")
                    except ConnectTimeout as exc:
                        raise TimedOut() from exc
                await super().reply_photo(photo, **kwargs)

        path = self.settings.schedules_directory / "tuesday.jpg"
        Image.new("RGB", (4, 4), "white").save(path, format="JPEG")
        message = FlakyMessage()

        with patch("schedule_bot.bot.asyncio.sleep", new=AsyncMock()) as sleep:
            asyncio.run(_send_images(message, [path], "Вторник"))

        self.assertEqual(message.attempts, 2)
        self.assertEqual(message.photos, [("tuesday.jpg", "Вторник")])
        sleep.assert_awaited_once_with(1)

    def test_help_does_not_reveal_admin_panel_or_media_paths(self) -> None:
        message = FakeMessage("/help")
        update = SimpleNamespace(effective_message=message)
        asyncio.run(help_command(update, self.context()))

        help_text = message.messages[0]
        self.assertIn("«Карта» — показывает карту;", help_text)
        self.assertIn("«Выход» или /stop", help_text)
        self.assertIn("Верхняя неделя — нечётная", help_text)
        self.assertIn("Неделя с 1 сентября считается верхней", help_text)
        self.assertIn("С 8 сентября начинается нижняя", help_text)
        self.assertNotIn("media/maps", help_text)
        self.assertNotIn("/admin", help_text)
        self.assertNotIn("администратор", help_text.casefold())

    def test_missing_media_messages_do_not_reveal_admin_panel(self) -> None:
        schedule_message = self.run_button("Пн", date(2026, 9, 3))
        map_message = self.run_button("🗺️ Карта", date(2026, 9, 3))

        public_text = "\n".join(schedule_message.messages + map_message.messages)
        self.assertNotIn("/admin", public_text)
        self.assertNotIn("администратор", public_text.casefold())

    def test_admin_command_is_only_in_admin_chat_menu(self) -> None:
        class FakeBot:
            def __init__(self) -> None:
                self.calls: list[tuple[list, dict]] = []

            async def set_my_commands(self, commands, **kwargs) -> None:
                self.calls.append((commands, kwargs))

        settings = Settings(
            project_root=self.settings.project_root,
            bot_title=self.settings.bot_title,
            schedules_directory=self.settings.schedules_directory,
            maps_directory=self.settings.maps_directory,
            token_file=self.settings.token_file,
            academic_year_start_month=9,
            academic_year_start_day=1,
            utc_offset_hours=3,
            admin_user_ids=frozenset({1125654781}),
        )
        bot = FakeBot()
        application = SimpleNamespace(
            bot=bot,
            bot_data={"settings": settings},
        )

        asyncio.run(set_commands(application))

        public_names = [command.command for command in bot.calls[0][0]]
        admin_names = [command.command for command in bot.calls[1][0]]
        self.assertIn("stop", public_names)
        self.assertNotIn("admin", public_names)
        self.assertNotIn("myid", public_names)
        self.assertIn("admin", admin_names)
        self.assertEqual(bot.calls[1][1]["scope"].chat_id, 1125654781)

    def test_exit_hides_keyboard(self) -> None:
        message = self.run_button("🚪 Выход", date(2026, 9, 3))
        self.assertIn("/start", message.messages[0])
        self.assertEqual(message.reply_markups[0].__class__.__name__, "ReplyKeyboardRemove")

    def test_only_exact_keyboard_labels_are_accepted(self) -> None:
        for text in (
            "понедельник",
            "сегодня",
            "карта кампуса",
            "help",
            "хватит",
            "завершить",
            "обычное сообщение",
        ):
            message = self.run_button(text, date(2026, 9, 3))
            self.assertEqual(message.messages, [], text)
            self.assertEqual(message.photos, [], text)

    def test_inactive_chat_is_silent_until_start(self) -> None:
        context = self.context()

        def update(text: str, chat_id: int = 100):
            return SimpleNamespace(
                effective_message=FakeMessage(text),
                effective_chat=SimpleNamespace(id=chat_id),
                callback_query=None,
            )

        for text in ("обычное сообщение", "/today", "/help", "/admin"):
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(activity_gate(update(text), context))

        start_update = update("/start")
        asyncio.run(activity_gate(start_update, context))
        asyncio.run(start(start_update, context))
        self.assertEqual(len(start_update.effective_message.messages), 1)

        plain_update = update("обычное сообщение")
        asyncio.run(activity_gate(plain_update, context))
        asyncio.run(route_text(plain_update, context))
        self.assertEqual(plain_update.effective_message.messages, [])
        context.user_data.clear()
        asyncio.run(activity_gate(update("Пн"), context))
        with self.assertRaises(ApplicationHandlerStop):
            asyncio.run(activity_gate(update("обычное сообщение", 200), context))

        stop_update = update("/stop")
        asyncio.run(stop_command(stop_update, context))
        with self.assertRaises(ApplicationHandlerStop):
            asyncio.run(activity_gate(update("ещё сообщение"), context))

    def test_group_activity_is_shared_by_all_members(self) -> None:
        owner_context = self.context()
        member_context = SimpleNamespace(
            application=owner_context.application,
            user_data={},
        )

        def group_update(text: str, user_id: int):
            return SimpleNamespace(
                effective_message=FakeMessage(text),
                effective_chat=SimpleNamespace(id=-1001234567890),
                effective_user=SimpleNamespace(id=user_id),
                callback_query=None,
            )

        asyncio.run(start(group_update("/start", 10), owner_context))
        asyncio.run(
            activity_gate(group_update("Пн", 20), member_context)
        )
        asyncio.run(stop_command(group_update("/stop", 20), member_context))
        with self.assertRaises(ApplicationHandlerStop):
            asyncio.run(
                activity_gate(group_update("Пн", 10), owner_context)
            )

    def test_private_and_group_states_are_independent(self) -> None:
        context = self.context()

        def update(text: str, chat_id: int):
            return SimpleNamespace(
                effective_message=FakeMessage(text),
                effective_chat=SimpleNamespace(id=chat_id),
                callback_query=None,
            )

        asyncio.run(start(update("/start", 1125654781), context))
        with self.assertRaises(ApplicationHandlerStop):
            asyncio.run(activity_gate(update("Пн", -10099), context))

        asyncio.run(start(update("/start", -10099), context))
        asyncio.run(stop_command(update("/stop", 1125654781), context))
        with self.assertRaises(ApplicationHandlerStop):
            asyncio.run(activity_gate(update("Пн", 1125654781), context))
        asyncio.run(activity_gate(update("Пн", -10099), context))

    def test_start_with_bot_name_wakes_inactive_user(self) -> None:
        context = self.context()
        update = SimpleNamespace(
            effective_message=FakeMessage("/start@ScheduleBot payload"),
            effective_chat=SimpleNamespace(id=-100),
            callback_query=None,
        )

        asyncio.run(activity_gate(update, context))
        asyncio.run(start(update, context))
        asyncio.run(
            activity_gate(
                SimpleNamespace(
                    effective_message=FakeMessage("Пн"),
                    effective_chat=SimpleNamespace(id=-100),
                    callback_query=None,
                ),
                context,
            )
        )


if __name__ == "__main__":
    unittest.main()
