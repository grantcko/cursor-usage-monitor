# INSTALL — Linux host

Target shape: Python 3.9+ on a logged-in Cursor CLI user, cron in your local timezone, optional localhost HTTP API. Secrets stay on disk under `~/.config`, never in git.

Example one-shot:

```
~/.local/bin/cursor-usage-monitor --once
```

Example cron (edit timezone and path):

```
CRON_TZ=UTC
0 8-22 * * * ~/.local/bin/cursor-usage-monitor --once
```

## 0. GitHub repo

Create a private (or public) repo under your own GitHub account, then push this tree. Do not commit tokens, webhook URLs, or `auth.json`.

```bash
# example — replace YOUR_GITHUB_USER
gh repo create YOUR_GITHUB_USER/cursor-usage-monitor --private --source=. --remote=origin --push
gh repo view YOUR_GITHUB_USER/cursor-usage-monitor --json isPrivate,url,visibility
```

SSH remotes are preferred so a PAT does not sit in `git remote -v`.

## 1. Clone onto the host

```bash
cd ~
git clone git@github.com:YOUR_GITHUB_USER/cursor-usage-monitor.git
cd cursor-usage-monitor
```

## 2. Install the CLI

Prefer a venv (PEP 668 / managed Python):

```bash
python3 --version   # 3.9+
python3 -m venv .venv
.venv/bin/pip install -e .
command -v cursor-usage-monitor || ln -sf "$(pwd)/.venv/bin/cursor-usage-monitor" ~/.local/bin/cursor-usage-monitor
cursor-usage-monitor --help
python3 -c 'import cursor_usage_monitor; print(cursor_usage_monitor.VERSION)'
```

Keep `~/.local/bin` on `PATH` for cron (cron often has a minimal PATH).

## 3. Cursor session token

The monitor does **not** store a token of its own.

1. Sign the host into Cursor CLI so `~/.config/cursor/auth.json` exists and contains `accessToken`, **or**
2. Export `CURSOR_ACCESS_TOKEN` in the cron/systemd environment.

```bash
python3 - <<'PY'
from pathlib import Path
p = Path.home() / ".config" / "cursor" / "auth.json"
print("auth.json exists:", p.is_file())
if p.is_file():
    import json
    data = json.loads(p.read_text())
    print("accessToken present:", bool(data.get("accessToken") or data.get("access_token")))
PY
# Never print the token itself.
```

`chmod 600 ~/.config/cursor/auth.json`.

## 4. Config directory (no secrets in the repo)

```bash
mkdir -p ~/.config/cursor-usage-monitor
mkdir -p ~/.local/share/cursor-usage-monitor
chmod 700 ~/.config/cursor-usage-monitor
```

Webhook (optional):

```bash
# live file — URL only, no comments
umask 077
printf '%s\n' 'https://YOUR-PRIVATE-WEBHOOK' > ~/.config/cursor-usage-monitor/webhook.url
printf '%s\n' 'X-Webhook-Key' > ~/.config/cursor-usage-monitor/webhook.header
printf '%s\n' 'YOUR-LONG-RANDOM-SECRET' > ~/.config/cursor-usage-monitor/webhook.key
chmod 600 ~/.config/cursor-usage-monitor/webhook.url \
          ~/.config/cursor-usage-monitor/webhook.key \
          ~/.config/cursor-usage-monitor/webhook.header
```

See `examples/webhook.url.example` in the repo. Do not copy that example URL into production.

Runtime files created by the monitor:

| Path | Git? |
| --- | --- |
| `~/.config/cursor-usage-monitor/state.json` | no |
| `~/.config/cursor-usage-monitor/alerts.log` | no |
| `~/.local/share/cursor-usage-monitor/monitor.log` | no |

## 5. Smoke test

```bash
cursor-usage-monitor usage --json
cursor-usage-monitor --no-webhook --once --json
# first run: event=baseline, crossed=[], no webhook
```

A second `--once` after usage has moved a 5% / $10 band should POST the webhook.

## 6. Cron

```bash
crontab -e
```

```cron
CRON_TZ=UTC
0 8-22 * * * /home/YOUR_USER/.local/bin/cursor-usage-monitor --once >/dev/null 2>&1
```

Template: `examples/crontab.example`. Set `CRON_TZ` and the absolute binary path for your host.

Confirm:

```bash
crontab -l
grep -E 'cursor-usage-monitor' ~/.local/share/cursor-usage-monitor/monitor.log | tail
```

## 7. Optional localhost HTTP API

```bash
cursor-usage-monitor serve --host 127.0.0.1 --port 47821
```

systemd user unit: `examples/cursor-usage-monitor.service`

```bash
mkdir -p ~/.config/systemd/user
cp examples/cursor-usage-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cursor-usage-monitor.service
curl -sS http://127.0.0.1:47821/health
```

Stay on `127.0.0.1`. If you bind another address, set `CURSOR_USAGE_API_TOKEN` and put a proxy in front.

## 8. Security checklist

- [ ] `git ls-files` does not include `auth.json`, `webhook.url`, `webhook.key`, `state.json`, tokens
- [ ] Config dir `700`, secret files `600`
- [ ] Cron uses an absolute path under the same user that owns `auth.json`
- [ ] HTTP API not published to the internet

## 9. Uninstall

```bash
python3 -m pip uninstall cursor-usage-monitor
crontab -l | grep -v cursor-usage-monitor | crontab -
# optional: rm -rf ~/.config/cursor-usage-monitor ~/.local/share/cursor-usage-monitor
```
