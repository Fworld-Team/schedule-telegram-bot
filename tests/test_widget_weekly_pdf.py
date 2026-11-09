import unittest
from schedule_bot.schedule_pdf import Cell
from schedule_bot.widget_weekly_pdf import extract_verified_weekly_cells
from schedule_bot.widget_html import WidgetDataError


def c(x0, top, x1, bottom, value):
    return Cell(x0, top, x1, bottom, value)


def fixtures():
    cells=[c(100,0,200,10,"ДЭ 23-24"),c(200,0,300,10,"ДЭ 24-24")]
    days=("Понедельник","Вторник","Среда","Четверг","Пятница")
    for i,day in enumerate(days):
        y=10+i*40
        cells += [c(0,y,40,y+40,day),c(40,y,100,y+40,"09.00-10.35")]
        cells += [c(100,y,200,y+20,"лек.Верхняя группа 23 01-201"),
                  c(100,y+20,200,y+40,"пр.Нижняя группа 23 02-206")]
        cells += [c(200,y,300,y+40,"пр.Обе недели группа 24 01-310")]
    return [cells]


class WidgetWeekPDFTests(unittest.TestCase):
    def test_top_upper_bottom_lower_common_both(self):
        data=extract_verified_weekly_cells(fixtures())
        self.assertIn("Верхняя",data["ДЭ 23-24"]["upper"][0][0]["subject"])
        self.assertIn("Нижняя",data["ДЭ 23-24"]["lower"][0][0]["subject"])
        self.assertEqual(data["ДЭ 24-24"]["upper"][0],data["ДЭ 24-24"]["lower"][0])

    def test_ambiguous_three_way_slot_is_rejected(self):
        tables=fixtures()
        cells=tables[0]
        cells[4]=c(100,10,200,25,"A")
        cells.append(c(100,25,200,30,"B"))
        cells.append(c(100,30,200,50,"C"))
        with self.assertRaises(WidgetDataError):
            extract_verified_weekly_cells(tables)
