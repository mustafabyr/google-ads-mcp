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

"""A plan applies once, in time, after checking the world still matches."""

import datetime
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from google.protobuf import field_mask_pb2

import ads_mcp.utils as utils
from ads_mcp.guardrails import audit_log, classes, guard, plans
from ads_mcp.guardrails.session import ReadRecord
from tests.fakes import FakeAds, offline_client, row

CID = "1234567890"
KEY = "t.fake_budget_tool"
QUERY = "SELECT campaign_budget.id, campaign_budget.amount_micros FROM campaign_budget WHERE campaign_budget.id = 55"


def fake_budget_tool(customer_id: str, budget_id: str, daily_amount: float,
                     confirm_spend: bool = False):
    """Reads a budget, writes it, reads it back -- the shape of a real tool."""
    if not confirm_spend:
        raise RuntimeError("confirmation expected to be injected")
    ga = utils.get_googleads_service("GoogleAdsService")
    before = [r for b in ga.search_stream(customer_id=customer_id, query=QUERY) for r in b.results]
    client = utils.get_googleads_client()
    op = client.get_type("CampaignBudgetOperation")
    op.update.resource_name = f"customers/{customer_id}/campaignBudgets/{budget_id}"
    op.update.amount_micros = int(daily_amount * 1_000_000)
    client.copy_from(op.update_mask, field_mask_pb2.FieldMask(paths=["amount_micros"]))
    utils.get_googleads_service("CampaignBudgetService").mutate_campaign_budgets(
        customer_id=customer_id, operations=[op])
    after = [r for b in ga.search_stream(customer_id=customer_id, query=QUERY) for r in b.results]
    return {"before_rows": len(before), "verified": len(after), "new": daily_amount}


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            plans.PLAN_DIR_ENV: str(pathlib.Path(self.tmp.name) / "plans"),
            audit_log.AUDIT_DIR_ENV: str(pathlib.Path(self.tmp.name) / "audit"),
            guard.REQUIRE_DRY_RUN_ENV: "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        guard.ORIGINALS[KEY] = fake_budget_tool
        self.addCleanup(guard.ORIGINALS.pop, KEY, None)
        self.events = []
        guard.WRITE_LISTENERS.append(self.events.append)
        self.addCleanup(guard.WRITE_LISTENERS.remove, self.events.append)
        self.fake = FakeAds().on("FROM campaign_budget", [row(campaign_budget={"id": 55, "amount_micros": 5_000_000})])

    def make_plan(self, effective=classes.SPEND, amount=6.0):
        reads = [ReadRecord(CID, QUERY, [{"campaign_budget": {"id": "55", "amount_micros": "5000000"}}])]
        return plans.create(
            tool=KEY, static_class=classes.SPEND, effective_class=effective,
            customer_id=CID,
            arguments={"customer_id": CID, "budget_id": "55", "daily_amount": amount},
            reads=reads, writes=[], changes=[{"resource": "campaign_budget"}],
            impact={"daily_delta": 1.0}, limits={"ok": True}, rollback=["set it back"],
            actor="tester",
        )


class TestStore(PlanCase):
    def test_round_trip_and_summary(self):
        plan = self.make_plan()
        loaded = plans.store().load(plan.plan_id)
        self.assertEqual(loaded.arguments["daily_amount"], 6.0)
        self.assertEqual(loaded.status, plans.PENDING)
        summary = loaded.summary()
        self.assertIn("plans_apply_plan", summary["next_step"])
        self.assertNotIn("confirm_destructive", summary["next_step"])
        self.assertEqual(summary["class"], classes.SPEND)

    def test_destructive_summary_asks_for_confirmation(self):
        plan = self.make_plan(effective=classes.DESTRUCTIVE)
        self.assertIn("confirm_destructive=True", plan.summary()["next_step"])

    def test_unknown_and_bad_ids(self):
        with self.assertRaises(plans.PlanNotFound):
            plans.get("plan-nope")
        with self.assertRaises(plans.PlanNotFound):
            plans.store().load("../etc/passwd")

    def test_list_and_discard(self):
        plan = self.make_plan()
        listed = plans.list_plans(customer_id="123-456-7890")
        self.assertEqual([p["plan_id"] for p in listed], [plan.plan_id])
        self.assertEqual(plans.list_plans(customer_id="999"), [])
        self.assertEqual(plans.discard(plan.plan_id)["status"], plans.DISCARDED)
        self.assertEqual(plans.list_plans(), [])
        self.assertEqual(len(plans.list_plans(include_finished=True)), 1)
        with self.assertRaises(plans.PlanNotOpen):
            plans.apply(plan.plan_id)

    def test_expiry(self):
        plan = self.make_plan()
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        plan.expires_at = past.isoformat(timespec="seconds")
        plans.store().save(plan)
        with self.assertRaises(plans.PlanExpired):
            plans.apply(plan.plan_id)
        self.assertEqual(plans.get(plan.plan_id)["status"], plans.EXPIRED)

    def test_purge_removes_old_finished_plans(self):
        plan = self.make_plan()
        plan.status = plans.APPLIED
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=8)
        plan.created_at = old.isoformat(timespec="seconds")
        plans.store().save(plan)
        self.assertEqual(plans.store().purge(), 1)
        self.assertEqual(plans.store().all(), [])


