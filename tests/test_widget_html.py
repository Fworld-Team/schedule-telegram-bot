"""Tests for standalone HTML widget, no Telegram token or VPS required."""

from datetime import date
from pathlib import Path
import shutil
import subprocess
import unittest
import zlib

from schedule_bot.widget_html import (
    build_widget_html, validate_week_data, WidgetDataError,
    MAX_SOURCE_BYTES, MAX_ZLIB_BYTES, GROUPS,
)


def sample():
    base = {"number": 1, "time": "09:00-10:35", "subject": "Базы данных",
            "type": "Лекция", "room": "01-201"}
    return {group: {kind: [[base.copy()], [], [], [], [], []]
                    for kind in ("upper", "lower")} for group in GROUPS}


class WidgetHTMLTests(unittest.TestCase):
    def test_widget_is_valid_self_contained_document_with_small_payload(self):
        html = build_widget_html(sample(), anchor_monday=date(2026, 11, 9), anchor_kind="upper")
        self.assertIn("ДЭ 23-24", html)
        self.assertIn("ДЭ 24-24", html)
        self.assertIn("localStorage", html)
        self.assertIn("publicDataChanged", html)
        self.assertNotIn("__WIDGET_DATA__", html)
        self.assertNotIn("fetch(", html)
        self.assertLessEqual(len(html.encode()), MAX_SOURCE_BYTES)
        self.assertLessEqual(len(zlib.compress(html.encode(), 9)), MAX_ZLIB_BYTES)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the widget runtime test")
    def test_offline_navigation_isolation_rollover_and_live_state(self):
        html = build_widget_html(
            sample(), anchor_monday=date(2026, 11, 9), anchor_kind="upper", sent_on=date(2026, 11, 10),
        )
        subprocess.run(["node", str(Path(__file__).with_name("widget_runtime.cjs"))],
                       input=html, text=True, check=True, timeout=10)

    def test_invalid_input_rejected_before_rendering(self):
        with self.assertRaises(WidgetDataError):
            validate_week_data({"ДЭ 23-24": {}})
        data = sample()
        del data["ДЭ 24-24"]["lower"]
        with self.assertRaises(WidgetDataError):
            build_widget_html(data, anchor_monday=date(2026,11,9),anchor_kind="upper")

    def test_anchor_requires_monday(self):
        with self.assertRaises(WidgetDataError):
            build_widget_html(sample(), anchor_monday=date(2026, 11, 10), anchor_kind="upper")

    def test_subject_is_encoded_without_html_or_script_injection(self):
        data = sample()
        data["ДЭ 23-24"]["upper"][0][0]["subject"] = "</script><img src=x onerror=alert(1)>"
        html = build_widget_html(data, anchor_monday=date(2026,11,9),anchor_kind="upper")
        self.assertNotIn("</script><img", html)
        self.assertIn("\\u003c/script\\u003e", html)

    def test_oversize_payload_rejected_not_silently_cut(self):
        data=sample()
        import random,string
        rng=random.Random(17)
        for g in GROUPS:
            for k in ("upper", "lower"):
                for d in range(6):
                    data[g][k][d]=[{"number":n,"time":"09:00-10:35",
                                      "subject":"".join(rng.choice(string.ascii_letters) for _ in range(210)),
                                      "type":"Практика","room":"01-201"} for n in range(1,9)]
        with self.assertRaises(WidgetDataError):
            build_widget_html(data, anchor_monday=date(2026,11,9),anchor_kind="upper")


if __name__ == "__main__":
    unittest.main()
