# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Every write leaves a line: local JSONL, both time zones, never stdout."""

import datetime
import io
import json
import os
import pathlib
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ads_mcp.guardrails import audit_log, classes, guard, limits, plans
from ads_mcp.guardrails.install import registered_tools
from tests.fakes import FakeAds, row

CID = "4935403315"


def _event(result="applied", tool="campaigns.update_campaign_budget",
           amount="1150000000", when="2026-09-18T10:00:00+00:00", plan_id="plan-x"):
    return {
        "time_utc": when,
        "customer_id": CID,
        "tool": tool,
        "tool_class": classes.SPEND,
        "plan_id": plan_id,
        "actor": "tester",
        "arguments": {"customer_id": CID, "budget_id": "55", "daily_amount": 1150},
        "before": [{"customer_id": CID, "query": "SELECT ...", "rows": [{"campaign_budget": {"id": "55"}}]}],
        "after": [{"service": "CampaignBudgetService", "method": "mutate_campaign_budgets",
                   "customer_id": CID, "results": [],
                   "operations": [{"kind": "update", "resource": "campaign_budget",
                                   "resource_name": f"customers/{CID}/campaignBudgets/55",
                                   "fields": {"amount_micros": amount}, "mask": ["amount_micros"]}]}],
        "result": result,
        "error": None,
        "output": {"verified": [{"campaign_budget": {"amount_micros": amount}}]},
        "validate_only": False,
    }


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name) / "audit"
        self.env = patch.dict(os.environ, {audit_log.AUDIT_DIR_ENV: str(self.dir),
                                           audit_log.GCS_BUCKET_ENV: ""})
        self.env.start()
        self.addCleanup(self.env.stop)


class TestRecord(AuditCase):
    def test_line_has_both_times_and_lands_in_the_month_file(self):
        out = io.StringIO()
        with redirect_stdout(out):
            entry = audit_log.record(_event())
        self.assertEqual(out.getvalue(), "")  # stdout is the MCP channel
        self.assertEqual(entry["time_utc"], "2026-09-18T10:00:00+00:00")
        self.assertEqual(entry["time_istanbul"], "2026-09-18T13:00:00+03:00")
        path = self.dir / "2026-09.jsonl"
        self.assertTrue(path.exists())
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        stored = json.loads(lines[0])
        self.assertEqual(stored["tool"], "campaigns.update_campaign_budget")
        self.assertEqual(stored["plan_id"], "plan-x")
        self.assertEqual(stored["after"][0]["operations"][0]["fields"]["amount_micros"], "1150000000")

    def test_big_output_is_capped(self):
        event = _event()
        event["output"] = {"verified": [{"row": i, "text": "x" * 100} for i in range(500)], "note": "n"}
        entry = audit_log.record(event)
        self.assertTrue(entry["output"]["truncated"])
        self.assertEqual(len(entry["output"]["verified"]), 5)
        self.assertEqual(entry["output"]["keys"], ["note", "verified"])

    def test_manual_entry(self):
        entry = audit_log.record_manual(
            "493-540-3315", "Panelden bütçe 1000'e çekildi, müşteri isteği.",
            resource_type="CAMPAIGN_BUDGET", campaign_id="42", when="2026-09-17",
            actor="me",
        )
        self.assertEqual(entry["source"], "manual")
        self.assertEqual(entry["customer_id"], CID)
        self.assertEqual(entry["time_utc"], "2026-09-17T00:00:00+00:00")
        self.assertEqual(entry["arguments"]["campaign_id"], "42")
        self.assertIn("müşteri", entry["note"])


class TestRead(AuditCase):
    def setUp(self):
        super().setUp()
        audit_log.record(_event(when="2026-09-01T10:00:00+00:00", plan_id="p1"))
        audit_log.record(_event(when="2026-09-10T10:00:00+00:00", plan_id="p2", result="error"))
        audit_log.record(_event(when="2026-08-20T10:00:00+00:00", plan_id="p0"))
        other = _event(when="2026-09-11T10:00:00+00:00", plan_id="p3")
        other["customer_id"] = "1111111111"
        audit_log.record(other)

    def test_filters_by_account_window_and_tool(self):
        entries = audit_log.read(CID, since="2026-09-01", until="2026-09-30")
        self.assertEqual([e["plan_id"] for e in entries], ["p2", "p1"])
        entries = audit_log.read(CID, since="2026-08-01", until="2026-09-30")
        self.assertEqual([e["plan_id"] for e in entries], ["p2", "p1", "p0"])
        self.assertEqual(audit_log.read(CID, since="2026-08-01", until="2026-09-30", tool="nope"), [])
        self.assertEqual(len(audit_log.read(None, since="2026-08-01", until="2026-09-30")), 4)

    def test_summary_is_compact(self):
        entry = audit_log.read(CID, since="2026-09-01", until="2026-09-30")[0]
        summary = audit_log.summarize(entry)
        self.assertNotIn("before", summary)
        self.assertEqual(summary["changes"][0]["resource"], "campaign_budget")
        self.assertEqual(summary["result"], "error")

    def test_last_budget_changes_only_counts_applied_writes(self):
        now = datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc)
        found = audit_log.last_budget_changes(CID, now=now)
        name = f"customers/{CID}/campaignBudgets/55"
        self.assertEqual(found[name].isoformat(), "2026-09-01T10:00:00+00:00")


