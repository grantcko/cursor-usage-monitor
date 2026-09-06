# cursor-usage-monitor

Monitor for **Cursor plan usage** and **Grok Bot weekly usage**. It is a library, a cron-friendly CLI, and a tiny localhost HTTP API. There is no UI.

Behavior: poll, compare buckets, alert only on upward crosses, POST a webhook only then, and include **every meter** on every alert.

Tokens, `auth.json`, webhook URLs, and state files are gitignored and must never be committed.

## Official API check (2026-09)

Cursor’s documented APIs ([cursor.com/docs/api](https://cursor.com/docs/api)) are:

| API | What it covers | Who can use it |
| --- | --- | --- |
| Admin API / Analytics API | Team spend, usage events, members | **Enterprise teams** |
| Cloud Agents API / SDKs | Running agents, not plan meters | All plans (agent runtime) |

There is **no official documented public API** for an individual account’s dashboard meters (plan total / Auto / API / on-demand / Grok Bot weekly). Those numbers still come from the same Connect RPCs the Cursor dashboard uses:

- `POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`
- `POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus`

Auth is the Cursor session token (`CURSOR_ACCESS_TOKEN` or `~/.config/cursor/auth.json` `accessToken`). These RPCs are **undocumented and can change**. The Enterprise Admin API is not a drop-in replacement for this personal monitor.

If Cursor later ships a documented personal usage API, switch `fetch_plans()` to that and keep the bucket / webhook layer.

## What it watches

Works with any Cursor account that exposes these dashboard meters (Hobby / Pro / Pro+ / etc. — field names are the same; display labels are generic).

| Meter | Source | Alert |
| --- | --- | --- |
| Grok Bot | `GetSandUsageStatus.usagePercent` (weekly) | upward **5%** bucket |
| Plan total | `planUsage.totalPercentUsed` | upward **5%** bucket |
| Auto | `planUsage.autoPercentUsed` | upward **5%** bucket |
| API | `planUsage.apiPercentUsed` | upward **5%** bucket |
| On-demand | `spendLimitUsage` cents → USD | every **+$10** |

Buckets are high-water within a period. Crossing 14% → 16% fires (10 → 15). Sitting inside the same 5% band does not. First run and billing/weekly period reset store a new baseline and **do not** alert.

Webhook JSON always includes:

- `plans` — all meters
- `all` — same object (alias)
- `crossed` — only the meters that moved up a bucket this check

## Install

Python 3.9+, stdlib only (no third-party runtime deps).

```bash
git clone git@github.com:YOUR_GITHUB_USER/cursor-usage-monitor.git
cd cursor-usage-monitor
python3 -m pip install --user .
# console script lands on PATH, typically ~/.local/bin
cursor-usage-monitor --help
```

On systems with PEP 668, use a venv instead of `--user`.

Editable install for development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Full host setup (cron, webhook files, permissions): see [INSTALL.md](INSTALL.md).

## Auth

In order:

1. `CURSOR_ACCESS_TOKEN` environment variable
2. `~/.config/cursor/auth.json` → `accessToken` (Cursor CLI / app login)

`userId::jwt` values are accepted; the JWT after `::` is used. **Never commit the token or auth.json.**

## CLI

```bash
# Cron entry (example):
cursor-usage-monitor --once

# Snapshot only — no state write, no webhook
cursor-usage-monitor usage
cursor-usage-monitor usage --json

# Same bucket logic as --once, no webhook
cursor-usage-monitor --no-webhook --once --json
cursor-usage-monitor check --no-webhook --json

# Localhost API (default 127.0.0.1:47821)
cursor-usage-monitor serve
cursor-usage-monitor serve --host 127.0.0.1 --port 47821
```

`--once` writes `~/.config/cursor-usage-monitor/state.json` and POSTs the webhook only when something crossed.

## HTTP API

Bind stays on loopback unless you pass another `--host`. Do not expose this on a public interface.

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/health` | Liveness. `{ok, service, version, token_present}` |
| `GET` | `/usage` | Live meter snapshot. Does **not** write state or webhook. |
| `POST` | `/check` | Run bucket logic. Webhook is **off** unless `"webhook": true` or `?webhook=1`. |

```bash
curl -sS http://127.0.0.1:47821/health
curl -sS http://127.0.0.1:47821/usage
curl -sS -X POST http://127.0.0.1:47821/check
curl -sS -X POST http://127.0.0.1:47821/check \
  -H 'Content-Type: application/json' \
  -d '{"webhook": true}'
```

Optional gate for the API (not required on 127.0.0.1): set `CURSOR_USAGE_API_TOKEN` and send `Authorization: Bearer …` or `X-Api-Token`.

## Webhook wiring

Files under `~/.config/cursor-usage-monitor/` (mode `600`):

| File | Purpose |
| --- | --- |
| `webhook.url` | Absolute `https://…` URL. Required to POST. |
| `webhook.key` | Secret value for an extra header. Optional. |
| `webhook.header` | Header name. Default `X-Webhook-Key` if a key is set. |
| `state.json` | Last buckets + period ids. Created at runtime. |
| `alerts.log` | JSONL copy of payloads that were eligible to POST. |

Example file (do not use as the live path): [`examples/webhook.url.example`](examples/webhook.url.example).

Env overrides: `CURSOR_USAGE_WEBHOOK_URL`, `CURSOR_USAGE_WEBHOOK_KEY`, `CURSOR_USAGE_WEBHOOK_HEADER`.

Example payload:

```json
{
  "ts": "2026-09-06T11:00:00+00:00",
  "event": "bucket_cross",
  "plans": { "grok_bot": { "value": 16.2, "bucket": 15, "unit": "percent" } },
  "all": { "grok_bot": { "value": 16.2, "bucket": 15, "unit": "percent" } },
  "crossed": [
    {
      "id": "grok_bot",
      "name": "Grok Bot",
      "from_bucket": 10,
      "to_bucket": 15,
      "value": 16.2,
      "unit": "percent"
    }
  ]
}
```

(`plans` / `all` include every meter present that check, not only `crossed`.)

## Cron

```cron
CRON_TZ=UTC
0 8-22 * * * /usr/local/bin/cursor-usage-monitor --once >/dev/null 2>&1
```

Adjust `CRON_TZ` and the binary path for your host. Logs: `~/.local/share/cursor-usage-monitor/monitor.log`.

## Library

```python
from cursor_usage_monitor import fetch_plans, process, read_token

snapshot = fetch_plans()          # meters only
result = process(webhook=False)   # buckets + optional webhook
```

## Security

- No tokens, webhook URLs, or `auth.json` in git.
- Session token can act as the Cursor user. Treat `CURSOR_ACCESS_TOKEN` like a password.
- Webhook URL and key live only in `~/.config/cursor-usage-monitor/` with mode `600`.
- HTTP API defaults to `127.0.0.1`. Binding `0.0.0.0` without a reverse proxy and auth is a credential leak.
- State files are chmod `600` when the process can set mode.

## Layout

```
src/cursor_usage_monitor/   library + CLI + HTTP server
tests/                      stdlib unittest
examples/                   webhook.url.example, crontab, systemd user unit
INSTALL.md                  Linux host deploy notes
```

MCP is out of scope; add it later against this library if needed.
