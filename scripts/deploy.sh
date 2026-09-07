#!/usr/bin/env bash
# Server checkout must be /opt/schedule-bot; run as its dedicated owner.
set -euo pipefail
[[ "${1:-}" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected full commit SHA' >&2; exit 2; }
cd /opt/schedule-bot
exec 9>.deploy.lock
flock -w 600 9
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo 'Tracked server files have local changes. Deployment stopped.' >&2
  exit 1
fi
git fetch --prune origin main
git cat-file -e "$1^{commit}"
# Avoid deploying an unrelated branch or overwriting a newer release.
if [[ "$(git rev-parse origin/main)" != "$1" ]]; then
  echo 'main has moved since this run. Use the newest successful workflow.' >&2
  exit 1
fi
git checkout --detach "$1"
compose=(docker compose -p schedule-bot -f compose.yaml -f deploy/compose.production.yaml)
"${compose[@]}" config --quiet
test -s secrets/telegram_bot_token.txt
# Build first so a failed build leaves the old running container intact.
"${compose[@]}" build bot
"${compose[@]}" up -d --no-build --wait --wait-timeout 120 bot
bash scripts/status.sh
printf 'Deployed %s\n' "$1"
