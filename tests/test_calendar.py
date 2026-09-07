from datetime import date
import unittest

from schedule_bot.calendar import (
    WeekKind,
    academic_week_kind,
    academic_week_number,
    alternating_week_kind,
    date_in_current_week,
    week_monday,
)


class AcademicCalendarTests(unittest.TestCase):
    def test_calendar_week_containing_september_first_is_upper(self) -> None:
        self.assertEqual(academic_week_number(date(2026, 8, 31)), 1)
        self.assertEqual(academic_week_kind(date(2026, 8, 31)), WeekKind.UPPER)
        self.assertEqual(academic_week_number(date(2026, 9, 1)), 1)
        self.assertEqual(academic_week_kind(date(2026, 9, 1)), WeekKind.UPPER)
        self.assertEqual(academic_week_kind(date(2026, 9, 6)), WeekKind.UPPER)

    def test_week_switches_on_monday(self) -> None:
        self.assertEqual(academic_week_number(date(2026, 9, 7)), 2)
        self.assertEqual(academic_week_kind(date(2026, 9, 7)), WeekKind.LOWER)
        self.assertEqual(academic_week_kind(date(2026, 9, 13)), WeekKind.LOWER)
        self.assertEqual(academic_week_kind(date(2026, 9, 14)), WeekKind.UPPER)

    def test_all_days_in_calendar_week_have_same_kind(self) -> None:
        kinds = {
            academic_week_kind(date(2026, 9, day))
            for day in range(7, 14)
        }
        self.assertEqual(kinds, {WeekKind.LOWER})

    def test_january_uses_previous_september(self) -> None:
        self.assertEqual(academic_week_number(date(2027, 1, 1)), 18)
        self.assertEqual(academic_week_kind(date(2027, 1, 1)), WeekKind.LOWER)

    def test_current_week_date(self) -> None:
        today = date(2026, 9, 3)  # Thursday
        self.assertEqual(date_in_current_week(today, 0), date(2026, 8, 31))
        self.assertEqual(date_in_current_week(today, 4), date(2026, 9, 4))

    def test_invalid_weekday_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            date_in_current_week(date(2026, 9, 3), 7)

    def test_admin_anchor_continues_across_new_year(self) -> None:
        anchor = date(2026, 12, 28)
        self.assertEqual(week_monday(date(2027, 1, 1)), anchor)
        self.assertEqual(
            alternating_week_kind(date(2027, 1, 8), anchor, WeekKind.LOWER),
            WeekKind.UPPER,
        )


if __name__ == "__main__":
    unittest.main()
