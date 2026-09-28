# Offline Timacad fixture

Captured during the explicitly requested manual smoke test on 2026-09-28.
Tests never contact Timacad or Telegram.
The HTML fixture is a reduced structural excerpt retaining the real institute,
course-link attributes, an unrelated institute and another study-mode section.
The PDF is the unchanged original download.

- Page: https://www.timacad.ru/about/sveden/document/rezhim-zaniatii-obuchaiushchikhsia
- Dynamically discovered PDF: https://www.timacad.ru/uploads/files/20260924/1790239252_ieiu-3.pdf
- PDF SHA-256: `a08adb5c6e1544c065fa6dc002810d503d223e032f98a6e281415031940da3b5`
- Four pages; target groups are on page 4. Visual check: 5 weekdays × 5 time
  slots per group; 20 occupied slots for ДЭ 23-24 and 17 for ДЭ 24-24.
- Cell subdivisions are preserved in reading order using ` | `, including
  empty positions. We do not guess which subdivision means which week.

The dated URL above is fixture provenance only, never a runtime fallback.
