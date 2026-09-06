from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from typing import Any

from cursor_usage_monitor.server import make_server


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "fetched_at": "2026-09-06T00:00:00+00:00",
            "plan_period_id": "1",
            "grok_period_id": "2",
            "billing_cycle_start": "1",
            "billing_cycle_end": "2",
            "grok_resets_at": "2",
            "meters": {
                "grok_bot": {
                    "id": "grok_bot",
                    "name": "Grok Bot",
                    "value": 11.0,
                    "unit": "percent",
                    "bucket": 10,
                }
            },
            "errors": {},
        }
        self.check_calls: list[dict[str, Any]] = []

        def fetch_fn() -> dict[str, Any]:
            return self.snapshot

        def process_fn(**kwargs: Any) -> dict[str, Any]:
            self.check_calls.append(kwargs)
            return {
                "snapshot": self.snapshot,
                "payload": {
                    "event": "no_change",
                    "plans": self.snapshot["meters"],
                    "all": self.snapshot["meters"],
                    "crossed": [],
                    "period": {},
                    "errors": {},
                },
                "crossed": [],
                "webhook": {"attempted": bool(kwargs.get("webhook")), "ok": False},
                "first_run": False,
            }

        self.httpd = make_server("127.0.0.1", 0, fetch_fn=fetch_fn, process_fn=process_fn)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _conn(self) -> HTTPConnection:
        return HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_health(self) -> None:
        conn = self._conn()
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "cursor-usage-monitor")

    def test_usage(self) -> None:
        conn = self._conn()
        conn.request("GET", "/usage")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("grok_bot", body["meters"])

    def test_check_webhook_optional_default_off(self) -> None:
        conn = self._conn()
        conn.request("POST", "/check", body="{}", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.check_calls[-1]["webhook"], False)
        self.assertIn("all", body)
        self.assertIn("plans", body)

    def test_check_webhook_on(self) -> None:
        conn = self._conn()
        conn.request(
            "POST",
            "/check",
            body=json.dumps({"webhook": True}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(self.check_calls[-1]["webhook"], True)

    def test_unknown_route(self) -> None:
        conn = self._conn()
        conn.request("GET", "/nope")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        conn.close()


if __name__ == "__main__":
    unittest.main()
