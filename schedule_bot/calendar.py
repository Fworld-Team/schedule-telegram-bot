"""Academic-week and weekday calculations independent from Telegram."""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum


class WeekKind(str, Enum):
    """The two alternating academic week variants."""

    UPPER = "upper"
    LOWER = "lower"


WEEKDAY_NAMES_RU: tuple[str, ...] = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)


def academic_year_anchor(
    target: date,
    start_month: int = 9,
    start_day: int = 1,
) -> date:
    """Return Monday of the most recent first academic week.

    The calendar week containing the configured September start is the first
    upper/odd week.  Anchoring on Monday prevents the variant from changing in
    the middle of a teaching week when 1 September is not a Monday.
    """

    configured_start = date(target.year, start_month, start_day)
    candidate = configured_start - timedelta(days=configured_start.weekday())
    if target < candidate:
        configured_start = date(target.year - 1, start_month, start_day)
        candidate = configured_start - timedelta(days=configured_start.weekday())
    return candidate


def academic_week_number(
    target: date,
    start_month: int = 9,
    start_day: int = 1,
) -> int:
    """Return a one-based academic week number."""

    anchor = academic_year_anchor(target, start_month, start_day)
    return ((target - anchor).days // 7) + 1


def academic_week_kind(
    target: date,
    start_month: int = 9,
    start_day: int = 1,
) -> WeekKind:
    """Return upper for odd academic weeks and lower for even weeks."""

    number = academic_week_number(target, start_month, start_day)
    return WeekKind.UPPER if number % 2 else WeekKind.LOWER


def date_in_current_week(today: date, weekday: int) -> date:
    """Return the date of ``weekday`` (Monday=0) in today's calendar week."""

    if not 0 <= weekday <= 6:
        raise ValueError("weekday must be between 0 (Monday) and 6 (Sunday)")
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(days=weekday)


def week_monday(target: date) -> date:
    """Return Monday of the calendar week containing target."""

    return target - timedelta(days=target.weekday())


def alternating_week_kind(
    target: date,
    anchor_monday: date,
    anchor_kind: WeekKind,
) -> WeekKind:
    """Calculate a week kind from an administrator-controlled anchor."""

    anchor = week_monday(anchor_monday)
    delta_weeks = (week_monday(target) - anchor).days // 7
    if delta_weeks % 2 == 0:
        return anchor_kind
    return WeekKind.LOWER if anchor_kind is WeekKind.UPPER else WeekKind.UPPER