class TestWiredIntoTheFlow(AuditCase):
    """A real tool through the guard writes a line, and the limits read it."""

    def setUp(self):
        super().setUp()
        import ads_mcp.coordinator  # noqa: F401

        root = pathlib.Path(self.tmp.name)
        (root / "limits.yaml").write_text(textwrap.dedent(
            f"""
            defaults: {{max_budget_change_pct: 20, min_days_between_budget_changes: 7}}
            accounts:
              "{CID}": {{max_daily_budget_try: 4500, max_cpc_ceiling_try: 20, allow_enable: true}}
            """
        ))
        self.more_env = patch.dict(os.environ, {
            limits.LIMITS_PATH_ENV: str(root / "limits.yaml"),
            plans.PLAN_DIR_ENV: str(root / "plans"),
            guard.REQUIRE_DRY_RUN_ENV: "",
        })
        self.more_env.start()
        self.addCleanup(self.more_env.stop)
        limits.reset_cache()
        self.addCleanup(limits.reset_cache)

    @staticmethod
    def _tool(namespace, name):
        import importlib

        server = getattr(importlib.import_module(f"ads_mcp.tools.{namespace}"), f"{namespace}_mcp")
        return next(t.fn for t in registered_tools(server) if t.name == name)

    def test_listener_is_installed_once(self):
        self.assertEqual(guard.WRITE_LISTENERS.count(audit_log.record), 1)
        self.assertIs(guard.BUDGET_HISTORY, audit_log.last_budget_changes)

    def test_plan_apply_and_direct_write_both_log(self):
        update = self._tool("campaigns", "update_campaign_budget")
        apply_plan = self._tool("plans", "apply_plan")
        fake = FakeAds().on("FROM campaign_budget", [row(campaign_budget={"id": 55, "amount_micros": 1_000_000_000})])
        with fake.installed():
            plan = update(CID, "55", 1100)
            apply_plan(plan["plan_id"])
            update(CID, "55", 900)  # a decrease: written directly
        entries = audit_log.read(CID)
        self.assertEqual([e["result"] for e in entries], ["applied", "applied"])
        self.assertEqual([e["tool_class"] for e in entries], [classes.PROTECTIVE, classes.SPEND])
        self.assertEqual(entries[1]["plan_id"], plan["plan_id"])
        self.assertIsNone(entries[0]["plan_id"])
        self.assertIn("time_istanbul", entries[0])

    def test_a_second_raise_within_seven_days_is_refused(self):
        update = self._tool("campaigns", "update_campaign_budget")
        apply_plan = self._tool("plans", "apply_plan")
        fake = FakeAds().on("FROM campaign_budget", [row(campaign_budget={"id": 55, "amount_micros": 1_000_000_000})])
        with fake.installed():
            plan = update(CID, "55", 1100)
            apply_plan(plan["plan_id"])
            with self.assertRaises(limits.LimitExceeded) as ctx:
                update(CID, "55", 1150)
        self.assertEqual(ctx.exception.key, "min_days_between_budget_changes")

    def test_change_log_tool_and_manual_entry(self):
        get_log = self._tool("audit", "get_change_log")
        manual = self._tool("audit", "log_manual_change")
        with FakeAds().installed():
            written = manual(CID, "Panelden sitelink eklendi", resource_type="asset", campaign_id="42")
            report = get_log(CID)
        self.assertEqual(written["source"], "manual")
        self.assertEqual(written["arguments"]["resource_type"], "ASSET")
        self.assertEqual(report["by_result"], {"recorded": 1})
        self.assertEqual(report["entries"][0]["note"], "Panelden sitelink eklendi")
        self.assertEqual(report["entries"][0]["tool"], "manual")
