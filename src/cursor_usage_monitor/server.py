"""Tiny localhost HTTP API. Bind 127.0.0.1 by default. No UI."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .core import VERSION, CursorRPCError, fetch_plans, process, read_token, utc_now_iso

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47821


def _json_bytes(payload: dict[str, Any], status: int = 200) -> tuple[int, bytes]:
    body = json.dumps(payload, default=str).encode("utf-8")
    return status, body


class UsageHandler(BaseHTTPRequestHandler):
    server_version = f"cursor-usage-monitor/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys_stderr_line = f"{utc_now_iso()} http {self.address_string()} {fmt % args}"
        try:
            print(sys_stderr_line, flush=True)
        except Exception:
            pass

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        status, body = _json_bytes(payload, status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _truthy(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    def _check_api_token(self) -> bool:
        expected = os.environ.get("CURSOR_USAGE_API_TOKEN", "").strip()
        if not expected:
            return True
        got = self.headers.get("Authorization") or self.headers.get("X-Api-Token") or ""
        if got.lower().startswith("bearer "):
            got = got[7:]
        return got.strip() == expected

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route == "/health":
            token_present = False
            try:
                token_present = bool(read_token())
            except Exception:
                token_present = bool(os.environ.get("CURSOR_ACCESS_TOKEN", "").strip())
            self._send(
                200,
                {
                    "ok": True,
                    "service": "cursor-usage-monitor",
                    "version": VERSION,
                    "token_present": token_present,
                },
            )
            return
        if route == "/usage":
            if not self._check_api_token():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            qs = parse_qs(parsed.query)
            include_raw = self._truthy((qs.get("raw") or ["0"])[0])
            try:
                snapshot = self.server.fetch_fn()  # type: ignore[attr-defined]
            except FileNotFoundError as exc:
                self._send(401, {"ok": False, "error": str(exc)})
                return
            except CursorRPCError as exc:
                status = 401 if exc.status in {401, 403} else 502
                self._send(status, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._send(500, {"ok": False, "error": str(exc)})
                return
            body = {
                "ok": True,
                "fetched_at": snapshot.get("fetched_at"),
                "meters": snapshot.get("meters"),
                "period": {
                    "plan_period_id": snapshot.get("plan_period_id"),
                    "grok_period_id": snapshot.get("grok_period_id"),
                    "billing_cycle_start": snapshot.get("billing_cycle_start"),
                    "billing_cycle_end": snapshot.get("billing_cycle_end"),
                    "grok_resets_at": snapshot.get("grok_resets_at"),
                },
                "errors": snapshot.get("errors") or {},
            }
            if include_raw:
                body["snapshot"] = snapshot
            self._send(200, body)
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route != "/check":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._check_api_token():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
            return
        qs = parse_qs(parsed.query)
        webhook = self._truthy(body.get("webhook")) or self._truthy(
            (qs.get("webhook") or ["0"])[0]
        )
        try:
            result = self.server.process_fn(webhook=webhook)  # type: ignore[attr-defined]
        except FileNotFoundError as exc:
            self._send(401, {"ok": False, "error": str(exc)})
            return
        except CursorRPCError as exc:
            status = 401 if exc.status in {401, 403} else 502
            self._send(status, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)})
            return
        payload = result.get("payload") or {}
        self._send(
            200,
            {
                "ok": True,
                "fetched_at": (result.get("snapshot") or {}).get("fetched_at"),
                "event": payload.get("event"),
                "first_run": result.get("first_run"),
                "crossed": result.get("crossed") or [],
                "plans": payload.get("plans") or {},
                "all": payload.get("all") or {},
                "period": payload.get("period") or {},
                "webhook": result.get("webhook") or {},
                "errors": payload.get("errors") or {},
            },
        )


class UsageHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        fetch_fn: Optional[Callable[..., dict[str, Any]]] = None,
        process_fn: Optional[Callable[..., dict[str, Any]]] = None,
    ) -> None:
        super().__init__(server_address, UsageHandler)
        self.fetch_fn = fetch_fn or (lambda: fetch_plans())
        self.process_fn = process_fn or process


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    fetch_fn: Optional[Callable[..., dict[str, Any]]] = None,
    process_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> UsageHTTPServer:
    return UsageHTTPServer((host, port), fetch_fn=fetch_fn, process_fn=process_fn)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    httpd = make_server(host, port)
    bind = f"http://{host}:{port}"
    print(f"cursor-usage-monitor {VERSION} listening on {bind}", flush=True)
    print("  GET  /health", flush=True)
    print("  GET  /usage", flush=True)
    print("  POST /check   (JSON {\"webhook\": true} to POST crosses)", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "WARNING: bound off localhost. Keep this private; do not expose without auth.",
            flush=True,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        httpd.shutdown()
