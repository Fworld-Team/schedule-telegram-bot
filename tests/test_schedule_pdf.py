from dataclasses import replace
from pathlib import Path
import unittest
import pdfplumber

from schedule_bot.schedule_pdf import (
    Cell, DAYS, GROUPS, ScheduleParseError, day_name, find_pdf_url,
    normalize, parse_cells, parse_pdf, time_slot,
)


HTML = '''<div><h3>Институт агробиотехнологии</h3>
<a href="/wrong.pdf">3 курс</a></div>
<div><h3>Институт экономики и управления АПК</h3>
<a href="/first.pdf">1 курс</a><a href="/uploads/current.pdf">3 курс</a></div>'''
PDF_URL = "https://www.timacad.ru/uploads/current.pdf"


def sample_groups():
    return {g: [{"day": day, "time": time, "lesson": lesson}
                for day in DAYS[:5]
                for time, lesson in [("09:00-10:35", "лек. Предмет ИВАНОВ А.А. 01-216"),
                                     ("10:55-12:30", "")]] for g in GROUPS}


def sample_cells():
    cells = [Cell(50, 0, 150, 10, GROUPS[0]), Cell(150, 0, 250, 10, GROUPS[1])]
    for i, day in enumerate(DAYS[:5]):
        top = 10 + i * 40
        cells.append(Cell(0, top, 10, top + 40, " ".join(day.upper())))
        for offset, time in [(0, "9.00 – 10.35"), (20, "10:55-12:30")]:
            y = top + offset
            cells.append(Cell(10, y, 50, y + 20, time))
            for x in (50, 150):
                cells.append(Cell(x, y, x + 100, y + 20,
                                  "лек. Предмет ИВАНОВ А.А. 01-216" if not offset else ""))
    return cells


class DiscoveryTests(unittest.TestCase):
    def test_real_html_fixture_title_not_visible_link_text(self):
        html = (Path(__file__).parent / "fixtures/timacad/source.html").read_text()
        self.assertEqual(find_pdf_url(html),
                         "https://www.timacad.ru/uploads/files/20260924/1790239252_ieiu-3.pdf")

    def test_course_in_title_attribute(self):
        self.assertEqual(find_pdf_url(HTML.replace('>3 курс</a>', ' title="3 курс">Скачать</a>')), PDF_URL)

    def test_selects_institute_and_third_course(self):
        self.assertEqual(find_pdf_url(HTML), PDF_URL)

    def test_new_pdf_url_is_discovered(self):
        self.assertEqual(find_pdf_url(HTML.replace("current.pdf", "new.pdf")),
                         PDF_URL.replace("current.pdf", "new.pdf"))

    def test_nested_link_text_and_spaces(self):
        html = HTML.replace('>3 курс</a>', '><b>3</b> курс</a>')
        self.assertEqual(find_pdf_url(html), PDF_URL)

    def test_missing_ambiguous_or_wrong_institute_fails_closed(self):
        for html in ("<html>error</html>", HTML.replace("экономики", "права"),
                     HTML.replace("current.pdf", "current.txt"),
                     HTML.replace('</div>', '<a href="/another.pdf">3 курс</a></div>')):
            with self.subTest(html=html), self.assertRaises(ScheduleParseError):
                find_pdf_url(html)

    def test_external_pdf_is_not_fetched(self):
        with self.assertRaises(ScheduleParseError):
            find_pdf_url(HTML.replace("/uploads/current.pdf", "https://other.example/file.pdf"))


