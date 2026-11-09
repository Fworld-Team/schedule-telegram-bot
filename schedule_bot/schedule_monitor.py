"""Quiet daily monitor with persistent baseline and at-most-once send attempts."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4

import httpx
from telegram.error import BadRequest, Forbidden, TelegramError

from .schedule_pdf import (
    DAYS, GROUPS, MAX_PDF_BYTES, PAGE_URL, Schedule, ScheduleParseError,
    find_pdf_url, safe_url, validate_schedule,
)


from .widget_html import WidgetDataError, validate_week_data


LOGGER = logging.getLogger(__name__)
DAILY_CHECK_HOUR = 9  # Existing configured bot timezone (production: Moscow).
WAKE_SECONDS = 300
RETRY_SECONDS = 1800
TASK_KEY = "schedule_monitor_task"
MESSAGE_LIMIT = 4000  # UTF-16 units, conservatively below Telegram's 4096.


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class StateStore:
    def __init__(self, directory: Path):
        self.path = directory / "schedule_monitor" / "state.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {"schema": 1, "last_successful_check": None, "groups": None}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("schema") != 1:
                raise ValueError("Unknown state schema")
            if state.get("groups") is not None:
                state["groups"] = validate_schedule(state["groups"])
                date.fromisoformat(state["last_successful_check"])
                safe_url(state["pdf_url"])
            else:
                # A real saved file always has an accepted baseline. Only a
                # genuinely absent file is allowed to initialize from scratch.
                raise ValueError("Saved state without baseline")
            if state.get("widget_weeks") is not None:
                try:
                    state["widget_weeks"] = validate_week_data(state["widget_weeks"])
                except WidgetDataError:
                    state.pop("widget_weeks")
            if "pending" in state:
                pending = state["pending"]
                if not isinstance(pending["event"], str) or not isinstance(pending["attempted"], list):
                    raise ValueError("Invalid pending event")
                if not all(isinstance(k, str) for k in pending["attempted"]):
                    raise ValueError("Invalid delivery journal")
                if not isinstance(pending["messages"], list) or not pending["messages"]:
                    raise ValueError("Invalid pending messages")
                if not all(isinstance(m, str) and 0 < _units(m) <= MESSAGE_LIMIT
                           for m in pending["messages"]):
                    raise ValueError("Invalid pending message text")
            return state
        except (ValueError, TypeError, KeyError) as exc:
            # Never treat damaged state as an empty baseline or overwrite it.
            raise ScheduleParseError("Damaged schedule monitor state; restore from backup") from exc

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".state-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            # The rename must survive a host crash, not just a process restart.
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


async def download(client: httpx.AsyncClient, url: str, limit: int) -> tuple[bytes, str]:
    """No conditional requests; fresh nonce + cache headers on every redirect."""
    headers = {"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"}
    for _ in range(6):
        safe_url(url)
        request_url = httpx.URL(url).copy_merge_params({"_schedule_monitor": uuid4().hex})
        async with client.stream("GET", request_url, headers=headers) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                url = str(httpx.URL(url).join(response.headers["location"]))
                continue
            if response.status_code != 200:
                if response.is_error:
                    response.raise_for_status()
                raise ScheduleParseError("Expected a fresh HTTP 200 response")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise ScheduleParseError("Source exceeds download size limit")
            if limit == MAX_PDF_BYTES and (
                    not content.startswith(b"%PDF-")
                    or "html" in response.headers.get("content-type", "").lower()):
                raise ScheduleParseError("Server returned non-PDF content")
            return bytes(content), url
    raise ScheduleParseError("Too many source redirects")


async def parse_in_worker(content: bytes) -> tuple[Schedule, dict | None]:
    """PDF work cannot monopolize the polling event loop; kill on timeout/stop."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "schedule_bot.schedule_pdf",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(content), timeout=90)
        if process.returncode:
            raise ScheduleParseError(error.decode(errors="replace")[-1000:])
        parsed = json.loads(output)
        weekly = parsed["widget_weeks"]
        return validate_schedule(parsed["groups"]), validate_week_data(weekly) if weekly is not None else None
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


