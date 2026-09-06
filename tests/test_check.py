from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cursor_usage_monitor.core import fetch_plans, process


def _period(**kwargs: Any) -> dict[str, Any]:
    plan = {
        "totalPercentUsed": 12.0,
        "autoPercentUsed": 6.0,
        "apiPercentUsed": 18.0,
        "includedSpend": 1200,
        "limit": 40000,
        "remaining": 38800,
        "totalSpend": 1200,
    }
    spend = {
        "totalSpend": 0,
        "individualUsed": 0,
        "individualLimit": 10000,
        "individualRemaining": 10000,
        "limitType": "user",
    }
    plan.update(kwargs.get("planUsage", {}))
    spend.update(kwargs.get("spendLimitUsage", {}))
    return {
        "billingCycleStart": kwargs.get("billingCycleStart", "1768399334000"),
        "billingCycleEnd": kwargs.get("billingCycleEnd", "1771077734000"),
        "planUsage": plan,
        "spendLimitUsage": spend,
    }


def _sand(**kwargs: Any) -> dict[str, Any]:
    data = {
        "usagePercent": 11.0,
        "hasAvailableUsage": True,
        "hasNonZeroIncludedLimit": True,
        "nextResetTimestampUtc": "1770000000000",
        "grokPlanLabel": "Grok Bot",
    }
    data.update(kwargs)
    return data


class FetchPlansTests(unittest.TestCase):
    def test_extracts_all_meters_and_on_demand_dollars(self) -> None:
        period = _period(spendLimitUsage={"individualUsed": 1234})
        sand = _sand(usagePercent=22.2)

        def rpc_fn(method: str, token: str, **_: Any) -> dict[str, Any]:
            if method == "GetCurrentPeriodUsage":
                return period
            if method == "GetSandUsageStatus":
                return sand
            raise AssertionError(method)

        snap = fetch_plans("tok", rpc_fn=rpc_fn)
        meters = snap["meters"]
        self.assertEqual(meters["grok_bot"]["value"], 22.2)
        self.assertEqual(meters["grok_bot"]["bucket"], 20)
        self.assertEqual(meters["plan_total"]["value"], 12.0)
        self.assertEqual(meters["plan_total"]["bucket"], 10)
        self.assertEqual(meters["auto"]["bucket"], 5)
        self.assertEqual(meters["api"]["bucket"], 15)
        self.assertAlmostEqual(meters["on_demand"]["value"], 12.34)
        self.assertEqual(meters["on_demand"]["bucket"], 10)
        self.assertEqual(snap["plan_period_id"], "1768399334000")
        self.assertEqual(snap["grok_period_id"], "1770000000000")

    def test_omits_grok_when_no_included_limit(self) -> None:
        def rpc_fn(method: str, token: str, **_: Any) -> dict[str, Any]:
            if method == "GetCurrentPeriodUsage":
                return _period()
            return _sand(hasNonZeroIncludedLimit=False)

        snap = fetch_plans("tok", rpc_fn=rpc_fn)
        self.assertNotIn("grok_bot", snap["meters"])
        self.assertIn("plan_total", snap["meters"])


class ProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.webhooks: list[dict[str, Any]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fetch(self, **overrides: Any) -> Any:
        period = _period()
        sand = _sand()
        if "period" in overrides:
            period = overrides["period"]
        if "sand" in overrides:
            sand = overrides["sand"]
        if "planUsage" in overrides:
            period["planUsage"].update(overrides["planUsage"])
        if "spendLimitUsage" in overrides:
            period["spendLimitUsage"].update(overrides["spendLimitUsage"])
        if "sand_update" in overrides:
            sand.update(overrides["sand_update"])

        def fetch_fn(token: Any = None) -> dict[str, Any]:
            def rpc_fn(method: str, tok: str, **_: Any) -> dict[str, Any]:
                if method == "GetCurrentPeriodUsage":
                    return period
                return sand

            return fetch_plans("tok", rpc_fn=rpc_fn)

        return fetch_fn

    def _webhook(self, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.webhooks.append(payload)
        return {"attempted": True, "ok": True, "status": 200}

    def test_first_run_is_silent_baseline(self) -> None:
        result = process(
            fetch_fn=self._fetch(),
            webhook_fn=self._webhook,
            state_path=self.state_path,
            webhook=True,
        )
        self.assertTrue(result["first_run"])
        self.assertEqual(result["crossed"], [])
        self.assertEqual(result["payload"]["event"], "baseline")
        self.assertEqual(self.webhooks, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["buckets"]["grok_bot"], 10)
        self.assertEqual(state["buckets"]["plan_total"], 10)
        self.assertEqual(state["buckets"]["auto"], 5)
        self.assertEqual(state["buckets"]["api"], 15)
        self.assertEqual(state["buckets"]["on_demand"], 0)

    def test_upward_five_percent_cross_posts_all_meters(self) -> None:
        process(
            fetch_fn=self._fetch(),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        result = process(
            fetch_fn=self._fetch(sand_update={"usagePercent": 16.2}),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        self.assertEqual(len(result["crossed"]), 1)
        self.assertEqual(result["crossed"][0]["id"], "grok_bot")
        self.assertEqual(result["crossed"][0]["from_bucket"], 10)
        self.assertEqual(result["crossed"][0]["to_bucket"], 15)
        self.assertEqual(len(self.webhooks), 1)
        payload = self.webhooks[0]
        self.assertIn("plans", payload)
        self.assertIn("all", payload)
        self.assertEqual(payload["plans"], payload["all"])
        self.assertEqual(set(payload["all"]), {"grok_bot", "plan_total", "auto", "api", "on_demand"})
        self.assertEqual(payload["crossed"][0]["id"], "grok_bot")
        self.assertEqual(payload["event"], "bucket_cross")

    def test_same_bucket_does_not_alert(self) -> None:
        process(fetch_fn=self._fetch(), webhook_fn=self._webhook, state_path=self.state_path)
        process(
            fetch_fn=self._fetch(sand_update={"usagePercent": 14.9}),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        self.assertEqual(self.webhooks, [])

    def test_downward_flicker_keeps_high_water(self) -> None:
        process(
            fetch_fn=self._fetch(sand_update={"usagePercent": 16}),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        process(
            fetch_fn=self._fetch(sand_update={"usagePercent": 8}),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        result = process(
            fetch_fn=self._fetch(sand_update={"usagePercent": 16}),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        self.assertEqual(result["crossed"], [])
        self.assertEqual(self.webhooks, [])

    def test_on_demand_ten_dollar_cross(self) -> None:
        process(fetch_fn=self._fetch(), webhook_fn=self._webhook, state_path=self.state_path)
        result = process(
            fetch_fn=self._fetch(spendLimitUsage={"individualUsed": 1050}),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        self.assertEqual(result["crossed"][0]["id"], "on_demand")
        self.assertEqual(result["crossed"][0]["from_bucket"], 0)
        self.assertEqual(result["crossed"][0]["to_bucket"], 10)
        self.assertEqual(len(self.webhooks), 1)

    def test_period_reset_is_silent_baseline(self) -> None:
        process(fetch_fn=self._fetch(), webhook_fn=self._webhook, state_path=self.state_path)
        period = _period()
        period["billingCycleStart"] = "1772000000000"
        period["planUsage"]["totalPercentUsed"] = 40.0
        result = process(
            fetch_fn=self._fetch(period=period),
            webhook_fn=self._webhook,
            state_path=self.state_path,
        )
        self.assertTrue(result["payload"]["plan_reset"])
        self.assertEqual(result["crossed"], [])
        self.assertEqual(result["payload"]["event"], "period_reset")
        self.assertEqual(self.webhooks, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["buckets"]["plan_total"], 40)

    def test_webhook_optional_off(self) -> None:
        process(fetch_fn=self._fetch(), webhook_fn=self._webhook, state_path=self.state_path)
        result = process(
            fetch_fn=self._fetch(sand_update={"usagePercent": 40}),
            webhook_fn=self._webhook,
            state_path=self.state_path,
            webhook=False,
        )
        self.assertTrue(result["crossed"])
        self.assertEqual(self.webhooks, [])


class AuthReadTests(unittest.TestCase):
    def test_env_token_wins(self) -> None:
        from cursor_usage_monitor.core import read_token

        with patch.dict("os.environ", {"CURSOR_ACCESS_TOKEN": "user::abc.def.ghi"}):
            self.assertEqual(read_token(), "abc.def.ghi")


if __name__ == "__main__":
    unittest.main()
