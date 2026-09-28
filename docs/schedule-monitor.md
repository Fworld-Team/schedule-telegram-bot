# Schedule monitor: operation and guarantees

## Integration and deployment

The existing bot, SQLite `active_chats`, handlers, admin conversations, media
storage, sequential update processing and polling watchdog are retained.
`bot.py` only starts the monitor and registers its cancellation callbacks.
The monitor owns a separate asyncio task; `post_stop` / `post_shutdown` cancel
it. It has no access to the watchdog's counters or intervals. PDF parsing runs
in a cancellable subprocess with a 90-second deadline; on Linux that process
also has a 256-MiB address-space / 60-CPU-second limit. HTTP I/O is asynchronous.

No new volume, port, service, environment variable or recipient list is needed.
The existing `BOT_DATA_DIR=/data` and production external volume are used.
The HTTP client is HTTPX, already used by python-telegram-bot; its existing
version is now an explicit dependency. New parser dependencies are pdfplumber
and beautifulsoup4. HTTP uses the existing `BOT_PROXY_URL` when configured,
without changing either Telegram client's proxy.

**One bot process/container per data volume**, as before. Do not run production
and a testing instance with the same Telegram token or `/data`.
This feature branch/PR does not deploy itself. A later authorized merge to
`main` follows the unchanged CI → `deploy` → server deployment workflow.

## Daily scheduling

The task wakes every 5 minutes. In the configured bot timezone (UTC+3 in the
current configuration), a check is eligible after 09:00 only when
`last_successful_check` is older than today. A late startup catches up that day.
After a successful check, repeated wakes/restarts do **not** request the source.
On error the loop logs the technical problem and retries after 30 minutes.
A failed attempt does not advance the successful date. A restart after a failed
attempt can retry immediately once the daily check is due.

## Source and parsing

Every real attempt downloads the source page and selects the third-course PDF
inside the institute's own HTML container. Both link text and its `title` are
supported; numeric accordion IDs and dated PDF filenames are not assumptions.
Missing/ambiguous institute/course links fail closed.

Both HTML and PDF requests, including redirects, use a new random query nonce,
`Cache-Control: no-cache, no-store, max-age=0` and `Pragma: no-cache`.
No ETag/If-Modified-Since conditional requests or cached PDF fallback is used.
Only fresh HTTP 200 results are accepted; redirects must remain HTTPS on
Timacad. The normalized original/final URL (without the monitor's own nonce)
is stored and linked in notifications. HTTP bodies have size limits, and both
the `%PDF-` signature and parsing must succeed. HTML error pages are rejected.

pdfplumber reads ruled table cells, not a flat list of nonempty text. Group
headers determine column geometry. Merged horizontal cells may belong to both
groups; merged weekday cells apply throughout their vertical span. Spaced,
vertical or reverse-extracted weekday text is normalized. Empty day cells
inherit the preceding day. Times are read from the actual time column and
normalized to `HH:MM-HH:MM`; invalid ranges, missing time rows, holes in group
columns, duplicate slots, missing groups and incomplete weeks fail closed.
At least Monday–Friday must be represented; additional weekdays are supported.
If a previously parsed weekday disappears, the monitor requests manual parser
review rather than sending a possibly false mass-deletion notification.

Stored data is `groups[group] = [{day, time, lesson}, ...]`, including empty
slots. Cell subdivisions are retained in reading order, separated by ` | `,
including empty positions. They are **not guessed to be specific week parities
or subgroups**. Spaces, line breaks, nonbreaking spaces, soft hyphens and dash
variants are normalized. Case, actual digits and lesson text are preserved.

Normalized schedules are the source of truth. The whole-PDF SHA-256 is only
diagnostic. Changes in other groups, metadata or the PDF URL alone are silent.
The diff identifies added/removed/changed lessons and unambiguous same-day time
moves. A room-only suffix change is labelled as a room change only if everything
else is identical. Subject/teacher/type/complex changes use honest `Было/Стало`
text; there is no heuristic guess of staff names or subject boundaries.

## Persistence, recipients and interrupted sends

`/data/schedule_monitor/state.json` contains schema version, successful date,
PDF URL/hash and the full normalized groups. Saves use a same-directory temp
file, flush, fsync, atomic replace and directory fsync (on POSIX).
Corrupt state is logged and left untouched: it is not silently replaced with
a new baseline. Restore it from a trusted `/data` backup after investigating.
Explicitly removing it resets the baseline silently and discards change history.

On first successful run no messages are sent. On subsequent runs the sequence
is: find/download/validate/parse both groups → diff → required delivery attempts
→ atomically accept the new baseline and successful date. Failures before this
retain the accepted baseline and successful date. On restart an unfinished
check fetches and validates the source again.

The monitor reads `application.bot_data[ACTIVE_CHAT_IDS_KEY]`, exactly the set
used by `activity_gate` and populated from `BotStorage.active_chat_ids()`.
It respects `/start` and `/stop`, including deactivation during a multipart
broadcast. Both positive private IDs and negative group/supergroup IDs work.
`Forbidden` and known unavailable-chat errors deactivate through the existing
`BotStorage.set_chat_active` and update that same in-memory set. Other send
errors do not deactivate the chat.

The pending delivery journal is **not** a subscriber list. It records the
specific event and already attempted chat/message parts, with stable chunk
boundaries. Each attempt is saved **before** calling Telegram. Thus a restart,
timeout or final-state-write failure cannot repeatedly send the same pending
part. Current active chats are rechecked when recovering; stopped chats are
not contacted. New baseline acceptance removes the pending journal.

**Delivery tradeoff:** Telegram sendMessage has no idempotency key and cannot
share a transaction with local disk. We prioritize no duplicate/spam attempts.
If the process dies between recording an attempt and sending, or Telegram times
out/rejects it, that part can be missed. It is logged and not blindly retried.
This is at-most-once attempt delivery, not a claim of exactly-once guaranteed
receipt. One inaccessible chat does not prevent attempts to the remaining chats.

Changes are ordered by group/day/time and normally combined into one message
per active chat. Large changes split below 4096 UTF-16 units; plain text avoids
Markdown/HTML injection. Every part contains the site instruction and PDF link.
Consecutive parts wait 1.1 seconds in private chats / 3.1 seconds in groups;
active status is rechecked after the pause so `/stop` takes effect immediately.
Technical errors, baseline creation and unchanged results never send Telegram
messages, including through the bot's public error handler.

## Offline tests and manual smoke

```sh
python -m unittest discover -s tests -v
```

The checked-in HTML/PDF fixtures were captured during the explicit manual smoke
on 2026-09-28. Automated tests use only fixtures, mock HTTP transport, temporary
storage and fake Telegram calls. They cover actual PDF extraction in a separate
process, silent results, errors, diffs, daily gating, atomic writes, interrupted
broadcast recovery, existing active-chat lifecycle and unchanged watchdog tests.

**Manual network check only**, outside the automated suite:

```sh
python -m scripts.smoke_schedule_monitor --output /tmp/timacad-manual-smoke
```

Use a fresh, separate directory, never production `/data`. The script downloads
the current page/PDF, prints parsed groups/day/time examples, asserts the
unchanged diff is empty and prints a **synthetic, unsent** change notification.
It does not load settings, secrets, a Telegram client or active chats. It saves
the HTML, PDF and normalized JSON for inspection. The current fixture has four
pages; page 4 has both target groups, 25 slots per group (20/17 nonempty).
