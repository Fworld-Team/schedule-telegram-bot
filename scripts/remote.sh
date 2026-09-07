#!/usr/bin/env bash
# Runs on an ephemeral GitHub-hosted runner. Never print key contents.
set -euo pipefail
for setting in SERVER_HOST SERVER_USER SERVER_SSH_KEY SERVER_KNOWN_HOSTS; do
  if [[ -z "${!setting:-}" ]]; then
    echo "Missing GitHub Actions secret: $setting" >&2
    exit 1
  fi
done
SERVER_PORT="${SERVER_PORT:-22}"
[[ "$SERVER_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || exit 2
[[ "$SERVER_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || exit 2
[[ "$SERVER_PORT" =~ ^[0-9]{1,5}$ ]] || exit 2
(( 10#$SERVER_PORT >= 1 && 10#$SERVER_PORT <= 65535 )) || exit 2

case "${1:-}" in
  deploy)
    [[ "${2:-}" =~ ^[0-9a-f]{40}$ ]] || exit 2
    remote_command="bash /opt/schedule-bot/scripts/deploy.sh $2"
    ;;
  status)
    remote_command='bash /opt/schedule-bot/scripts/status.sh'
    ;;
  *) echo 'Usage: remote.sh deploy COMMIT_SHA | status' >&2; exit 2 ;;
esac

ssh_directory="$(mktemp -d)"
chmod 700 "$ssh_directory"
trap 'rm -f "$ssh_directory/key" "$ssh_directory/known_hosts"; rmdir "$ssh_directory"' EXIT
umask 077
printf '%s\n' "$SERVER_SSH_KEY" > "$ssh_directory/key"
printf '%s\n' "$SERVER_KNOWN_HOSTS" > "$ssh_directory/known_hosts"
ssh -i "$ssh_directory/key" -p "$SERVER_PORT" \
  -o UserKnownHostsFile="$ssh_directory/known_hosts" \
  -o StrictHostKeyChecking=yes -o BatchMode=yes -o IdentitiesOnly=yes \
  -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
  "$SERVER_USER@$SERVER_HOST" "$remote_command"
