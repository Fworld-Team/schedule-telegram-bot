"""Publish and refresh the last schedule card requested in each private chat."""

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging

from telegram.error import BadRequest, EndPointNotFound, TelegramError

from .schedule_monitor import StateStore, digest
from .widget_html import GROUPS, KINDS, build_widget_html
from .widget_transport import WidgetTransport, encode_state

LOGGER = logging.getLogger(__name__)


def public_patch(base: dict, current: dict, anchor: dict) -> str:
    """Build a cumulative patch against the immutable HTML, not the last edit."""
    patches = []
    for group_index, group in enumerate(GROUPS):
        for kind_index, kind in enumerate(KINDS):
            for day, (before, after) in enumerate(zip(base[group][kind], current[group][kind])):
                previous = {lesson["number"]: lesson for lesson in before}
                updated = {lesson["number"]: lesson for lesson in after}
                for number in sorted(previous.keys() | updated.keys()):
                    old, new = previous.get(number, {}), updated.get(number)
                    if old == new:
                        continue
                    delta = None if new is None else {
                        key: value for key, value in new.items()
                        if key != "number" and old.get(key) != value
                    }
                    patches.append([group_index, kind_index, day, number, delta])
    return encode_state({**anchor, "patches": patches})


class ScheduleWidgetCards:
    def __init__(self, bot, storage, settings):
        self.transport = WidgetTransport(bot)
        self.storage = storage
        self.store = StateStore(settings.runtime_data_directory)
        self.zone = timezone(timedelta(hours=settings.utc_offset_hours))
        self.lock = asyncio.Lock()
        self._refreshed_revision: str | None = None

    def anchor(self, today: date) -> dict:
        monday, kind, _ = self.storage.week_source(today)
        return {"anchor": monday.isoformat(), "kind": kind.value}

    async def open(self, chat_id: int, *, thread_id: int | None = None) -> bool:
        async with self.lock:
            try:
                weekly = self.store.load().get("widget_weeks")
                if weekly is None:
                    return False
                anchor = self.anchor(datetime.now(self.zone).date())
                await self.send(chat_id, weekly, anchor, thread_id=thread_id)
            except (ValueError, OSError, TelegramError) as exc:
                LOGGER.warning("Schedule widget unavailable: %s", type(exc).__name__)
                return False
        return True

    async def send(self, chat_id, weekly, anchor, *, thread_id=None, silent=False):
        revision = digest({"weeks": weekly, "anchor": anchor})
        html = build_widget_html(
            weekly, anchor_monday=date.fromisoformat(anchor["anchor"]),
            anchor_kind=anchor["kind"], sent_on=datetime.now(self.zone).date(),
        )
        message_id = await self.transport.send(chat_id, html, thread_id=thread_id, silent=silent)
        self.storage.save_schedule_widget_card(chat_id, message_id, json.dumps(weekly), revision, thread_id)

    async def refresh(self, weekly: dict, today: date, active_chats) -> None:
        anchor = self.anchor(today)
        revision = digest({"weeks": weekly, "anchor": anchor})
        if revision == self._refreshed_revision:
            return
        for card in self.storage.schedule_widget_cards():
            chat_id = card["chat_id"]
            if chat_id <= 0 or chat_id not in active_chats() or card["revision"] == revision:
                continue
            async with self.lock:
                # A request can publish a current card while the monitor is
                # between chats. Re-read the row before editing its message.
                current = self.storage.schedule_widget_card(chat_id)
                if current is None or current["revision"] == revision or chat_id not in active_chats():
                    continue
                await self.refresh_card(current, weekly, revision, anchor, active_chats)
            await asyncio.sleep(0.1)
        self._refreshed_revision = revision

    async def refresh_card(self, card, weekly, revision, anchor, active_chats):
        chat_id, message_id, base = card["chat_id"], card["message_id"], card["base"]
        # Persist the attempt before calling Telegram. After an ambiguous
        # timeout or crash the request might already have taken effect.
        self.storage.save_schedule_widget_card(chat_id, message_id, base, revision, card["thread_id"])
        try:
            try:
                patch = public_patch(json.loads(base), weekly, anchor)
            except ValueError:
                patch = None
            if patch is not None:
                try:
                    await self.transport.edit(chat_id, message_id, patch)
                    return
                except EndPointNotFound:
                    pass  # A server can support sending before it supports editing.
                except BadRequest as exc:
                    if "not modified" in str(exc).lower():
                        return
            if chat_id in active_chats():
                await self.send(chat_id, weekly, anchor, thread_id=card["thread_id"], silent=True)
        except (TelegramError, ValueError) as exc:
            LOGGER.warning("Schedule widget refresh unavailable: %s", type(exc).__name__)
