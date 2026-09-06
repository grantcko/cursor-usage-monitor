"""Local Cursor / Grok Bot usage bucket monitor.

Polls Cursor's undocumented DashboardService Connect RPCs on api2.cursor.sh
(the same endpoints the local cron on Cursor account dashboard uses).

There is no official public personal usage API as of 2026-09. Cursor's
documented Admin / Analytics APIs are Enterprise-team only and do not replace
GetCurrentPeriodUsage + GetSandUsageStatus for an individual account's plan /
Grok Bot meters. See README.md.

Alerts fire only on upward bucket crosses:
  * Grok Bot / plan total / Auto / API — 5% buckets
  * On-demand spend — every +$10

Webhook POST happens only on crosses. Every alert payload includes ALL meters
(`plans` and `all`) plus `crossed`. First run and period reset are silent
baselines.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from . import paths

VERSION = "1.0.0"
API_BASE = os.environ.get("CURSOR_API_BASE", "https://api2.cursor.sh").rstrip("/")
HEADERS_BASE = {
    "Content-Type": "application/json",
    "Connect-Protocol-Version": "1",
    "User-Agent": f"cursor-usage-monitor/{VERSION}",
}
PERCENT_STEP = int(os.environ.get("CURSOR_USAGE_PERCENT_STEP", "5"))
ON_DEMAND_STEP_USD = float(os.environ.get("CURSOR_USAGE_ON_DEMAND_STEP", "10"))
RPC_TIMEOUT_SEC = float(os.environ.get("CURSOR_USAGE_RPC_TIMEOUT", "20"))
WEBHOOK_TIMEOUT_SEC = float(os.environ.get("CURSOR_USAGE_WEBHOOK_TIMEOUT", "15"))

METER_GROK = "grok_bot"
METER_TOTAL = "plan_total"
METER_AUTO = "auto"
METER_API = "api"
METER_ON_DEMAND = "on_demand"

PLAN_METERS = (METER_TOTAL, METER_AUTO, METER_API, METER_ON_DEMAND)
GROK_METERS = (METER_GROK,)

METER_NAMES = {
    METER_GROK: "Grok Bot",
    METER_TOTAL: "Plan total",
    METER_AUTO: "Auto",
    METER_API: "API",
    METER_ON_DEMAND: "On-demand",
}


# ---------------------------------------------------------------------------
# Clock / logging
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def log(message: str, *, path: Optional[Path] = None) -> None:
    line = f"{utc_now_iso()} {message}"
    print(line, file=sys.stderr)
    log_path = path or paths.LOG_PATH
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


def bucket_of(value: float, step: float = PERCENT_STEP) -> int:
    """Largest N*step that is <= value. Negative values clamp to 0."""
    if step <= 0:
        raise ValueError("step must be positive")
    if value < 0:
        value = 0.0
    return int(value // step) * int(step)


def percent_bucket(value: float) -> int:
    return min(100, bucket_of(value, PERCENT_STEP))


def money_bucket(value: float) -> int:
    return bucket_of(value, ON_DEMAND_STEP_USD)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state(path: Optional[Path] = None) -> dict[str, Any]:
    state_path = path or paths.STATE_PATH
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"state: could not read {state_path}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any], path: Optional[Path] = None) -> None:
    state_path = path or paths.STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(state_path)
    _chmod_private(state_path)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _extract_token(raw: str) -> str:
    token = raw.strip().strip('"')
    if "::" in token:
        maybe = token.rsplit("::", 1)[-1]
        if maybe.count(".") >= 2:
            token = maybe
    return token.strip()


def read_token(auth_path: Optional[Path] = None) -> str:
    env = os.environ.get("CURSOR_ACCESS_TOKEN", "").strip()
    if env:
        return _extract_token(env)

    path = auth_path or paths.AUTH_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"No CURSOR_ACCESS_TOKEN and auth file missing: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"auth.json is not valid JSON: {path}") from exc

    token = ""
    if isinstance(data, dict):
        raw = data.get("accessToken") or data.get("access_token")
        if isinstance(raw, str):
            token = raw
        else:
            for key in ("token", "auth", "user"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    raw = nested.get("accessToken") or nested.get("access_token")
                    if isinstance(raw, str):
                        token = raw
                        break
    if not token:
        raise ValueError(f"No accessToken in {path}")
    return _extract_token(token)


# ---------------------------------------------------------------------------
# Cursor DashboardService RPCs (undocumented; not the Enterprise Admin API)
# ---------------------------------------------------------------------------


class CursorRPCError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def rpc(
    method: str,
    token: str,
    *,
    body: Optional[dict[str, Any]] = None,
    api_base: Optional[str] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """POST /aiserver.v1.DashboardService/{method} with Connect protocol headers."""
    base = (api_base or API_BASE).rstrip("/")
    url = f"{base}/aiserver.v1.DashboardService/{method}"
    payload = json.dumps(body if body is not None else {}).encode("utf-8")
    headers = dict(HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    do_open = opener or urllib.request.urlopen
    try:
        with do_open(req, timeout=RPC_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        raise CursorRPCError(
            f"{method} HTTP {exc.code}",
            status=exc.code,
            body=err_body[:500],
        ) from exc
    except urllib.error.URLError as exc:
        raise CursorRPCError(f"{method} network error: {exc.reason}") from exc

    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CursorRPCError(f"{method} returned non-JSON", body=raw[:500]) from exc
    if not isinstance(parsed, dict):
        raise CursorRPCError(f"{method} returned non-object JSON")
    return parsed


def _as_float(value: Any) -> Optional[float]:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_intish(value: Any) -> Optional[int]:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _cents_to_usd(cents: Any) -> Optional[float]:
    number = _as_float(cents)
    if number is None:
        return None
    return round(number / 100.0, 4)


def _period_id(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _meter(
    meter_id: str,
    value: Optional[float],
    unit: str,
    *,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    bucket: Optional[int] = None
    if value is not None:
        bucket = percent_bucket(value) if unit == "percent" else money_bucket(value)
    out: dict[str, Any] = {
        "id": meter_id,
        "name": METER_NAMES[meter_id],
        "value": value,
        "unit": unit,
        "bucket": bucket,
    }
    if extra:
        out.update(extra)
    return out


def fetch_plans(
    token: Optional[str] = None,
    *,
    rpc_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Fetch Grok Bot + plan + on-demand meters from DashboardService."""
    tok = token if token is not None else read_token()
    call = rpc_fn or rpc

    period: dict[str, Any] = {}
    sand: dict[str, Any] = {}
    errors: dict[str, str] = {}

    try:
        period = call("GetCurrentPeriodUsage", tok)
    except Exception as exc:
        errors["plan"] = str(exc)
        period = {}

    try:
        sand = call("GetSandUsageStatus", tok)
    except Exception as exc:
        errors["grok"] = str(exc)
        sand = {}

    if not period and not sand:
        detail = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "empty responses"
        raise CursorRPCError(f"both usage RPCs failed ({detail})")

    plan_usage = period.get("planUsage") if isinstance(period.get("planUsage"), dict) else {}
    spend = (
        period.get("spendLimitUsage")
        if isinstance(period.get("spendLimitUsage"), dict)
        else {}
    )

    total_pct = _as_float(plan_usage.get("totalPercentUsed"))
    auto_pct = _as_float(plan_usage.get("autoPercentUsed"))
    api_pct = _as_float(plan_usage.get("apiPercentUsed"))

    included_spend = _cents_to_usd(plan_usage.get("includedSpend"))
    included_limit = _cents_to_usd(plan_usage.get("limit"))
    included_remaining = _cents_to_usd(plan_usage.get("remaining"))
    total_spend = _cents_to_usd(plan_usage.get("totalSpend"))

    on_demand_used = _cents_to_usd(
        spend.get("individualUsed") if spend.get("individualUsed") is not None else spend.get("totalSpend")
    )
    on_demand_limit = _cents_to_usd(
        spend.get("individualLimit") if spend.get("individualLimit") is not None else spend.get("pooledLimit")
    )
    on_demand_remaining = _cents_to_usd(
        spend.get("individualRemaining")
        if spend.get("individualRemaining") is not None
        else spend.get("pooledRemaining")
    )

    grok_pct = _as_float(sand.get("usagePercent"))
    grok_available = sand.get("hasAvailableUsage")
    grok_has_limit = sand.get("hasNonZeroIncludedLimit")
    grok_label = sand.get("grokPlanLabel")
    grok_reset = sand.get("nextResetTimestampUtc")

    meters: dict[str, Any] = {}
    if grok_has_limit is False:
        pass
    elif grok_pct is not None:
        meters[METER_GROK] = _meter(
            METER_GROK,
            grok_pct,
            "percent",
            extra={
                "available": grok_available,
                "resets_at": grok_reset,
                "plan_label": grok_label,
            },
        )

    if total_pct is not None:
        meters[METER_TOTAL] = _meter(
            METER_TOTAL,
            total_pct,
            "percent",
            extra={
                "included_spend_usd": included_spend,
                "included_limit_usd": included_limit,
                "included_remaining_usd": included_remaining,
                "total_spend_usd": total_spend,
            },
        )
    if auto_pct is not None:
        meters[METER_AUTO] = _meter(METER_AUTO, auto_pct, "percent")
    if api_pct is not None:
        meters[METER_API] = _meter(METER_API, api_pct, "percent")

    if spend:
        meters[METER_ON_DEMAND] = _meter(
            METER_ON_DEMAND,
            on_demand_used if on_demand_used is not None else 0.0,
            "usd",
            extra={
                "limit_usd": on_demand_limit,
                "remaining_usd": on_demand_remaining,
                "limit_type": spend.get("limitType"),
            },
        )

    snapshot = {
        "fetched_at": utc_now_iso(),
        "plan_period_id": _period_id(period.get("billingCycleStart")),
        "grok_period_id": _period_id(grok_reset),
        "billing_cycle_start": period.get("billingCycleStart"),
        "billing_cycle_end": period.get("billingCycleEnd"),
        "grok_resets_at": grok_reset,
        "meters": meters,
        "errors": errors,
    }
    return snapshot


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _read_text_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_webhook_config(
    *,
    url_path: Optional[Path] = None,
    key_path: Optional[Path] = None,
    header_path: Optional[Path] = None,
) -> dict[str, str]:
    url = os.environ.get("CURSOR_USAGE_WEBHOOK_URL", "").strip() or _read_text_file(
        url_path or paths.WEBHOOK_PATH
    )
    key = os.environ.get("CURSOR_USAGE_WEBHOOK_KEY", "").strip() or _read_text_file(
        key_path or paths.WEBHOOK_KEY_PATH
    )
    header = os.environ.get("CURSOR_USAGE_WEBHOOK_HEADER", "").strip() or _read_text_file(
        header_path or paths.WEBHOOK_HEADER_PATH
    )
    if key and not header:
        header = "X-Webhook-Key"
    return {"url": url, "key": key, "header": header}


