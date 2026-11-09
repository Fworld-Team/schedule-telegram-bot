"""Strict, machine-readable Timacad table extraction; no Telegram or OCR."""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
import re
import sys
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import pdfplumber


PAGE_URL = "https://www.timacad.ru/about/sveden/document/rezhim-zaniatii-obuchaiushchikhsia"
INSTITUTE = "Институт экономики и управления АПК"
GROUPS = ("ДЭ 23-24", "ДЭ 24-24")
DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
MAX_PDF_BYTES = 20 * 1024 * 1024
TIME = re.compile(r"(?<!\d)(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})(?!\d)")
DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−﹘﹣－"})
Schedule = dict[str, list[dict[str, str]]]


class ScheduleParseError(ValueError):
    """The source does not prove a complete, unambiguous target schedule."""


def normalize(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(DASHES)
    text = re.sub("\u00ad\\s*", "", text).replace("\u200b", "")
    text = re.sub(r"(?<=\d)\s*-\s*(?=[\dА-Яа-я])", "-", text)
    text = re.sub(r"\b(лек|пр|лаб)\.\s*", r"\1. ", text)
    text = re.sub(r"\b([А-ЯЁ])\.\s+([А-ЯЁ])\.", r"\1.\2.", text)
    return re.sub(r"\s+", " ", text).strip()


def day_name(text: str | None) -> str | None:
    compact = re.sub(r"\s+", "", normalize(text)).casefold()
    for day in DAYS:
        if compact in {day.casefold(), day.casefold()[::-1]}:
            return day
    return None


def time_slot(text: str | None) -> str | None:
    cleaned = normalize(text)
    match = TIME.fullmatch(cleaned)
    if not match:
        return None
    h1, m1, h2, m2 = map(int, match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59 or (h1, m1) >= (h2, m2):
        raise ScheduleParseError(f"Invalid time slot: {cleaned}")
    return f"{h1:02}:{m1:02}-{h2:02}:{m2:02}"


def safe_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.timacad.ru", "timacad.ru"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ScheduleParseError("Source URL must stay on HTTPS Timacad")
    return url


def find_pdf_url(html: str) -> str:
    """Select the course link inside its institute, never by a dated filename."""
    soup = BeautifulSoup(html, "html.parser")
    labels = soup.find_all(string=lambda s: s and normalize(s) == INSTITUTE)
    candidates: set[str] = set()
    for label in labels:
        # Both accordion bodies and a heading followed by a course list occur
        # on the site. Ascend only as far as the nearest institute container.
        for container in label.parents:
            links = container.find_all("a", href=True)
            matches = [a for a in links if any(
                re.fullmatch(r"3\s*курс", normalize(value), re.I)
                for value in (a.get_text(" "), a.get("title", "")))]
            if matches:
                # An ancestor containing several institutes is not a valid scope.
                titles = {normalize(s) for s in container.stripped_strings
                          if normalize(s).startswith("Институт ")}
                if titles - {INSTITUTE}:
                    break
                for link in matches:
                    url = safe_url(urljoin(PAGE_URL, link["href"]))
                    if urlsplit(url).path.lower().endswith(".pdf"):
                        candidates.add(url)
                break
    if len(candidates) != 1:
        raise ScheduleParseError(f"Expected one institute / 3 курс PDF, found {len(candidates)}")
    return candidates.pop()


@dataclass(frozen=True)
class Cell:
    x0: float
    top: float
    x1: float
    bottom: float
    text: str


def _group(text: str) -> str | None:
    compact = re.sub(r"\s+", "", normalize(text))
    return next((g for g in GROUPS if compact == g.replace(" ", "")), None)


def parse_cells(tables: list[list[Cell]]) -> Schedule:
    """Use cell geometry, including vertical/horizontal merged cells.

    Empty slots are retained: a missing cell is an error, not a deleted lesson.
    Multiple subrows within one time slot retain their order and boundaries.
    """
    result: Schedule = {g: [] for g in GROUPS}
    for cells in tables:
        headers = {g: [c for c in cells if _group(c.text) == g] for g in GROUPS}
        if not any(headers.values()):
            continue
        for group, group_headers in headers.items():
            for header in group_headers:
                times = sorted((c for c in cells if c.x1 <= header.x0 + 1 and time_slot(c.text)),
                               key=lambda c: (c.top, c.x0))
                times = [c for c in times if c.top >= header.bottom - 1]
                body = [c for c in cells if c.top >= header.bottom - 1
                        and c.x0 < header.x1 - 1 and c.x1 > header.x0 + 1]
                if not body or not times:
                    raise ScheduleParseError("Missing schedule body or time column")
                cursor = header.bottom
                for slot in times:
                    if abs(slot.top - cursor) > 1:
                        raise ScheduleParseError("Missing or overlapping time slot")
                    cursor = slot.bottom
                if abs(cursor - max(c.bottom for c in body)) > 1:
                    raise ScheduleParseError("Incomplete time column")
                current_day = None
                for slot in times:
                    if slot.top < header.bottom - 1:
                        continue
                    # Merged weekday labels can be centered far below this row.
                    days = {day_name(c.text) for c in cells
                            if c.x1 <= slot.x0 + 1 and c.top <= slot.top + 1
                            and c.bottom > slot.top + 1 and day_name(c.text)}
                    if len(days) > 1:
                        raise ScheduleParseError("Ambiguous weekday")
                    current_day = next(iter(days), current_day)
                    if current_day is None:
                        raise ScheduleParseError("Time slot without weekday")
                    selected = sorted((c for c in cells
                                       if c.top >= slot.top - 1 and c.bottom <= slot.bottom + 1
                                       and c.x0 < header.x1 - 1 and c.x1 > header.x0 + 1),
                                      key=lambda c: (c.top, c.x0))
                    if not selected:
                        raise ScheduleParseError(f"Missing cell for {group} / {current_day}")
                    # Do not silently accept holes in the extracted column.
                    boundaries = sorted({slot.top, slot.bottom, *[c.top for c in selected],
                                         *[c.bottom for c in selected]})
                    for top, bottom in zip(boundaries, boundaries[1:]):
                        if bottom - top < 1:
                            continue
                        row = [c for c in selected if c.top <= top + 1 and c.bottom >= bottom - 1]
                        cursor = header.x0
                        for cell in sorted(row, key=lambda c: c.x0):
                            if cell.x0 > cursor + 1:
                                raise ScheduleParseError("Incomplete group column")
                            cursor = max(cursor, cell.x1)
                        if cursor < header.x1 - 1:
                            raise ScheduleParseError("Incomplete group column")
                    # Preserve subrow/subgroup boundaries; never infer parity.
                    parts = [normalize(c.text) for c in selected]
                    # Splitting an identical lesson into two ruled subrows
                    # must not look like a schedule change (or vice versa).
                    lesson = parts[0] if len(set(parts)) == 1 else " | ".join(parts)
                    result[group].append({"day": current_day, "time": time_slot(slot.text),
                                          "lesson": lesson})
    return validate_schedule(result)


def validate_schedule(groups: object) -> Schedule:
    """Reject incomplete or ambiguous data before comparison or state loading."""
    if not isinstance(groups, dict) or set(groups) != set(GROUPS):
        raise ScheduleParseError("Both target groups are required")
    cleaned: Schedule = {}
    for group in GROUPS:
        rows = groups[group]
        if not isinstance(rows, list) or not rows:
            raise ScheduleParseError(f"Missing group: {group}")
        indexed = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"day", "time", "lesson"}:
                raise ScheduleParseError("Invalid lesson structure")
            if not all(isinstance(v, str) for v in row.values()):
                raise ScheduleParseError("Invalid lesson fields")
            day = day_name(row["day"])
            time = time_slot(row["time"])
            if not day or not time or len(row["lesson"]) > 12000:
                raise ScheduleParseError("Invalid day, time or lesson")
            key = (day, time)
            if key in indexed:
                raise ScheduleParseError(f"Duplicate slot: {group} / {key}")
            indexed[key] = {"day": day, "time": time, "lesson": normalize(row["lesson"])}
        # A weekly university table must account for at least Mon-Fri, even
        # if some slots are empty. Fail closed on partial page extraction.
        if not set(DAYS[:5]).issubset({day for day, _ in indexed}):
            raise ScheduleParseError(f"Incomplete week: {group}")
        if not any(r["lesson"] for r in indexed.values()):
            raise ScheduleParseError(f"Empty/unreadable group: {group}")
        cleaned[group] = sorted(indexed.values(), key=lambda r: (DAYS.index(r["day"]), r["time"]))
    return cleaned


def _extract_tables(content: bytes) -> list[list[Cell]]:
    if not content.startswith(b"%PDF-") or len(content) > MAX_PDF_BYTES:
        raise ScheduleParseError("Not a PDF or PDF too large")
    tables = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if not 1 <= len(pdf.pages) <= 40:
                raise ScheduleParseError("Unexpected PDF page count")
            for page in pdf.pages:
                for table in page.find_tables():
                    extracted = table.extract(x_tolerance=2, y_tolerance=3)
                    cells = [Cell(*box, text or "")
                             for row, texts in zip(table.rows, extracted)
                             for box, text in zip(row.cells, texts) if box is not None]
                    tables.append(cells)
                page.close()
        return tables
    except ScheduleParseError:
        raise
    except Exception as exc:
        raise ScheduleParseError("Cannot parse PDF") from exc


def parse_pdf(content: bytes) -> Schedule:
    return parse_cells(_extract_tables(content))


def parse_snapshot(content: bytes) -> tuple[Schedule, dict | None]:
    from .widget_html import WidgetDataError
    from .widget_weekly_pdf import extract_verified_weekly_cells

    tables = _extract_tables(content)
    groups = parse_cells(tables)
    try:
        weekly = extract_verified_weekly_cells(tables)
    except WidgetDataError:
        # Preserve existing change detection if the widget cannot prove parity.
        weekly = None
    return groups, weekly


def main() -> int:
    """Bounded subprocess worker; only communicates schedule JSON via stdio."""
    try:
        if sys.platform == "linux":
            import resource
            # Production has 512 MiB for the whole container. Leave headroom
            # for polling/SQLite, and fail closed on hostile or oversized PDFs.
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
            resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
        groups, weekly = parse_snapshot(sys.stdin.buffer.read(MAX_PDF_BYTES + 1))
        print(json.dumps({"groups": groups, "widget_weeks": weekly}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
