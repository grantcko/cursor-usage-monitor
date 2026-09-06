"""Config, state, and log paths. Secrets stay out of the git tree."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("CURSOR_USAGE_MONITOR_CONFIG_DIR")
    or (Path.home() / ".config" / "cursor-usage-monitor")
)
STATE_PATH = CONFIG_DIR / "state.json"
WEBHOOK_PATH = CONFIG_DIR / "webhook.url"
WEBHOOK_KEY_PATH = CONFIG_DIR / "webhook.key"
WEBHOOK_HEADER_PATH = CONFIG_DIR / "webhook.header"
ALERTS_PATH = CONFIG_DIR / "alerts.log"

LOG_DIR = Path(
    os.environ.get("CURSOR_USAGE_MONITOR_LOG_DIR")
    or (Path.home() / ".local" / "share" / "cursor-usage-monitor")
)
LOG_PATH = LOG_DIR / "monitor.log"

AUTH_PATH = Path(
    os.environ.get("CURSOR_AUTH_PATH")
    or (Path.home() / ".config" / "cursor" / "auth.json")
)