class TableTests(unittest.TestCase):
    def test_both_groups_and_times(self):
        self.assertEqual(parse_cells([sample_cells()]), sample_groups())

    def test_all_spaced_vertical_days(self):
        for day in DAYS:
            for text in ("\n".join(day.upper()), " ".join(day), day[::-1]):
                self.assertEqual(day_name(text), day)

    def test_merged_weekday_and_empty_cell_inherit_current_day(self):
        cells = sample_cells()
        cells[2] = replace(cells[2], bottom=30)
        cells.append(Cell(0, 30, 10, 50, ""))
        result = parse_cells([cells])
        self.assertEqual(result[GROUPS[0]][1]["day"], "Понедельник")
        self.assertEqual(result[GROUPS[0]][1]["lesson"], "")

    def test_horizontal_merged_lesson_belongs_to_both_groups(self):
        cells = sample_cells()
        cells = [c for c in cells if not (c.top == 10 and c.x0 >= 50)]
        cells.append(Cell(50, 10, 250, 30, "Общая лекция"))
        parsed = parse_cells([cells])
        for group in GROUPS:
            self.assertEqual(parsed[group][0]["lesson"], "Общая лекция")

    def test_subrows_are_preserved_in_order(self):
        cells = sample_cells()
        cells = [c for c in cells if not (c.top == 10 and c.x0 == 50)]
        cells.extend([Cell(50, 10, 150, 20, "Верхняя"), Cell(50, 20, 150, 30, "Нижняя")])
        self.assertEqual(parse_cells([cells])[GROUPS[0]][0]["lesson"], "Верхняя | Нижняя")

    def test_same_lesson_split_into_identical_subrows_is_cosmetic(self):
        cells = sample_cells()
        text = cells[4].text
        cells = [c for c in cells if not (c.top == 10 and c.x0 == 50)]
        cells.extend([Cell(50, 10, 150, 20, text), Cell(50, 20, 150, 30, text)])
        self.assertEqual(parse_cells([cells]), sample_groups())

    def test_missing_one_or_both_groups(self):
        for targets in ({GROUPS[0]}, set(GROUPS)):
            cells = [c for c in sample_cells() if c.text not in targets]
            with self.assertRaises(ScheduleParseError):
                parse_cells([cells])

    def test_missing_day_time_or_lesson_cell_rejected(self):
        for index in (2, 4):
            cells = sample_cells()
            del cells[index]
            with self.assertRaises(ScheduleParseError):
                parse_cells([cells])
        cells = [replace(c, text="") if time_slot(c.text) else c for c in sample_cells()]
        with self.assertRaises(ScheduleParseError):
            parse_cells([cells])

    def test_partial_week_is_not_a_mass_deletion(self):
        with self.assertRaises(ScheduleParseError):
            parse_cells([[c for c in sample_cells() if c.top < 170]])

    def test_missing_single_time_cannot_masquerade_as_deleted_lesson(self):
        cells = [replace(c, text="unknown") if c.top == 30 and c.x0 == 10 else c
                 for c in sample_cells()]
        with self.assertRaises(ScheduleParseError):
            parse_cells([cells])

    def test_final_time_slot_missing_fails_closed(self):
        cells = [replace(c, text="") if c.top == 190 and c.x0 == 10 else c
                 for c in sample_cells()]
        with self.assertRaises(ScheduleParseError):
            parse_cells([cells])

    def test_duplicate_slots_rejected(self):
        with self.assertRaises(ScheduleParseError):
            parse_cells([sample_cells(), sample_cells()])

    def test_time_normalization(self):
        self.assertEqual(time_slot("9.00\n–10.35"), "09:00-10:35")
        for value in ("25:00-26:00", "10:00-09:00", "09:80-10:35"):
            with self.assertRaises(ScheduleParseError):
                time_slot(value)

    def test_cosmetics_normalized_real_room_preserved(self):
        self.assertEqual(normalize("лек.\nПредмет\u00a0\u00a0ИВАНОВ А.А. 01–216\u00ad"),
                         "лек. Предмет ИВАНОВ А.А. 01-216")
        self.assertNotEqual(normalize("01-203"), normalize("03-101"))
        self.assertEqual(normalize("лек.Инфо\u00ad\nкоммуникации 01 - 216"),
                         normalize("лек. Инфокоммуникации 01-216"))
        self.assertEqual(normalize("ВЕТОШКИН А. Ю."), "ВЕТОШКИН А.Ю.")

    def test_corrupt_pdf_and_html(self):
        for content in (b"<html>error</html>", b"%PDF-1.7\nbroken"):
            with self.assertRaises(ScheduleParseError):
                parse_pdf(content)


class RealPDFFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = (Path(__file__).parent / "fixtures/timacad/schedule.pdf").read_bytes()
        cls.groups = parse_pdf(cls.pdf)

    def test_target_groups_all_days_and_times(self):
        for group, nonempty in zip(GROUPS, (20, 17)):
            rows = self.groups[group]
            self.assertEqual(len(rows), 25)
            self.assertEqual(sum(bool(r["lesson"]) for r in rows), nonempty)
            self.assertEqual({r["day"] for r in rows}, set(DAYS[:5]))
            self.assertEqual({r["time"] for r in rows},
                             {"09:00-10:35", "10:55-12:30", "13:00-14:35", "14:55-16:30", "16:50-18:25"})

    def test_known_lessons_match_visual_smoke(self):
        self.assertEqual(self.groups[GROUPS[0]][0], {
            "day": "Понедельник", "time": "09:00-10:35",
            "lesson": "лек. Инфокоммуникационные системы и сети ВЕТОШКИН А.Ю. 09-ХД"})
        self.assertEqual(self.groups[GROUPS[1]][22]["lesson"],
                         "пр. Моделирование информационных систем БОДУР А.М. 01-214")

    def test_empty_upper_subrow_retained(self):
        self.assertEqual(self.groups[GROUPS[1]][2]["lesson"],
                         "| пр. Программная инженерия ХРАМОВ Д.Э. 02-106")

    def test_pdf_bytes_can_change_without_target_change(self):
        self.assertEqual(parse_pdf(self.pdf + b"\n% harmless metadata comment\n"), self.groups)

    def test_other_groups_are_ignored(self):
        tables = []
        with pdfplumber.open(Path(__file__).parent / "fixtures/timacad/schedule.pdf") as pdf:
            for page in pdf.pages:
                for table in page.find_tables():
                    texts = table.extract(x_tolerance=2, y_tolerance=3)
                    tables.append([Cell(*box, text or "") for row, values in zip(table.rows, texts)
                                   for box, text in zip(row.cells, values) if box is not None])
        changed = [[replace(c, text="ДРУГОЙ КАБИНЕТ 03-101")
                    if c.x0 >= 82.8 and c.x1 <= 261.66 and c.top > 155 else c
                    for c in table] for table in tables]
        self.assertEqual(parse_cells(changed), self.groups)