@dataclass(frozen=True)
class Snapshot:
    pdf_url: str
    pdf_hash: str
    groups: Schedule
    widget_weeks: dict | None = None


async def fetch_snapshot(client: httpx.AsyncClient) -> Snapshot:
    async with asyncio.timeout(150):
        page, _ = await download(client, PAGE_URL, 2 * 1024 * 1024)
        url = find_pdf_url(page.decode("utf-8"))
        content, url = await download(client, url, MAX_PDF_BYTES)
        groups, weekly = await parse_in_worker(content)
        return Snapshot(url, hashlib.sha256(content).hexdigest(), groups, weekly)


@dataclass(frozen=True)
class Change:
    group: str
    day: str
    time: str
    old: str
    new: str
    old_time: str | None = None

    def description(self) -> str:
        if self.old_time:
            return f"Время: {self.old_time} → {self.time}\n{self.new}"
        if not self.old:
            return f"Добавлена пара:\n{self.new}"
        if not self.new:
            return f"Удалена пара:\n{self.old}"
        # Only classify a room when the entire remaining text is identical.
        pattern = r"(.*?)(\b\d{2}-[\dА-Яа-яA-Za-z]+)$"
        old_room, new_room = re.fullmatch(pattern, self.old), re.fullmatch(pattern, self.new)
        if old_room and new_room and old_room[1] == new_room[1]:
            return f"Кабинет: {old_room[2]} → {new_room[2]}"
        return f"Изменено занятие\nБыло: {self.old}\nСтало: {self.new}"


def diff_schedule(old: Schedule, new: Schedule) -> list[Change]:
    changes = []
    for group in GROUPS:
        before = {(r["day"], r["time"]): r["lesson"] for r in old[group] if r["lesson"]}
        after = {(r["day"], r["time"]): r["lesson"] for r in new[group] if r["lesson"]}
        removed = {k: v for k, v in before.items() if k not in after}
        added = {k: v for k, v in after.items() if k not in before}
        moved = set()
        for key, lesson in removed.items():
            matches = [k for k, v in added.items() if k[0] == key[0] and v == lesson]
            reverse = [k for k, v in removed.items() if k[0] == key[0] and v == lesson]
            if len(matches) == len(reverse) == 1:
                target = matches[0]
                changes.append(Change(group, target[0], target[1], lesson, lesson, key[1]))
                moved.update((key, target))
        for day, time in before.keys() | after.keys():
            key = (day, time)
            if key not in moved and before.get(key, "") != after.get(key, ""):
                changes.append(Change(group, day, time, before.get(key, ""), after.get(key, "")))
    return sorted(changes, key=lambda c: (GROUPS.index(c.group), DAYS.index(c.day), c.time))


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def format_notifications(changes: list[Change], pdf_url: str) -> list[str]:
    if not changes:
        return []
    header = "🔔 Расписание обновилось\n\n"
    footer = f"\n\nПроверь актуальное расписание на сайте.\n\nPDF:\n{safe_url(pdf_url)}"
    budget = MESSAGE_LIMIT - _units(header + footer)
    if budget < 200:
        raise ScheduleParseError("PDF URL too long for a notification")
    chunks, current, current_group = [], "", None
    for change in changes:
        label = f"{change.day}, {change.time.replace('-', '–')}"
        block = f"{label}\n{change.description()}"
        # Prefer whole change blocks; exceptionally long cells are split safely.
        while block:
            separator = "\n\n" if current else ""
            group_title = f"{change.group}\n\n" if current_group != change.group else ""
            available = budget - _units(current + separator + group_title)
            if _units(block) <= available:
                current += separator + group_title + block
                current_group = change.group
                break
            if current:
                chunks.append(header + current + footer)
                current, current_group = "", None
                continue
            cut, used = 0, 0
            for char in block:
                if used + _units(char) > available:
                    break
                used += _units(char)
                cut += 1
            chunks.append(header + group_title + block[:cut] + footer)
            block = f"{label} (продолжение)\n" + block[cut:]
    if current:
        chunks.append(header + current + footer)
    return chunks