def webhook_post(
    payload: dict[str, Any],
    *,
    url: Optional[str] = None,
    key: Optional[str] = None,
    header_name: Optional[str] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """POST JSON to the configured webhook. http(s) only."""
    cfg = load_webhook_config()
    target = (url if url is not None else cfg["url"]).strip()
    secret = key if key is not None else cfg["key"]
    header = (header_name if header_name is not None else cfg["header"]).strip()

    if not target:
        return {"attempted": False, "ok": False, "reason": "no webhook URL configured"}

    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"attempted": False, "ok": False, "reason": "webhook URL must be http(s)"}

    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"cursor-usage-monitor/{VERSION}",
    }
    if secret and header:
        headers[header] = secret

    req = urllib.request.Request(target, data=body, headers=headers, method="POST")
    do_open = opener or urllib.request.urlopen
    try:
        with do_open(req, timeout=WEBHOOK_TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", 200)
            resp.read()
        ok = 200 <= int(status) < 300
        return {"attempted": True, "ok": ok, "status": int(status)}
    except urllib.error.HTTPError as exc:
        return {"attempted": True, "ok": False, "status": int(exc.code), "reason": str(exc)}
    except Exception as exc:
        return {"attempted": True, "ok": False, "reason": str(exc)}


# ---------------------------------------------------------------------------
# Bucket processing
# ---------------------------------------------------------------------------


def _append_alert(payload: dict[str, Any]) -> None:
    try:
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with paths.ALERTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
        _chmod_private(paths.ALERTS_PATH)
    except OSError as exc:
        log(f"alerts.log write failed: {exc}")


def _high_water_update(
    meters: dict[str, Any],
    prev_buckets: dict[str, Any],
    baseline_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Alert only on upward bucket crosses. High-water within a period."""
    crossed: list[dict[str, Any]] = []
    new_buckets: dict[str, int] = {}
    for key, raw in prev_buckets.items():
        if isinstance(raw, int):
            new_buckets[key] = raw
        else:
            number = _as_intish(raw)
            if number is not None:
                new_buckets[key] = number

    for meter_id, meter in meters.items():
        bucket = meter.get("bucket")
        if bucket is None or meter.get("value") is None:
            continue
        bucket = int(bucket)
        if meter_id in baseline_ids or meter_id not in new_buckets:
            new_buckets[meter_id] = bucket
            continue
        prev = int(new_buckets[meter_id])
        if bucket > prev:
            crossed.append(
                {
                    "id": meter_id,
                    "name": meter.get("name") or METER_NAMES.get(meter_id, meter_id),
                    "from_bucket": prev,
                    "to_bucket": bucket,
                    "value": meter.get("value"),
                    "unit": meter.get("unit"),
                }
            )
            new_buckets[meter_id] = bucket
        # else keep high-water; no downward update, no alert
    return crossed, new_buckets


def process(
    *,
    token: Optional[str] = None,
    webhook: bool = True,
    fetch_fn: Optional[Callable[..., dict[str, Any]]] = None,
    webhook_fn: Optional[Callable[..., dict[str, Any]]] = None,
    state_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Fetch usage, compare buckets, optionally POST webhook on crosses."""
    snapshot = (fetch_fn or fetch_plans)(token) if fetch_fn else fetch_plans(token)
    meters: dict[str, Any] = snapshot.get("meters") or {}
    state = load_state(state_path)

    first_run = not state
    prev_plan_period = state.get("plan_period_id")
    prev_grok_period = state.get("grok_period_id")
    plan_period = snapshot.get("plan_period_id")
    grok_period = snapshot.get("grok_period_id")

    plan_reset = bool(
        prev_plan_period and plan_period and str(prev_plan_period) != str(plan_period)
    )
    grok_reset = bool(
        prev_grok_period and grok_period and str(prev_grok_period) != str(grok_period)
    )

    baseline_ids: set[str] = set()
    if first_run:
        baseline_ids.update(meters.keys())
    else:
        if plan_reset:
            baseline_ids.update(mid for mid in PLAN_METERS if mid in meters)
        if grok_reset:
            baseline_ids.update(mid for mid in GROK_METERS if mid in meters)

    prev_buckets = state.get("buckets") if isinstance(state.get("buckets"), dict) else {}
    crossed, new_buckets = _high_water_update(meters, prev_buckets, baseline_ids)

    new_state = {
        "version": 1,
        "updated_at": utc_now_iso(),
        "plan_period_id": plan_period or prev_plan_period,
        "grok_period_id": grok_period or prev_grok_period,
        "buckets": new_buckets,
        "baseline": first_run or plan_reset or grok_reset,
    }
    save_state(new_state, state_path)

    reason = "baseline" if (first_run or (not crossed and (plan_reset or grok_reset))) else (
        "bucket_cross" if crossed else "no_change"
    )
    if first_run:
        reason = "baseline"
        crossed = []
    elif plan_reset or grok_reset:
        # crosses from non-reset meters may still fire; reset meters were baselined
        reason = "bucket_cross" if crossed else "period_reset"

    payload = {
        "ts": utc_now_iso(),
        "event": reason,
        "reason": reason,
        "first_run": first_run,
        "plan_reset": plan_reset,
        "grok_reset": grok_reset,
        "plans": meters,
        "all": meters,
        "crossed": crossed,
        "period": {
            "plan_period_id": plan_period,
            "grok_period_id": grok_period,
            "billing_cycle_start": snapshot.get("billing_cycle_start"),
            "billing_cycle_end": snapshot.get("billing_cycle_end"),
            "grok_resets_at": snapshot.get("grok_resets_at"),
        },
        "errors": snapshot.get("errors") or {},
    }

    webhook_result: dict[str, Any] = {"attempted": False, "ok": False, "reason": "not requested"}
    should_post = bool(webhook and crossed and not first_run)
    if should_post:
        _append_alert(payload)
        poster = webhook_fn or webhook_post
        webhook_result = poster(payload)
        if webhook_result.get("ok"):
            log(f"webhook ok crossed={[c['id'] for c in crossed]}")
        else:
            log(f"webhook failed: {webhook_result}")
    elif first_run:
        log("baseline stored (first run, no alert)")
    elif plan_reset or grok_reset:
        log(
            "period reset baseline "
            f"plan_reset={plan_reset} grok_reset={grok_reset} crossed={[c['id'] for c in crossed]}"
        )
    elif crossed:
        log(f"crosses detected but webhook disabled: {[c['id'] for c in crossed]}")
    else:
        log("no bucket crosses")

    return {
        "snapshot": snapshot,
        "payload": payload,
        "crossed": crossed,
        "webhook": webhook_result,
        "state": new_state,
        "first_run": first_run,
    }
