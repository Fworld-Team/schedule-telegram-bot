from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zlib

import httpx
from telegram.error import BadRequest, EndPointNotFound, TimedOut

from schedule_bot.calendar import WeekKind
from schedule_bot.admin import clear_week_override, set_week_kind
from schedule_bot.schedule_monitor import ScheduleMonitor
from schedule_bot.schedule_widgets import ScheduleWidgetCards
from schedule_bot.storage import BotStorage
from tests.test_schedule_monitor import PDF_URL, sample_groups
from tests.test_widget_html import sample


class ScheduleWidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.storage = BotStorage(self.root / "data", self.root / "seed", self.root / "maps")
        self.storage.initialize(date(2026, 11, 9))
        self.settings = SimpleNamespace(runtime_data_directory=self.storage.data_directory,
                                        utc_offset_hours=3)
        self.bot = AsyncMock()
        self.cards = ScheduleWidgetCards(self.bot, self.storage, self.settings)
        self.weekly = sample()
        self.today = date(2026, 11, 9)

    async def publish_base(self):
        self.bot.do_api_request.return_value = {"message_id": 50}
        await self.cards.send(12, self.weekly, self.cards.anchor(self.today))
        self.bot.do_api_request.reset_mock()

    async def test_no_data_never_attempts_telegram(self):
        self.assertFalse(await self.cards.open(12))
        self.bot.do_api_request.assert_not_awaited()

    async def test_definitive_edit_failures_create_one_replacement(self):
        for error in (BadRequest("message to edit not found"), EndPointNotFound("edit unavailable")):
            with self.subTest(error=error):
                self.cards = ScheduleWidgetCards(self.bot, self.storage, self.settings)
                self.bot.do_api_request.side_effect = None
                await self.publish_base()
                changed = deepcopy(self.weekly)
                changed["ДЭ 23-24"]["upper"][0].clear()
                self.bot.do_api_request.side_effect = [error, {"message_id": 51}]
                await self.cards.refresh(changed, self.today, lambda: {12})
                await self.cards.refresh(changed, self.today, lambda: {12})
                self.assertEqual([call.args[0] for call in self.bot.do_api_request.await_args_list],
                                 ["edit_message_widget_data", "send_widget"])
                self.assertEqual(self.storage.schedule_widget_card(12)["message_id"], 51)

    async def test_large_patch_skips_edit_and_sends_a_full_card(self):
        await self.publish_base()
        changed = deepcopy(self.weekly)
        for variants in changed.values():
            for days in variants.values():
                for day in range(6):
                    days[day] = [{**days[0][0], "subject": "Изменение " * 15}]
        self.bot.do_api_request.return_value = {"message_id": 52}
        await self.cards.refresh(changed, self.today, lambda: {12})
        self.assertEqual(self.bot.do_api_request.await_count, 1)
        self.assertEqual(self.bot.do_api_request.await_args.args, ("send_widget",))
        self.assertTrue(self.bot.do_api_request.await_args.kwargs["api_kwargs"]["disable_notification"])

    async def test_ambiguous_error_is_not_retried_after_restart(self):
        await self.publish_base()
        changed = deepcopy(self.weekly)
        changed["ДЭ 23-24"]["upper"][0].clear()
        self.bot.do_api_request.side_effect = TimedOut()
        await self.cards.refresh(changed, self.today, lambda: {12})
        restarted = ScheduleWidgetCards(self.bot, self.storage, self.settings)
        await restarted.refresh(changed, self.today, lambda: {12})
        self.assertEqual(self.bot.do_api_request.await_count, 1)
        self.assertEqual(self.storage.schedule_widget_card(12)["message_id"], 50)

    async def test_unchanged_or_stopped_chat_gets_no_card_update(self):
        await self.publish_base()
        await self.cards.refresh(self.weekly, self.today, lambda: {12})
        changed = deepcopy(self.weekly)
        changed["ДЭ 23-24"]["upper"][0].clear()
        await self.cards.refresh(changed, self.today, set)
        self.bot.do_api_request.assert_not_awaited()

    async def test_admin_week_correction_and_removal_refresh_cached_card(self):
        app = SimpleNamespace(bot=self.bot, bot_data={
            "settings": self.settings, "storage": self.storage,
            "schedule_widget_cards": self.cards, "active": {12},
        })
        self.cards.store.save({
            "schema": 1, "last_successful_check": "2026-11-08",
            "pdf_url": PDF_URL, "groups": sample_groups(), "widget_weeks": self.weekly,
        })
        await self.publish_base()
        original_kind = self.storage.week_kind(self.today)
        corrected_kind = WeekKind.LOWER if original_kind is WeekKind.UPPER else WeekKind.UPPER
        update = SimpleNamespace(
            callback_query=SimpleNamespace(data=f"week:{corrected_kind.value}"),
            effective_user=SimpleNamespace(id=42), effective_message=AsyncMock(),
        )
        context = SimpleNamespace(application=app)
        self.bot.do_api_request.return_value = True
        now = datetime(2026, 11, 9, 5, tzinfo=timezone.utc)  # 08:00 local, before the PDF run.
        monitor = ScheduleMonitor(app, "active", client=None)
        with patch("schedule_bot.schedule_monitor.fetch_snapshot", new_callable=AsyncMock) as fetch, \
             patch("schedule_bot.admin._guard_callback", new=AsyncMock(return_value=True)), \
             patch("schedule_bot.admin._today", return_value=self.today), \
             patch("schedule_bot.admin._send_admin_menu", new=AsyncMock()):
            self.assertEqual(await monitor.check(now), "not-due")
            self.bot.do_api_request.assert_not_awaited()
            await set_week_kind(update, context)
            await monitor.check(now)
            await monitor.check(now)
            await clear_week_override(update, context)
            await monitor.check(now)
            await monitor.check(now)
            fetch.assert_not_awaited()
        calls = self.bot.do_api_request.await_args_list
        self.assertEqual([call.args[0] for call in calls], ["edit_message_widget_data"] * 2)
        self.assertEqual([call.kwargs["api_kwargs"]["message_id"] for call in calls], [50, 50])
        states = [json.loads(call.kwargs["api_kwargs"]["public_data"]) for call in calls]
        self.assertEqual([state["kind"] for state in states], [corrected_kind.value, original_kind.value])
        self.assertEqual([state["patches"] for state in states], [[], []])

    async def test_backfills_legacy_cache_before_due_without_advancing_daily_check(self):
        fixture = Path(__file__).parent / "fixtures/timacad"
        pdf = (fixture / "schedule.pdf").read_bytes()
        requests = []

        def university(request):
            requests.append(request.url.path)
            if request.url.path.endswith(".pdf"):
                return httpx.Response(200, content=pdf)
            return httpx.Response(200, content=(fixture / "source.html").read_bytes())

        app = SimpleNamespace(bot=self.bot, bot_data={
            "settings": self.settings, "storage": self.storage,
            "schedule_widget_cards": self.cards, "active": {12},
        })
        async with httpx.AsyncClient(transport=httpx.MockTransport(university)) as client:
            monitor = ScheduleMonitor(app, "active", client)
            yesterday = datetime(2026, 11, 8, 8, tzinfo=timezone.utc)
            self.assertEqual(await monitor.check(yesterday), "baseline")
            legacy = monitor.store.load()
            del legacy["widget_weeks"]
            monitor.store.save(legacy)
            requests.clear()

            early = datetime(2026, 11, 9, 5, tzinfo=timezone.utc)
            self.assertEqual(await monitor.check(early), "not-due")
            self.assertEqual(len(requests), 1)
            self.assertTrue(requests[0].endswith(".pdf"))
            filled = monitor.store.load()
            self.assertTrue(filled.pop("widget_weeks"))
            self.assertEqual(filled, legacy)
            restarted = ScheduleMonitor(app, "active", client)
            await restarted.check(early)
            self.assertEqual(len(requests), 1)
            self.bot.do_api_request.assert_not_awaited()
            self.bot.send_message.assert_not_awaited()
            self.bot.do_api_request.return_value = {"message_id": 54}
            self.assertTrue(await self.cards.open(12))

            due = datetime(2026, 11, 9, 6, tzinfo=timezone.utc)
            self.assertEqual(await restarted.check(due), "unchanged")
            self.assertEqual(len(requests), 3)
            self.assertEqual(monitor.store.load()["last_successful_check"], "2026-11-09")
            self.bot.do_api_request.assert_awaited_once()
            self.bot.send_message.assert_not_awaited()

    async def test_backfill_never_adopts_a_changed_pdf_or_retries_on_every_tick(self):
        legacy = {
            "schema": 1, "last_successful_check": self.today.isoformat(),
            "pdf_url": PDF_URL, "groups": sample_groups(),
            "pdf_hash": hashlib.sha256(b"%PDF-accepted").hexdigest(),
        }
        self.cards.store.save(legacy)
        app = SimpleNamespace(bot=self.bot, bot_data={
            "settings": self.settings, "schedule_widget_cards": self.cards, "active": {12},
        })
        monitor = ScheduleMonitor(app, "active", client=None)
        now = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
        with patch("schedule_bot.schedule_monitor.download", new_callable=AsyncMock) as download, \
             patch("schedule_bot.schedule_monitor.parse_in_worker", new_callable=AsyncMock) as parse:
            download.return_value = (b"%PDF-changed", PDF_URL)
            self.assertEqual(await monitor.check(now), "already-checked")
            self.assertEqual(await monitor.check(now), "already-checked")
            download.assert_awaited_once()
            parse.assert_not_awaited()
        self.assertEqual(monitor.store.load(), legacy)
        self.bot.do_api_request.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_real_pdf_to_cached_card_to_update_without_extra_downloads(self):
        fixture = Path(__file__).parent / "fixtures/timacad"
        html = (fixture / "source.html").read_bytes()
        pdf = (fixture / "schedule.pdf").read_bytes()
        requests = []

        def university(request):
            requests.append(request.url.path)
            return httpx.Response(200, content=pdf if request.url.path.endswith(".pdf") else html)

        app = SimpleNamespace(bot=self.bot, bot_data={
            "settings": self.settings, "storage": self.storage,
            "schedule_widget_cards": self.cards, "active": {12},
        })
        self.storage.set_current_week_kind(date(2026, 11, 9), WeekKind.LOWER, user_id=42)
        async with httpx.AsyncClient(transport=httpx.MockTransport(university)) as client:
            monitor = ScheduleMonitor(app, "active", client)
            self.assertEqual(await monitor.check(datetime(2026, 11, 9, 8, tzinfo=timezone.utc)), "baseline")
            self.bot.do_api_request.return_value = {"message_id": 54}
            with patch("schedule_bot.schedule_widgets.datetime") as clock:
                clock.now.return_value = datetime(2026, 11, 9, 11)
                self.assertTrue(await self.cards.open(12))
            source = self.bot.do_api_request.await_args.kwargs["api_kwargs"]["html"].encode()
            self.assertIn(b'"kind":"lower"', source)
            self.assertLessEqual(len(source), 32768)
            self.assertLessEqual(len(zlib.compress(source, 9)), 4096)
            self.assertEqual(len(requests), 2)
            pdf = (fixture / "schedule-2026-10-27.pdf").read_bytes()
            self.bot.do_api_request.return_value = True
            checked = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
            self.assertEqual(await monitor.check(checked), "changed")
            self.assertEqual(await monitor.check(checked), "already-checked")

        weekly = monitor.store.load()["widget_weeks"]
        group23, group24 = weekly["ДЭ 23-24"], weekly["ДЭ 24-24"]
        self.assertEqual([len(day) for day in group23["upper"]], [3, 3, 3, 3, 4, 0])
        self.assertEqual([len(day) for day in group23["lower"]], [3, 3, 2, 4, 4, 0])
        self.assertEqual([len(day) for day in group24["upper"]], [2, 3, 4, 3, 4, 0])
        self.assertEqual([len(day) for day in group24["lower"]], [3, 3, 4, 2, 4, 0])
        for group in (group23, group24):
            upper, lower = group["upper"][0], group["lower"][0]
            self.assertEqual(upper[0], lower[0])
            self.assertIn("ВОДЯНИЦКИЙ", upper[0]["subject"])
            self.assertIn("Администрирование", upper[1]["subject"])
            self.assertIn("Фронтенд", lower[1]["subject"])
        self.assertEqual(group24["lower"][0][2]["room"], "02-106")
        self.assertEqual(len(requests), 4)
        _, edit = self.bot.do_api_request.await_args_list
        self.assertEqual(edit.args, ("edit_message_widget_data",))
        public_data = edit.kwargs["api_kwargs"]["public_data"]
        self.assertLessEqual(len(public_data.encode()), 1024)
        self.assertEqual(len(json.loads(public_data)["patches"]), 6)