class TestApply(PlanCase):
    def test_apply_writes_once_and_records(self):
        plan = self.make_plan()
        with self.fake.installed():
            result = plans.apply(plan.plan_id)
        self.assertEqual(result["status"], plans.APPLIED)
        self.assertEqual(result["actual"]["new"], 6.0)
        self.assertEqual(result["precondition"], "1 read(s) matched the plan")
        call = self.fake.calls_to("CampaignBudgetService.mutate_campaign_budgets")[0]
        self.assertEqual(call["operations"][0].update.amount_micros, 6_000_000)
        self.assertEqual(plans.get(plan.plan_id)["status"], plans.APPLIED)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["plan_id"], plan.plan_id)
        self.assertEqual(self.events[0]["result"], "applied")
        self.assertEqual(self.events[0]["tool_class"], classes.SPEND)
        with self.fake.installed():
            with self.assertRaises(plans.PlanNotOpen) as ctx:
                plans.apply(plan.plan_id)
        self.assertIn("already applied", str(ctx.exception))
        self.assertEqual(len(self.fake.calls), 1)

    def test_changed_account_makes_the_plan_stale(self):
        plan = self.make_plan()
        self.fake = FakeAds().on("FROM campaign_budget", [row(campaign_budget={"id": 55, "amount_micros": 9_000_000})])
        with self.fake.installed():
            with self.assertRaises(plans.PlanPreconditionFailed):
                plans.apply(plan.plan_id)
        self.assertEqual(self.fake.calls, [])
        stored = plans.get(plan.plan_id)
        self.assertEqual(stored["status"], plans.STALE)
        self.assertIn("account changed", stored["error"])
        self.assertEqual(self.events[0]["result"], "precondition_failed")

    def test_destructive_needs_confirmation(self):
        plan = self.make_plan(effective=classes.DESTRUCTIVE)
        with self.fake.installed():
            with self.assertRaises(plans.DestructiveConfirmationRequired):
                plans.apply(plan.plan_id)
            result = plans.apply(plan.plan_id, confirm_destructive=True)
        self.assertEqual(result["status"], plans.APPLIED)

    def test_validate_only_does_not_consume_the_plan(self):
        plan = self.make_plan()
        with self.fake.installed():
            first = plans.apply(plan.plan_id, validate_only=True)
            second = plans.apply(plan.plan_id, validate_only=True)
        self.assertEqual(first["status"], plans.VALIDATED)
        self.assertEqual(second["status"], plans.VALIDATED)
        calls = self.fake.calls_to("CampaignBudgetService.mutate_campaign_budgets")
        self.assertEqual([c["validate_only"] for c in calls], [True, True])
        self.assertEqual(plans.get(plan.plan_id)["status"], plans.VALIDATED)

    def test_require_dry_run_validates_first_then_writes(self):
        plan = self.make_plan()
        with patch.dict(os.environ, {guard.REQUIRE_DRY_RUN_ENV: "true"}):
            with self.fake.installed():
                first = plans.apply(plan.plan_id)
                second = plans.apply(plan.plan_id)
        self.assertEqual(first["status"], plans.VALIDATED)
        self.assertIn("again", first["note"])
        self.assertEqual(second["status"], plans.APPLIED)
        calls = self.fake.calls_to("CampaignBudgetService.mutate_campaign_budgets")
        self.assertEqual([bool(c.get("validate_only")) for c in calls], [True, False])

    def test_api_failure_marks_the_plan_failed(self):
        plan = self.make_plan()
        self.fake.fail("CampaignBudgetService.mutate_campaign_budgets", RuntimeError("boom"))
        with self.fake.installed():
            with self.assertRaises(RuntimeError):
                plans.apply(plan.plan_id)
        self.assertEqual(plans.get(plan.plan_id)["status"], plans.FAILED)
        self.assertEqual(self.events[0]["result"], "error")

    def test_lock_refuses_a_concurrent_apply(self):
        plan = self.make_plan()
        with plans.store().lock(plan.plan_id):
            with self.fake.installed():
                with self.assertRaises(plans.PlanNotOpen):
                    plans.apply(plan.plan_id)

    def test_missing_tool_is_reported(self):
        plan = self.make_plan()
        plan.tool = "t.gone"
        plans.store().save(plan)
        with self.fake.installed():
            with self.assertRaises(plans.PlanError) as ctx:
                plans.apply(plan.plan_id)
        self.assertIn("not mounted", str(ctx.exception))
