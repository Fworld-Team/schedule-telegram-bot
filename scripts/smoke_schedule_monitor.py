"""EXPLICIT manual network smoke, never imported/run by the offline test suite.

No bot token, active chats, production state or Telegram client is loaded.
Run from the repository root: python -m scripts.smoke_schedule_monitor
"""

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx

from schedule_bot.schedule_monitor import (
    diff_schedule, download, format_notifications, parse_in_worker,
)
from schedule_bot.schedule_pdf import GROUPS, MAX_PDF_BYTES, PAGE_URL, find_pdf_url


async def smoke(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), trust_env=False) as client:
        page, _ = await download(client, PAGE_URL, 2 * 1024 * 1024)
        (output / "source.html").write_bytes(page)
        url = find_pdf_url(page.decode("utf-8"))
        print(f"Discovered PDF: {url}")
        pdf, url = await download(client, url, MAX_PDF_BYTES)
        (output / "source.pdf").write_bytes(pdf)
    groups, _ = await parse_in_worker(pdf)
    (output / "extracted.json").write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PDF SHA256: {hashlib.sha256(pdf).hexdigest()}")
    for group, rows in groups.items():
        print(f"{group}: {len(rows)} slots, {sum(bool(r['lesson']) for r in rows)} nonempty")
        for row in rows[:4]:
            print(json.dumps(row, ensure_ascii=False))
    assert format_notifications(diff_schedule(groups, groups), url) == []
    print("NO CHANGES: 0 notifications (no Telegram client created)")
    example = deepcopy(groups)
    lesson = next(r for r in example[GROUPS[0]] if r["lesson"])
    lesson["lesson"] += " [демонстрационное изменение, НЕ данные сайта]"
    print("DRY-RUN: synthetic change, NOT SENT")
    print("\n\n".join(format_notifications(diff_schedule(groups, example), url)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="isolated output directory, not the production /data")
    asyncio.run(smoke(parser.parse_args().output))
