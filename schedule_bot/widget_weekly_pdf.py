"""Extract week variants from the same ruled PDF tables as the daily monitor."""

from __future__ import annotations

from collections import defaultdict
import re

from .schedule_pdf import Cell, DAYS, GROUPS, _group, day_name, normalize, time_slot
from .widget_html import WidgetDataError, validate_week_data

_STANDARDS = (
    "09:00-10:35", "10:55-12:30", "13:00-14:35", "14:55-16:30",
    "16:50-18:25", "18:30-20:05", "20:15-21:50",
)
_ROOM = re.compile(r"(?<!\w)(?:\d{2}-[A-Za-zА-Яа-яЁё0-9-]+|СК)(?!\w)")
_TOL = 1.0


def _lesson(number: int, time: str, raw: str) -> dict | None:
    text = normalize(raw)
    if not text or text in {"-", "—", "–"}:
        return None
    kind = ""
    for prefix, name in (("лек.", "Лекция"), ("пр.", "Практика"), ("лаб.", "Лабораторная")):
        if text.casefold().startswith(prefix):
            kind = name
            text = text[len(prefix):].strip()
            break
    found = list(_ROOM.finditer(text))
    room = ""
    if found and found[-1].end() == len(text) and len(found) == 1:
        room = found[-1].group(0)
        text = text[:found[-1].start()].strip()
    return {"number": number, "time": time, "subject": text,
            "type": kind, "room": room}


def extract_verified_weekly_cells(tables: list[list[Cell]]) -> dict:
    """Extract six weekdays for both groups from geometry-preserving PDF cells.

    This is deliberately strict: a third stacked lesson, an unfilled vertical
    span, horizontal subdivisions, or missing weekday is ambiguous.
    """
    output = {group: {"upper": [[] for _ in range(6)],
                      "lower": [[] for _ in range(6)]} for group in GROUPS}
    processed = defaultdict(set)
    for cells in tables:
        for group in GROUPS:
            headers = [c for c in cells if _group(c.text) == group]
            for header in headers:
                slots = sorted((c for c in cells
                                if c.x1 <= header.x0 + _TOL and time_slot(c.text)
                                and c.top >= header.bottom - _TOL),
                               key=lambda c: c.top)
                current_day = None
                for slot in slots:
                    candidate_days = {day_name(c.text) for c in cells
                                      if c.x1 <= slot.x0 + _TOL
                                      and c.top <= slot.top + _TOL
                                      and c.bottom > slot.top + _TOL
                                      and day_name(c.text)}
                    if len(candidate_days) > 1:
                        raise WidgetDataError("Ambiguous PDF weekday")
                    current_day = next(iter(candidate_days), current_day)
                    if current_day is None:
                        raise WidgetDataError("Unlabelled PDF weekday")
                    if current_day not in DAYS[:6]:
                        raise WidgetDataError("Unsupported Sunday schedule")
                    day = DAYS.index(current_day)
                    time = time_slot(slot.text)
                    if time not in _STANDARDS:
                        raise WidgetDataError("Non-standard time; number needs verification")
                    number = _STANDARDS.index(time) + 1
                    if (day, time) in processed[group]:
                        raise WidgetDataError("Duplicate slot for " + group)
                    processed[group].add((day, time))
                    selected = sorted((c for c in cells
                                       if c.top >= slot.top - _TOL
                                       and c.bottom <= slot.bottom + _TOL
                                       and c.x0 < header.x1 - _TOL
                                       and c.x1 > header.x0 + _TOL),
                                      key=lambda c: (c.top, c.x0))
                    if len(selected) not in (1, 2):
                        raise WidgetDataError("Ambiguous stacked PDF cells")
                    if any(abs(c.x0 - header.x0) > _TOL
                           or abs(c.x1 - header.x1) > _TOL for c in selected):
                        raise WidgetDataError("Horizontal subgroup split is ambiguous")
                    if abs(selected[0].top - slot.top) > _TOL or abs(selected[-1].bottom - slot.bottom) > _TOL:
                        raise WidgetDataError("Incomplete PDF cell height")
                    if len(selected) == 2 and abs(selected[0].bottom - selected[1].top) > _TOL:
                        raise WidgetDataError("Stacked PDF cells leave an unproven gap")
                    if len(selected) == 2:
                        midpoint = (slot.top + slot.bottom) / 2
                        # The university draws upper/lower variants in half-cells.
                        if abs(selected[0].bottom - midpoint) > max(_TOL, (slot.bottom - slot.top) * .08):
                            raise WidgetDataError("Split does not match the upper/lower layout")
                    variants = (selected[0].text, selected[0].text) if len(selected) == 1 else (
                        selected[0].text, selected[1].text)
                    for kind, raw in zip(("upper", "lower"), variants):
                        lesson = _lesson(number, time, raw)
                        if lesson is not None:
                            output[group][kind][day].append(lesson)
    for group in GROUPS:
        if not set(range(5)).issubset({day for day, _ in processed[group]}):
            raise WidgetDataError("PDF does not cover Mon-Fri for " + group)
    return validate_week_data(output)
