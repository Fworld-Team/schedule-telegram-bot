import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from telegram import ReplyKeyboardRemove
from telegram.ext import ApplicationHandlerStop
from telegram.error import BadRequest, Forbidden, TimedOut

from schedule_bot.bot import (
    ACTIVE_CHAT_IDS_KEY,
    EXIT_BUTTON,
    MAIN_KEYBOARD,
    activity_gate,
    route_text,
    POLLING_WATCHDOG_TASK_KEY,
    initialize_application,
    shutdown_application,
    start,
    stop_command,
)
from schedule_bot.schedule_monitor import (
    MESSAGE_LIMIT, ScheduleMonitor, Snapshot, StateStore, TASK_KEY, _units,
    diff_schedule, download, fetch_snapshot, format_notifications, schedule_monitor,
    start_monitor, stop_monitor,
    parse_in_worker,
)
from schedule_bot.schedule_pdf import GROUPS, MAX_PDF_BYTES, ScheduleParseError
from schedule_bot.settings import Settings
from schedule_bot.storage import BotStorage
from tests.test_handlers import FakeMessage
from tests.test_schedule_pdf import HTML, PDF_URL, sample_groups


NOW = datetime(2026, 9, 28, 10, tzinfo=timezone(timedelta(hours=3)))


class DiffTests(unittest.TestCase):
    def test_production_state_uses_persistent_data_volume(self):
        self.assertEqual(StateStore(Path("/data")).path, Path("/data/schedule_monitor/state.json"))

    def test_no_changes_zero_notifications(self):
        self.assertEqual(format_notifications(diff_schedule(sample_groups(), sample_groups()), PDF_URL), [])

    def test_room_change(self):
        old, new = sample_groups(), sample_groups()
        new[GROUPS[0]][0]["lesson"] = old[GROUPS[0]][0]["lesson"].replace("01-216", "01-214")
        changes = diff_schedule(old, new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].description(), "Кабинет: 01-216 → 01-214")

    def test_teacher_subject_type_and_complete_change(self):
        for before, after in (("ИВАНОВ", "ПЕТРОВ"), ("Предмет", "Математика"),
                              ("лек.", "пр."), (sample_groups()[GROUPS[0]][0]["lesson"], "Другая пара")):
            old, new = sample_groups(), sample_groups()
            new[GROUPS[0]][0]["lesson"] = new[GROUPS[0]][0]["lesson"].replace(before, after)
            changes = diff_schedule(old, new)
            self.assertEqual(len(changes), 1)
            self.assertIn("Было:", changes[0].description())
            self.assertIn(after, changes[0].description())

    def test_time_change(self):
        old, new = sample_groups(), sample_groups()
        new[GROUPS[0]][1]["lesson"] = new[GROUPS[0]][0]["lesson"]
        new[GROUPS[0]][0]["lesson"] = ""
        change, = diff_schedule(old, new)
        self.assertEqual(change.old_time, "09:00-10:35")
        self.assertEqual(change.time, "10:55-12:30")

    def test_addition_and_removal(self):
        old, new = sample_groups(), sample_groups()
        new[GROUPS[0]][1]["lesson"] = "Новый предмет"
        added, = diff_schedule(old, new)
        removed, = diff_schedule(new, old)
        self.assertIn("Добавлена пара", added.description())
        self.assertIn("Удалена пара", removed.description())

    def test_one_other_or_both_groups(self):
        for targets in ((GROUPS[0],), (GROUPS[1],), GROUPS):
            old, new = sample_groups(), sample_groups()
            for group in targets:
                new[group][0]["lesson"] = "Другая пара"
            changes = diff_schedule(old, new)
            self.assertEqual(tuple(c.group for c in changes), targets)
            messages = format_notifications(changes, PDF_URL)
            self.assertEqual(len(messages), 1)
            self.assertIn("Проверь актуальное расписание на сайте.", messages[0])
            self.assertIn(PDF_URL, messages[0])

    def test_several_changes_grouped_in_single_message(self):
        old, new = sample_groups(), sample_groups()
        for group in GROUPS:
            for index in (0, 2, 8):
                new[group][index]["lesson"] = "Новый предмет 03-101"
        message, = format_notifications(diff_schedule(old, new), PDF_URL)
        for group in GROUPS:
            self.assertEqual(message.count(group), 1)
        self.assertLess(message.index("Понедельник"), message.index("Вторник"))

    def test_long_unicode_messages_fit_telegram_without_loss(self):
        old, new = sample_groups(), sample_groups()
        new[GROUPS[0]][0]["lesson"] = "🔬" * 6000
        messages = format_notifications(diff_schedule(old, new), PDF_URL)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(_units(m) <= MESSAGE_LIMIT for m in messages))
        self.assertEqual("".join(messages).count("🔬"), 6000)
        self.assertTrue(all(GROUPS[0] in m and "Понедельник" in m for m in messages))


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root, "test", root / "schedules", root / "maps", root / "token.txt",
                                 9, 1, 3, data_directory=root / "data")
        self.storage = BotStorage(self.settings.runtime_data_directory, root / "schedules", root / "maps")
        self.storage.initialize(NOW.date())
        for chat in (42, -10099):
            self.storage.set_chat_active(chat, True)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.app = SimpleNamespace(bot=self.bot, bot_data={"settings": self.settings,
                                  "storage": self.storage, ACTIVE_CHAT_IDS_KEY: self.storage.active_chat_ids()})
        self.monitor = ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, None)
        self.groups = sample_groups()
        self.fetch = AsyncMock(return_value=Snapshot(PDF_URL, "hash-1", self.groups))
        self.patch_fetch = patch("schedule_bot.schedule_monitor.fetch_snapshot", self.fetch)
        self.patch_fetch.start()
        self.addCleanup(self.patch_fetch.stop)

    async def baseline(self):
        await self.monitor.check(NOW - timedelta(days=1))
        self.bot.send_message.reset_mock()
        self.fetch.reset_mock()

    def changed(self):
        self.groups[GROUPS[0]][0]["lesson"] = "Другой предмет 03-101"

    async def test_first_baseline_is_silent_and_persistent(self):
        self.assertEqual(await self.monitor.check(NOW), "baseline")
        self.bot.send_message.assert_not_awaited()
        state = self.monitor.store.load()
        self.assertEqual(state["last_successful_check"], "2026-09-28")
        self.assertEqual(state["groups"], sample_groups())

    async def test_no_changes_zero_send_message_calls(self):
        await self.baseline()
        self.assertEqual(await self.monitor.check(NOW), "unchanged")
        self.assertEqual(self.bot.send_message.call_count, 0)

    async def test_full_pipeline_pdf_replacement_is_silent_offline(self):
        self.patch_fetch.stop()
        fixture = Path(__file__).parent / "fixtures/timacad"
        html = (fixture / "source.html").read_bytes()
        pdf = (fixture / "schedule.pdf").read_bytes()
        body = pdf
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=body if request.url.path.endswith(".pdf") else html)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.monitor.client = client
            await self.monitor.check(NOW - timedelta(days=1))
            previous_hash = self.monitor.store.load()["pdf_hash"]
            body = pdf + b"\n% replaced file at the same URL\n"
            await self.monitor.check(NOW)
            self.assertNotEqual(self.monitor.store.load()["pdf_hash"], previous_hash)
            restarted = ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, client)
            await restarted.check(NOW)
        self.assertEqual(len(requests), 4)
        self.assertEqual(self.bot.send_message.call_count, 0)

    async def test_full_pipeline_http_and_pdf_failures_send_nothing_offline(self):
        await self.baseline()
        self.patch_fetch.stop()
        previous = self.monitor.store.path.read_bytes()
        for broken_stage in ("page", "pdf", "html", "corrupt"):
            def handler(request):
                if not request.url.path.endswith(".pdf"):
                    return httpx.Response(503) if broken_stage == "page" else httpx.Response(200, text=HTML)
                if broken_stage == "pdf":
                    return httpx.Response(404)
                return httpx.Response(200, content=b"<html>error</html>" if broken_stage == "html" else b"%PDF-broken")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.monitor.client = client
                with self.subTest(stage=broken_stage), self.assertRaises((httpx.HTTPError, ScheduleParseError)):
                    await self.monitor.check(NOW)
            self.assertEqual(self.monitor.store.path.read_bytes(), previous)
            self.assertEqual(self.bot.send_message.call_count, 0)

    async def test_pdf_changed_target_groups_unchanged_zero_messages(self):
        await self.baseline()
        self.fetch.return_value = Snapshot(PDF_URL + "?v=new", "hash-other", self.groups)
        self.assertEqual(await self.monitor.check(NOW), "unchanged")
        self.assertEqual(self.bot.send_message.call_count, 0)
        self.assertEqual(self.monitor.store.load()["pdf_hash"], "hash-other")

    async def test_same_day_and_restart_do_not_even_fetch(self):
        await self.monitor.check(NOW)
        self.fetch.reset_mock()
        for monitor in (self.monitor, ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, None)):
            self.assertEqual(await monitor.check(NOW), "already-checked")
        self.fetch.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_before_due_no_network(self):
        self.assertEqual(await self.monitor.check(NOW.replace(hour=8)), "not-due")
        self.fetch.assert_not_awaited()

    async def test_date_uses_bot_timezone(self):
        await self.monitor.check(NOW)
        self.assertEqual(await self.monitor.check(NOW.astimezone(timezone.utc)), "already-checked")

    async def test_current_private_and_group_once_not_inactive(self):
        await self.baseline()
        self.storage.set_chat_active(999, True)
        self.storage.set_chat_active(999, False)
        self.changed()
        self.assertEqual(await self.monitor.check(NOW), "changed")
        ids = [call.kwargs["chat_id"] for call in self.bot.send_message.call_args_list]
        self.assertCountEqual(ids, [42, -10099])
        self.assertEqual(len(ids), 2)
        await self.monitor.check(NOW + timedelta(days=1))
        self.assertEqual(self.bot.send_message.call_count, 2)

    async def test_existing_start_and_stop_control_recipients(self):
        context = SimpleNamespace(application=self.app)
        await stop_command(SimpleNamespace(effective_message=FakeMessage("/stop"),
                                          effective_chat=SimpleNamespace(id=42)), context)
        await start(SimpleNamespace(effective_message=FakeMessage("/start"),
                                    effective_chat=SimpleNamespace(id=123)), context)
        await self.baseline()
        self.changed()
        await self.monitor.check(NOW)
        self.assertCountEqual([c.kwargs["chat_id"] for c in self.bot.send_message.call_args_list], [-10099, 123])

    async def test_exit_preserves_private_and_group_notification_delivery(self):
        context = SimpleNamespace(application=self.app)
        chat_ids = {42, -10099}

        def update(text, chat_id):
            return SimpleNamespace(
                effective_message=FakeMessage(text),
                effective_chat=SimpleNamespace(
                    id=chat_id, type="private" if chat_id > 0 else "supergroup",
                ),
                callback_query=None,
            )

        await self.baseline()
        with patch.object(self.storage, "set_chat_active", wraps=self.storage.set_chat_active) as set_active:
            for chat_id in chat_ids:
                exit_update = update(EXIT_BUTTON, chat_id)
                await activity_gate(exit_update, context)
                await route_text(exit_update, context)
                self.assertIsInstance(exit_update.effective_message.reply_markups[0], ReplyKeyboardRemove)
            set_active.assert_not_called()
        self.assertEqual(self.storage.active_chat_ids(), chat_ids)
        self.assertEqual(self.app.bot_data[ACTIVE_CHAT_IDS_KEY], chat_ids)
        self.changed()
        self.assertEqual(await self.monitor.check(NOW), "changed")
        self.assertCountEqual([c.kwargs["chat_id"] for c in self.bot.send_message.await_args_list], chat_ids)

        for chat_id in chat_ids:
            await stop_command(update("/stop", chat_id), context)
            stale_exit = update(EXIT_BUTTON, chat_id)
            with self.assertRaises(ApplicationHandlerStop):
                await activity_gate(stale_exit, context)
        self.assertEqual(self.storage.active_chat_ids(), set())
        self.assertEqual(self.app.bot_data[ACTIVE_CHAT_IDS_KEY], set())
        self.bot.send_message.reset_mock()
        self.groups[GROUPS[0]][0]["lesson"] = "Следующее изменение"
        self.assertEqual(await self.monitor.check(NOW + timedelta(days=1)), "changed")
        self.bot.send_message.assert_not_awaited()

        for chat_id in chat_ids:
            start_update = update("/start", chat_id)
            await activity_gate(start_update, context)
            await start(start_update, context)
            self.assertIs(start_update.effective_message.reply_markups[0], MAIN_KEYBOARD)
        self.assertEqual(self.storage.active_chat_ids(), chat_ids)
        self.assertEqual(self.app.bot_data[ACTIVE_CHAT_IDS_KEY], chat_ids)
        self.groups[GROUPS[0]][0]["lesson"] = "Изменение после включения"
        self.assertEqual(await self.monitor.check(NOW + timedelta(days=2)), "changed")
        self.assertCountEqual([c.kwargs["chat_id"] for c in self.bot.send_message.await_args_list], chat_ids)

    async def test_no_active_chats_accepts_baseline_without_sending(self):
        await self.baseline()
        self.app.bot_data[ACTIVE_CHAT_IDS_KEY].clear()
        self.changed()
        await self.monitor.check(NOW)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.monitor.store.load()["groups"], self.groups)

    async def test_parser_site_pdf_failures_preserve_baseline_and_are_silent(self):
        await self.baseline()
        previous = self.monitor.store.path.read_bytes()
        for error in (ScheduleParseError("parser error"), httpx.ConnectError("site offline"),
                      httpx.ReadTimeout("PDF offline"), ScheduleParseError("HTML instead of PDF"),
                      ScheduleParseError("damaged PDF")):
            with self.subTest(error=str(error)):
                self.fetch.side_effect = error
                with self.assertRaises(type(error)):
                    await self.monitor.check(NOW)
                self.assertEqual(self.monitor.store.path.read_bytes(), previous)
                self.bot.send_message.assert_not_awaited()

    async def test_missing_one_or_both_groups_silent(self):
        await self.baseline()
        old = self.monitor.store.path.read_bytes()
        for groups in ({GROUPS[0]: self.groups[GROUPS[0]]}, {}, {GROUPS[0]: [], GROUPS[1]: []}):
            self.fetch.return_value = Snapshot(PDF_URL, "other", groups)
            with self.assertRaises(ScheduleParseError):
                await self.monitor.check(NOW)
            self.assertEqual(self.monitor.store.path.read_bytes(), old)
        self.bot.send_message.assert_not_awaited()

    async def test_interrupted_fetch_retries_after_restart(self):
        await self.baseline()
        self.fetch.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.monitor.check(NOW)
        self.assertEqual(self.monitor.store.load()["last_successful_check"], "2026-09-27")
        self.fetch.side_effect = None
        restarted = ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, None)
        await restarted.check(NOW)
        self.assertEqual(self.fetch.await_count, 2)
        self.bot.send_message.assert_not_awaited()

    async def test_interruption_during_send_does_not_repeat_attempt(self):
        await self.baseline()
        self.changed()
        self.bot.send_message.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.monitor.check(NOW)
        self.assertEqual(self.monitor.store.load()["last_successful_check"], "2026-09-27")
        first = self.bot.send_message.call_args.kwargs["chat_id"]
        self.bot.send_message.side_effect = None
        await ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, None).check(NOW)
        ids = [c.kwargs["chat_id"] for c in self.bot.send_message.call_args_list]
        self.assertCountEqual(ids, [42, -10099])
        self.assertEqual(ids.count(first), 1)

    async def test_failed_final_save_does_not_repeat_broadcast_on_restart(self):
        await self.baseline()
        self.changed()
        real_save = self.monitor.store.save
        def fail_final(state):
            if state.get("last_successful_check") == "2026-09-28":
                raise OSError("disk full")
            real_save(state)
        with patch.object(self.monitor.store, "save", side_effect=fail_final), self.assertRaises(OSError):
            await self.monitor.check(NOW)
        self.assertEqual(self.bot.send_message.call_count, 2)
        await ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, None).check(NOW)
        self.assertEqual(self.bot.send_message.call_count, 2)

    async def test_failed_journal_save_sends_nothing(self):
        await self.baseline()
        self.changed()
        with patch.object(self.monitor.store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            await self.monitor.check(NOW)
        self.bot.send_message.assert_not_awaited()

    async def test_forbidden_and_chat_not_found_deactivate_existing_storage(self):
        await self.baseline()
        self.changed()
        self.bot.send_message.side_effect = [Forbidden("blocked"), BadRequest("Chat not found")]
        await self.monitor.check(NOW)
        self.assertEqual(self.storage.active_chat_ids(), set())
        self.assertEqual(self.app.bot_data[ACTIVE_CHAT_IDS_KEY], set())

    async def test_ambiguous_network_timeout_not_retried_or_deactivated(self):
        await self.baseline()
        self.changed()
        self.bot.send_message.side_effect = TimedOut()
        await self.monitor.check(NOW)
        await self.monitor.check(NOW + timedelta(days=1))
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.assertEqual(self.storage.active_chat_ids(), {42, -10099})

    async def test_corrupted_state_does_not_reset_or_send(self):
        await self.baseline()
        for content in ('{broken', '{"schema": 1, "groups": {}}', '{"schema": 5}',
                        '{"schema": 1, "groups": null, "last_successful_check": null}'):
            self.monitor.store.path.write_text(content)
            with self.assertRaises(ScheduleParseError):
                await self.monitor.check(NOW)
            self.assertEqual(self.monitor.store.path.read_text(), content)
        self.fetch.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_atomic_replace_failure_keeps_old_state(self):
        await self.baseline()
        old = self.monitor.store.path.read_bytes()
        state = self.monitor.store.load()
        state["last_successful_check"] = "2026-09-28"
        with patch("schedule_bot.schedule_monitor.os.replace", side_effect=OSError("failed")), self.assertRaises(OSError):
            self.monitor.store.save(state)
        self.assertEqual(self.monitor.store.path.read_bytes(), old)
        self.assertEqual(list(self.monitor.store.path.parent.glob(".state-*.tmp")), [])

    async def test_atomic_write_flush_fsync_before_replace(self):
        await self.baseline()
        events = []
        import os
        real_replace, real_fsync = os.replace, os.fsync
        def sync(fd):
            events.append("fsync")
            real_fsync(fd)
        def replace(source, target):
            # Complete JSON is readable before publication.
            self.assertEqual(json.loads(Path(source).read_text())["schema"], 1)
            events.append("replace")
            real_replace(source, target)
        with patch("schedule_bot.schedule_monitor.os.fsync", side_effect=sync), \
             patch("schedule_bot.schedule_monitor.os.replace", side_effect=replace):
            self.monitor.store.save(self.monitor.store.load())
        self.assertEqual(events[:2], ["fsync", "replace"])

    async def test_existing_storage_reloads_after_process_restart(self):
        await self.baseline()
        self.changed()
        reopened = BotStorage(self.settings.runtime_data_directory, self.settings.schedules_directory,
                              self.settings.maps_directory)
        self.app.bot_data[ACTIVE_CHAT_IDS_KEY] = reopened.active_chat_ids()
        self.app.bot_data["storage"] = reopened
        await ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, None).check(NOW)
        self.assertCountEqual([c.kwargs["chat_id"] for c in self.bot.send_message.call_args_list], [42, -10099])

    async def test_deactivation_mid_broadcast_immediately_skips_chat(self):
        await self.baseline()
        self.changed()
        async def stop_next(**kwargs):
            self.app.bot_data[ACTIVE_CHAT_IDS_KEY].discard(42)
            self.storage.set_chat_active(42, False)
        self.bot.send_message.side_effect = stop_next
        await self.monitor.check(NOW)
        self.assertEqual(self.bot.send_message.call_count, 1)

    async def test_multipart_broadcast_spaces_parts_for_private_and_group(self):
        await self.baseline()
        self.changed()
        with patch("schedule_bot.schedule_monitor.format_notifications", return_value=["part one", "part two"]), \
             patch("schedule_bot.schedule_monitor.asyncio.sleep", new=AsyncMock()) as sleep:
            await self.monitor.check(NOW)
        self.assertEqual(self.bot.send_message.call_count, 4)
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertIn(3.1, delays)
        self.assertIn(1.1, delays)

    async def test_stop_during_multipart_pause_prevents_next_part(self):
        await self.baseline()
        self.changed()
        async def deactivate_during_pause(seconds):
            if seconds == 3.1:
                self.app.bot_data[ACTIVE_CHAT_IDS_KEY].clear()
        with patch("schedule_bot.schedule_monitor.format_notifications", return_value=["part one", "part two"]), \
             patch("schedule_bot.schedule_monitor.asyncio.sleep", side_effect=deactivate_during_pause):
            await self.monitor.check(NOW)
        self.assertEqual(self.bot.send_message.call_count, 1)

    async def test_unfinished_baseline_save_stays_absent_and_retries(self):
        with patch.object(self.monitor.store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            await self.monitor.check(NOW)
        self.assertFalse(self.monitor.store.path.exists())
        await ScheduleMonitor(self.app, ACTIVE_CHAT_IDS_KEY, None).check(NOW)
        self.assertEqual(self.fetch.await_count, 2)
        self.bot.send_message.assert_not_awaited()

    async def test_concurrent_checks_share_lock(self):
        await asyncio.gather(self.monitor.check(NOW), self.monitor.check(NOW))
        self.fetch.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_background_errors_only_logged_and_retried(self):
        with patch.object(ScheduleMonitor, "check", side_effect=ScheduleParseError("oops")), \
             patch("schedule_bot.schedule_monitor.asyncio.sleep", side_effect=asyncio.CancelledError) as sleep, \
             self.assertLogs("schedule_bot.schedule_monitor", level="ERROR"), \
             self.assertRaises(asyncio.CancelledError):
            await schedule_monitor(self.app, ACTIVE_CHAT_IDS_KEY)
        sleep.assert_called_once_with(1800)
        self.bot.send_message.assert_not_awaited()

    async def test_lifecycle_starts_and_cancels_background_tasks(self):
        async def forever(*args):
            await asyncio.Event().wait()
        with patch("schedule_bot.bot.set_commands", new=AsyncMock()), \
             patch("schedule_bot.schedule_monitor.schedule_monitor", side_effect=forever):
            await initialize_application(self.app)
            monitor_task = self.app.bot_data[TASK_KEY]
            watchdog_task = self.app.bot_data[POLLING_WATCHDOG_TASK_KEY]
            self.assertEqual(monitor_task.get_name(), "schedule-monitor")
            self.assertEqual(watchdog_task.get_name(), "polling-watchdog")
            await asyncio.sleep(0)
            await shutdown_application(self.app)
            await shutdown_application(self.app)
            self.assertTrue(monitor_task.cancelled())
            self.assertTrue(watchdog_task.cancelled())


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pdf_in_subprocess_from_mock_http_offline(self):
        fixture = Path(__file__).parent / "fixtures/timacad"
        html = (fixture / "source.html").read_bytes()
        pdf = (fixture / "schedule.pdf").read_bytes()
        def handler(request):
            return httpx.Response(200, content=pdf if request.url.path.endswith(".pdf") else html)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_snapshot(client)
        self.assertEqual(len(result.groups[GROUPS[0]]), 25)
        self.assertEqual(len(result.groups[GROUPS[1]]), 25)

    async def test_parser_worker_error(self):
        with self.assertRaises(ScheduleParseError):
            await parse_in_worker(b"%PDF-1.7 broken")

    async def test_parser_worker_cancelled_and_killed(self):
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError),
                                  kill=Mock(), wait=AsyncMock())
        with patch("schedule_bot.schedule_monitor.asyncio.create_subprocess_exec", return_value=process), \
             self.assertRaises(asyncio.CancelledError):
            await parse_in_worker(b"%PDF-test")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_parser_worker_timeout_and_killed(self):
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=TimeoutError),
                                  kill=Mock(), wait=AsyncMock())
        with patch("schedule_bot.schedule_monitor.asyncio.create_subprocess_exec", return_value=process), \
             self.assertRaises(TimeoutError):
            await parse_in_worker(b"%PDF-test")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_discovery_and_download_use_cache_busting_every_time(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.endswith(".pdf"):
                return httpx.Response(200, content=b"%PDF-test", headers={"content-type": "application/pdf"})
            return httpx.Response(200, text=HTML)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("schedule_bot.schedule_monitor.parse_in_worker", new=AsyncMock(return_value=sample_groups())):
                await fetch_snapshot(client)
                await fetch_snapshot(client)
        self.assertEqual(len(requests), 4)
        self.assertEqual(len({r.url.params["_schedule_monitor"] for r in requests}), 4)
        for request in requests:
            self.assertIn("no-cache", request.headers["cache-control"])
            self.assertEqual(request.headers["pragma"], "no-cache")
            self.assertNotIn("if-none-match", request.headers)

    async def test_http_error_html_and_size_limit(self):
        for response, expected in ((httpx.Response(503), httpx.HTTPStatusError),
                                   (httpx.Response(200, text="<html>Error</html>"), ScheduleParseError),
                                   (httpx.Response(304), ScheduleParseError)):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
                with self.assertRaises(expected):
                    await download(client, PDF_URL, MAX_PDF_BYTES)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"123456"))) as client:
            with self.assertRaises(ScheduleParseError):
                await download(client, PDF_URL, 5)

    async def test_offsite_redirect_rejected_before_request(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private.pdf"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ScheduleParseError):
                await download(client, PDF_URL, MAX_PDF_BYTES)
        self.assertEqual(len(requests), 1)
