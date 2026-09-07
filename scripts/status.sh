#!/usr/bin/env bash
# Read-only infrastructure check. Does not consume Telegram updates.
set -euo pipefail
cd /opt/schedule-bot
compose=(docker compose -p schedule-bot -f compose.yaml -f deploy/compose.production.yaml)
container_id="$("${compose[@]}" ps -q bot)"
test -n "$container_id"
test "$(docker inspect --format '{{.State.Status}}' "$container_id")" = running
test "$(docker inspect --format '{{.State.Health.Status}}' "$container_id")" = healthy
"${compose[@]}" exec -T bot python -m schedule_bot --healthcheck
