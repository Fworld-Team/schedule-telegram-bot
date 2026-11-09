"""Self-contained Telegram message-widget renderer (Bot API 10.4 profile 1).

This module intentionally accepts *verified* upper/lower timetable data.  The
existing change-monitor parser returns flattened text only, which must not be
silently assigned to one of the two academic-week variants.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import zlib

from .schedule_pdf import GROUPS
KINDS = ("upper", "lower")
MAX_SOURCE_BYTES = 32768
MAX_ZLIB_BYTES = 4096
_TIME = re.compile(r"^\d{2}:\d{2}[-–]\d{2}:\d{2}$")
_TEMPLATE = Path(__file__).with_name("schedule_widget.html")


class WidgetDataError(ValueError):
    """A widget would have incomplete/ambiguous timetable data."""


def validate_week_data(data: object) -> dict:
    """Require two complete group/variant matrices, preserving empty days."""

    if not isinstance(data, dict) or set(data) != set(GROUPS):
        raise WidgetDataError("Expected exactly both target groups")
    clean = {}
    for group in GROUPS:
        variants = data[group]
        if not isinstance(variants, dict) or set(variants) != set(KINDS):
            raise WidgetDataError("Each group needs upper and lower weeks")
        clean[group] = {}
        for kind in KINDS:
            days = variants[kind]
            if not isinstance(days, list) or len(days) != 6:
                raise WidgetDataError("Each week needs six day arrays")
            normalized = []
            for lessons in days:
                if not isinstance(lessons, list) or len(lessons) > 8:
                    raise WidgetDataError("Invalid number of daily lessons")
                numbers = set()
                items = []
                for lesson in lessons:
                    if not isinstance(lesson, dict) or set(lesson) != {
                        "number", "time", "subject", "type", "room"
                    }:
                        raise WidgetDataError("Invalid lesson structure")
                    number = lesson["number"]
                    if type(number) is not int or not 1 <= number <= 12 or number in numbers:
                        raise WidgetDataError("Invalid or duplicate lesson number")
                    numbers.add(number)
                    if not isinstance(lesson["time"], str) or not _TIME.fullmatch(lesson["time"]):
                        raise WidgetDataError("Invalid time")
                    if any(not isinstance(lesson[k], str) or len(lesson[k]) > limit
                           for k, limit in (("subject", 220), ("type", 50), ("room", 90))):
                        raise WidgetDataError("Invalid lesson text")
                    items.append(lesson.copy())
                normalized.append(sorted(items, key=lambda item: item["number"]))
            clean[group][kind] = normalized
    return clean


def build_widget_html(data: object, *, anchor_monday: date, anchor_kind: str) -> str:
    """Build HTML with locally selectable groups/days and a shared week anchor.

    The anchor should come from BotStorage's manually editable week correction;
    do not derive upper/lower from the ordinal academic-week number alone.
    """
    if anchor_monday.weekday() != 0 or anchor_kind not in KINDS:
        raise WidgetDataError("Expected a Monday anchor and upper/lower kind")
    payload = {"anchor": anchor_monday.isoformat(), "kind": anchor_kind,
               "groups": validate_week_data(data)}
    source = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # Never let a subject such as '</script>' break out of its JS script tag.
    source = source.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = _TEMPLATE.read_text(encoding="utf-8").replace("__WIDGET_DATA__", source)
    raw = html.encode("utf-8")
    if len(raw) > MAX_SOURCE_BYTES or len(zlib.compress(raw, 9)) > MAX_ZLIB_BYTES:
        raise WidgetDataError("Widget exceeds Telegram 32KB / 4KB zlib limits")
    return html