class ScheduleMonitor:
    def __init__(self, application, active_chat_ids_key: str, client: httpx.AsyncClient):
        self.application = application
        self.active_chat_ids_key = active_chat_ids_key
        self.client = client
        self.settings = application.bot_data["settings"]
        self.store = StateStore(self.settings.runtime_data_directory)
        self.lock = asyncio.Lock()
        self.widget_backfill_attempted = False

    def active_chats(self) -> set[int]:
        # Same authoritative in-memory set used by activity_gate, populated
        # from BotStorage.active_chat_ids() on startup. No monitor subscribers.
        return set(self.application.bot_data[self.active_chat_ids_key])

    def deactivate(self, chat_id: int) -> None:
        self.application.bot_data["storage"].set_chat_active(chat_id, False)
        self.application.bot_data[self.active_chat_ids_key].discard(chat_id)

    async def check(self, now: datetime | None = None) -> str:
        """One eligible check. Failures propagate only to the private log loop."""
        async with self.lock:
            local_timezone = timezone(timedelta(hours=self.settings.utc_offset_hours))
            now = now or datetime.now(local_timezone)
            if now.tzinfo is None:
                raise ValueError("An aware datetime is required")
            now = now.astimezone(local_timezone)
            today = now.date().isoformat()
            state = self.store.load()
            if state["last_successful_check"] and state["last_successful_check"] >= today:
                await self.backfill_widget_weeks(state)
                await self.refresh_widgets(state, now.date())
                return "already-checked"
            if now.hour < DAILY_CHECK_HOUR:
                await self.backfill_widget_weeks(state)
                await self.refresh_widgets(state, now.date())
                return "not-due"
            try:
                snapshot = await fetch_snapshot(self.client)
            except (httpx.HTTPError, ScheduleParseError, TimeoutError):
                await self.refresh_widgets(state, now.date())
                raise
            groups = validate_schedule(snapshot.groups)
            old = state["groups"]
            if old:
                for group in GROUPS:
                    if not {r["day"] for r in old[group]} <= {r["day"] for r in groups[group]}:
                        raise ScheduleParseError("A previously parsed weekday is missing")
            changes = diff_schedule(old, groups) if old else []
            messages = format_notifications(changes, snapshot.pdf_url)
            if messages:
                event = digest({"old": old, "new": groups})
                pending = state.get("pending")
                if pending is None or pending["event"] != event:
                    state["pending"] = {"event": event, "attempted": [], "messages": messages}
                    self.store.save(state)
                # Retain chunk boundaries even if the same content moved to
                # a new PDF URL during recovery from an interrupted broadcast.
                await self.deliver(state, state["pending"]["messages"])
            accepted = {"schema": 1, "last_successful_check": today,
                        "pdf_url": snapshot.pdf_url, "pdf_hash": snapshot.pdf_hash,
                        "groups": groups, "widget_weeks": snapshot.widget_weeks}
            self.store.save(accepted)
            await self.refresh_widgets(accepted, now.date())
            if old is None:
                LOGGER.info("Schedule monitor baseline initialized")
            else:
                LOGGER.info("Schedule monitor check accepted: %s changed slots", len(changes))
            return "changed" if changes else "baseline" if old is None else "unchanged"

    async def backfill_widget_weeks(self, state: dict) -> None:
        """Recover the widget data from the accepted PDF without advancing the daily check."""
        if (
            self.widget_backfill_attempted
            or self.application.bot_data.get("schedule_widget_cards") is None
            or state.get("widget_weeks") is not None
            or not state.get("pdf_hash")
        ):
            return

        self.widget_backfill_attempted = True
        try:
            async with asyncio.timeout(120):
                content, _ = await download(self.client, state["pdf_url"], MAX_PDF_BYTES)
                if hashlib.sha256(content).hexdigest() != state["pdf_hash"]:
                    LOGGER.info("Schedule widget backfill deferred: the accepted PDF has changed")
                    return
                _, weekly = await parse_in_worker(content)
        except (httpx.HTTPError, ScheduleParseError, WidgetDataError, TimeoutError) as exc:
            LOGGER.warning("Schedule widget backfill failed: %s", type(exc).__name__)
            return

        if weekly is None:
            LOGGER.info("Schedule widget backfill deferred: no verified week variants")
            return
        if self.store.load() != state:
            return  # Another check advanced the baseline during the download.
        self.store.save({**state, "widget_weeks": weekly})
        state["widget_weeks"] = weekly
        LOGGER.info("Schedule widget cache backfilled from the accepted PDF")

    async def refresh_widgets(self, state: dict, today: date) -> None:
        cards = self.application.bot_data.get("schedule_widget_cards")
        weekly = state.get("widget_weeks")
        if cards is not None and weekly is not None:
            await cards.refresh(weekly, today, self.active_chats)

    async def deliver(self, state: dict, messages: list[str]) -> None:
        for chat_id in sorted(self.active_chats()):
            for index, message in enumerate(messages):
                if index:
                    # Multipart updates must respect per-chat/group limits,
                    # not just the global broadcast rate. Never block polling.
                    await asyncio.sleep(3.1 if chat_id < 0 else 1.1)
                if chat_id not in self.active_chats():
                    break  # /stop during a multipart broadcast takes effect immediately.
                key = f"{chat_id}:{index}"
                if key in state["pending"]["attempted"]:
                    continue
                # Telegram has no idempotency key. Journal BEFORE sending:
                # ambiguous timeouts/process death must not cause spam on retry.
                journal = deepcopy(state)
                journal["pending"]["attempted"].append(key)
                self.store.save(journal)
                state["pending"] = journal["pending"]
                try:
                    await self.application.bot.send_message(
                        chat_id=chat_id, text=message, parse_mode=None,
                        disable_web_page_preview=True, connect_timeout=10,
                        read_timeout=20, write_timeout=20, pool_timeout=10,
                    )
                except Forbidden:
                    self.deactivate(chat_id)
                    LOGGER.warning("Schedule monitor deactivated inaccessible chat %s", chat_id)
                    break
                except BadRequest as exc:
                    if str(exc).lower() in {"chat not found", "group chat was deleted",
                                          "bot was kicked from the group chat"}:
                        self.deactivate(chat_id)
                        break
                    LOGGER.warning("Schedule monitor send rejected for %s: %s", chat_id, exc)
                except TelegramError as exc:
                    LOGGER.warning("Schedule monitor send attempt failed for %s: %s", chat_id, exc)
                await asyncio.sleep(0.1)  # Bound broadcast rate; no queue pressure on polling.


async def schedule_monitor(application, active_chat_ids_key: str) -> None:
    settings = application.bot_data["settings"]
    async with httpx.AsyncClient(proxy=settings.proxy_url, trust_env=False,
                                 timeout=httpx.Timeout(20, connect=10),
                                 follow_redirects=False) as client:
        monitor = ScheduleMonitor(application, active_chat_ids_key, client)
        while True:
            delay = WAKE_SECONDS
            try:
                await monitor.check()
            except Exception:
                LOGGER.exception("Schedule monitor check failed; baseline retained")
                delay = RETRY_SECONDS
            await asyncio.sleep(delay)


def start_monitor(application, active_chat_ids_key: str) -> None:
    # post_init precedes Application.start. Own the task explicitly so PTB's
    # stop() never waits indefinitely for an infinite create_task coroutine.
    application.bot_data[TASK_KEY] = asyncio.create_task(
        schedule_monitor(application, active_chat_ids_key), name="schedule-monitor")


async def stop_monitor(application) -> None:
    task = application.bot_data.pop(TASK_KEY, None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
