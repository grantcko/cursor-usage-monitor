"""CLI: cursor-usage-monitor --once | usage | serve | check."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .core import VERSION, CursorRPCError, fetch_plans, process
from .server import DEFAULT_HOST, DEFAULT_PORT, serve as serve_http


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _run_once(*, webhook: bool, json_out: bool) -> int:
    try:
        result = process(webhook=webhook)
    except FileNotFoundError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 1
    except CursorRPCError as exc:
        print(f"cursor rpc error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if json_out:
        _print_json(
            {
                "event": result["payload"]["event"],
                "crossed": result["crossed"],
                "plans": result["payload"]["plans"],
                "all": result["payload"]["all"],
                "webhook": result["webhook"],
                "errors": result["payload"].get("errors") or {},
            }
        )
    return 0


def _run_usage(*, json_out: bool) -> int:
    try:
        snapshot = fetch_plans()
    except FileNotFoundError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 1
    except CursorRPCError as exc:
        print(f"cursor rpc error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if json_out:
        _print_json(snapshot)
        return 0
    meters = snapshot.get("meters") or {}
    if not meters:
        print("no meters in snapshot")
        return 0
    width = max(len(m["name"]) for m in meters.values())
    for meter in meters.values():
        value = meter.get("value")
        unit = meter.get("unit")
        bucket = meter.get("bucket")
        if unit == "percent":
            shown = f"{value:.2f}%" if isinstance(value, (int, float)) else str(value)
        elif unit == "usd":
            shown = f"${value:.2f}" if isinstance(value, (int, float)) else str(value)
        else:
            shown = str(value)
        print(f"{meter['name']:<{width}}  {shown}  bucket={bucket}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cursor-usage-monitor",
        description=(
            "Monitor Cursor plan / Grok Bot usage buckets. "
            "Alerts only on upward 5% (plan) or +$10 (on-demand) crosses."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch usage, update state, POST webhook on crosses (cron entry).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON on stdout.",
    )
    parser.add_argument(
        "--no-webhook",
        action="store_true",
        help="With --once / check: do not POST the webhook.",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("usage", help="Print a live meter snapshot without writing state.")
    check = sub.add_parser("check", help="Run bucket logic (same as --once).")
    check.add_argument("--no-webhook", action="store_true", dest="check_no_webhook")
    check.add_argument("--json", action="store_true", dest="check_json")

    serve_p = sub.add_parser("serve", help="Run the localhost HTTP API.")
    serve_p.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default {DEFAULT_HOST})")
    serve_p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Bind port (default {DEFAULT_PORT})",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.once:
        return _run_once(webhook=not args.no_webhook, json_out=args.json)

    command = args.command
    if command == "usage":
        return _run_usage(json_out=args.json)
    if command == "check":
        webhook = not (args.no_webhook or getattr(args, "check_no_webhook", False))
        json_out = args.json or getattr(args, "check_json", False)
        return _run_once(webhook=webhook, json_out=json_out)
    if command == "serve":
        serve_http(host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 0
