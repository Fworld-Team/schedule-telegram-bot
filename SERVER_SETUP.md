# Production deployment

The production bot runs on `158.160.156.107` and keeps runtime state outside
GitHub. A merge to `main` triggers CI. After the tests and Docker build pass,
the workflow force-updates the `deploy` branch to that exact commit. A root-owned
systemd timer on the server fetches only `deploy` with a repository-scoped,
read-only SSH key and installs the release within about one minute.

GitHub never receives an SSH key for the server. The server key can only read
this repository. The Telegram token, admin IDs and Docker volume remain on the
server under `/etc/fworld-bot` and are never committed.

## Files on the server

- deployment service: `fworld-deploy.service` and `fworld-deploy.timer`;
- deploy command: `/usr/local/sbin/fworld-deploy-poll`;
- read-only repository mirror: `/var/lib/fworld-deploy/repository`;
- immutable releases: `/srv/bots/fworld-releases/<commit-sha>`;
- current release link: `/srv/bots/fworld-current`;
- settings and token: `/etc/fworld-bot`;
- persistent data: Docker volume `fworld_bot_data`;
- outbound network: external Docker network `bots-egress`.

The timer does nothing until the `deploy` branch exists. `DEPLOY_ENABLED=true`
allows the workflow to create or update that branch after CI succeeds.

## Normal operation

Open a pull request into `main`. CI checks tracked files for credentials and
runtime data, runs the Python tests, validates the initial media and builds the
container. Merge only after CI succeeds. The `Deploy production` workflow then
promotes the exact merge commit; the server timer deploys it.

```sh
sudo systemctl status fworld-deploy.timer --no-pager
sudo journalctl -u fworld-deploy.service --since '30 minutes ago' --no-pager
sudo cat /var/lib/fworld-deploy/current-sha
sudo docker ps --filter label=com.docker.compose.project=fworld-bot
```

The image is tagged with the commit SHA. Build failures leave the current
container untouched. If container startup or the Telegram-aware healthcheck
fails, the deploy command restores the previous image. The next timer run will
retry the promoted commit.

The healthcheck verifies SQLite and performs `getMe` through the bot proxy. It
does not consume Telegram updates. This catches revoked tokens and a failed VPN
path, which the old database-only check could not detect.

Do not run `docker compose down -v`: it deletes the persistent schedule volume.
Do not commit `.env`, `token.txt`, `data/`, private keys or files under
`secrets/` other than its README. `scripts/check_repository.py` enforces this in
CI, and the repository history was separately scanned before enabling deploys.

## Initial key setup

The server generates `/etc/fworld-deploy/github_readonly` and its `.pub` file.
Add the public key in GitHub under **Settings → Deploy keys**, without write
access. The server pins GitHub's published Ed25519 host key. GitHub documents the
current fingerprint as `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.

If the key is rotated, replace it both on the server and in the repository's
Deploy keys. Removing the Deploy key immediately revokes server access.
