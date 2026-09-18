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

"""The whole flow through real tools: plan, refuse, write, apply, audit."""

import os
import pathlib
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from ads_mcp.guardrails import classes, guard, limits, plans
from ads_mcp.guardrails.install import registered_tools
from ads_mcp.guardrails.session import PlanPreconditionFailed
from tests.fakes import FakeAds, row

CID = "4935403315"
NO_LIMITS = "6440518752"
LOCKED = "6314904949"

LIMITS = textwrap.dedent(
    f"""
    defaults:
      max_budget_change_pct: 20
      min_days_between_budget_changes: 7
    accounts:
      "{CID}": {{max_daily_budget_try: 4500, max_cpc_ceiling_try: 20, allow_enable: true}}
      "{LOCKED}": {{max_daily_budget_try: 1500, max_cpc_ceiling_try: 45, allow_enable: false}}
    """
)


def guarded(namespace, name):
    import importlib

    module = importlib.import_module(f"ads_mcp.tools.{namespace}")
    server = getattr(module, f"{namespace}_mcp")
    for tool in registered_tools(server):
        if tool.name == name:
            return tool.fn
    raise LookupError(name)


def budget_rows(amount):
    return [row(campaign_budget={"id": 55, "name": "B", "amount_micros": int(amount * 1_000_000)})]


def campaign_rows(status, amount=1000):
    return [row(campaign={"id": 42, "name": "Brand", "status": status},
                campaign_budget={"amount_micros": int(amount * 1_000_000)})]


class FlowCase(unittest.TestCase):
    def setUp(self):
        import ads_mcp.coordinator  # noqa: F401 - mounts and guards every tool

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        (root / "limits.yaml").write_text(LIMITS)
        self.env = patch.dict(os.environ, {
            limits.LIMITS_PATH_ENV: str(root / "limits.yaml"),
            plans.PLAN_DIR_ENV: str(root / "plans"),
            guard.REQUIRE_DRY_RUN_ENV: "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        limits.reset_cache()
        self.addCleanup(limits.reset_cache)
        self.events = []
        guard.WRITE_LISTENERS.append(self.events.append)
        self.addCleanup(guard.WRITE_LISTENERS.remove, self.events.append)

    def plan_files(self):
        directory = pathlib.Path(os.environ[plans.PLAN_DIR_ENV])
        return sorted(directory.glob("plan-*.json")) if directory.exists() else []


class TestBudgetFlow(FlowCase):
    def test_fifteen_percent_raise_plans_then_applies(self):
        update = guarded("campaigns", "update_campaign_budget")
        fake = FakeAds().on("FROM campaign_budget", budget_rows(1000))
        with fake.installed():
            plan = update(CID, "55", 1150)
        self.assertEqual(fake.calls, [])
        self.assertEqual(plan["status"], plans.PENDING)
        self.assertEqual(plan["class"], classes.SPEND)
        self.assertEqual(plan["tool"], "campaigns.update_campaign_budget")
        change = plan["changes"][0]
        self.assertEqual(change["before"]["amount_micros"], 1_000_000_000)
        self.assertEqual(change["after"]["amount_micros"], 1_150_000_000)
        self.assertEqual(plan["impact"]["daily_delta"], 150.0)
        self.assertEqual(plan["impact"]["monthly_delta"], 4560.0)
        self.assertIn("max_budget_change_pct", plan["limits"]["checked"])
        self.assertIn("back to {'amount_micros': 1000000000}", plan["rollback"][0])
        self.assertEqual(len(self.plan_files()), 1)

        apply_plan = guarded("plans", "apply_plan")
        with fake.installed():
            result = apply_plan(plan["plan_id"])
        self.assertEqual(result["status"], plans.APPLIED)
        op = fake.calls_to("CampaignBudgetService.mutate_campaign_budgets")[0]["operations"][0]
        self.assertEqual(op.update.amount_micros, 1_150_000_000)
        self.assertEqual(result["actual"]["new_daily_amount"], 1150)
        self.assertEqual(result["actual"]["old_daily_amount"], 1000.0)
        self.assertTrue(result["actual"]["verified"])
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["plan_id"], plan["plan_id"])
        self.assertEqual(event["customer_id"], CID)
        self.assertEqual(event["after"][0]["operations"][0]["fields"]["amount_micros"], "1150000000")

    def test_forty_percent_raise_is_refused_and_leaves_no_plan(self):
        update = guarded("campaigns", "update_campaign_budget")
        fake = FakeAds().on("FROM campaign_budget", budget_rows(1000))
        with fake.installed():
            with self.assertRaises(limits.LimitExceeded) as ctx:
                update(CID, "55", 1400)
        self.assertEqual(ctx.exception.key, "max_budget_change_pct")
        self.assertIn("Requested +40.0%", str(ctx.exception))
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.plan_files(), [])

    def test_ceiling_refusal_names_requested_and_allowed(self):
        update = guarded("campaigns", "update_campaign_budget")
        fake = FakeAds().on("FROM campaign_budget", budget_rows(4400))
        with fake.installed():
            with self.assertRaises(limits.LimitExceeded) as ctx:
                update(CID, "55", 4600)
        self.assertEqual(ctx.exception.key, "max_daily_budget_try")
        self.assertIn("Requested 4600.0, allowed 4500.0", str(ctx.exception))

    def test_lowering_writes_immediately_without_a_plan(self):
        update = guarded("campaigns", "update_campaign_budget")
        fake = FakeAds().on("FROM campaign_budget", budget_rows(1000))
        with fake.installed():
            result = update(CID, "55", 800)
        self.assertEqual(result["new_daily_amount"], 800)
        self.assertEqual(len(fake.calls_to("CampaignBudgetService.mutate_campaign_budgets")), 1)
        self.assertEqual(self.plan_files(), [])
        self.assertEqual(self.events[0]["tool_class"], classes.PROTECTIVE)
        self.assertIsNone(self.events[0]["plan_id"])
        self.assertEqual(self.events[0]["before"][0]["rows"][0]["campaign_budget"]["amount_micros"], "1000000000")

    def test_lowering_works_even_without_a_limits_row(self):
        # The account gate is checked first, so even a decrease needs a row.
        update = guarded("campaigns", "update_campaign_budget")
        fake = FakeAds().on("FROM campaign_budget", budget_rows(1000))
        with fake.installed():
            with self.assertRaises(limits.LimitsNotConfigured):
                update(NO_LIMITS, "55", 800)
        self.assertEqual(fake.reads, [])

    def test_unknown_account_is_refused_before_any_read(self):
        update = guarded("campaigns", "update_campaign_budget")
        fake = FakeAds().on("FROM campaign_budget", budget_rows(1000))
        with fake.installed():
            with self.assertRaises(limits.LimitsNotConfigured) as ctx:
                update(NO_LIMITS, "55", 1100)
        self.assertIn(f"No limits are defined for account {NO_LIMITS}", str(ctx.exception))
        self.assertEqual(fake.reads, [])

    def test_panel_change_after_planning_fails_the_precondition(self):
        update = guarded("campaigns", "update_campaign_budget")
        apply_plan = guarded("plans", "apply_plan")
        with FakeAds().on("FROM campaign_budget", budget_rows(1000)).installed():
            plan = update(CID, "55", 1100)
        changed = FakeAds().on("FROM campaign_budget", budget_rows(1200))
        with changed.installed():
            with self.assertRaises(PlanPreconditionFailed) as ctx:
                apply_plan(plan["plan_id"])
        self.assertIn("account changed", str(ctx.exception))
        self.assertEqual(changed.calls, [])
        self.assertEqual(plans.get(plan["plan_id"])["status"], plans.STALE)

    def test_new_budget_is_planned_and_gets_a_real_id_on_apply(self):
        create = guarded("campaigns", "create_campaign_budget")
        apply_plan = guarded("plans", "apply_plan")
        fake = FakeAds().on("FROM campaign_budget", budget_rows(300))
        with fake.installed():
            plan = create(CID, "Eylul", 300)
            self.assertEqual(fake.calls, [])
            self.assertEqual(plan["changes"][0]["kind"], "create")
            self.assertEqual(plan["impact"]["details"][0]["what"], "new budget")
            result = apply_plan(plan["plan_id"])
        self.assertEqual(result["status"], plans.APPLIED)
        self.assertEqual(result["actual"]["budget_id"], "1001")
        self.assertEqual(result["actual"]["resource_name"], "customers/0/campaignBudgets/1001")


class TestStatusFlow(FlowCase):
    def test_pause_needs_no_limits_row_and_no_plan(self):
        pause = guarded("campaigns", "pause_campaign")
        fake = FakeAds().on("FROM campaign WHERE", campaign_rows("ENABLED"))
        with fake.installed():
            result = pause(NO_LIMITS, "42")
        self.assertEqual(result["new_status"], "PAUSED")
        self.assertEqual(len(fake.calls_to("CampaignService.mutate_campaigns")), 1)
        self.assertEqual(self.plan_files(), [])
        self.assertEqual(self.events[0]["tool_class"], classes.PROTECTIVE)
        self.assertEqual(self.events[0]["tool"], "campaigns.pause_campaign")

    def test_enable_plans_with_the_money_and_applies(self):
        enable = guarded("campaigns", "enable_campaign")
        apply_plan = guarded("plans", "apply_plan")
        fake = FakeAds().on("FROM campaign WHERE", campaign_rows("PAUSED", 500))
        with fake.installed():
            plan = enable(CID, "42")  # no confirm_spend: the plan is the confirmation
        self.assertEqual(fake.calls, [])
        self.assertEqual(plan["impact"]["daily_delta"], 500.0)
        self.assertEqual(plan["impact"]["monthly_delta"], 15200.0)
        self.assertEqual(plan["changes"][0]["after"], {"status": "ENABLED"})
        self.assertEqual(plan["changes"][0]["before"]["status"], "PAUSED")
        with fake.installed():
            result = apply_plan(plan["plan_id"])
        op = fake.calls_to("CampaignService.mutate_campaigns")[0]["operations"][0]
        self.assertEqual(op.update.status.name, "ENABLED")
        self.assertEqual(result["actual"]["new_status"], "ENABLED")

    def test_enable_refused_where_the_file_forbids_it(self):
        enable = guarded("campaigns", "enable_campaign")
        fake = FakeAds().on("FROM campaign WHERE", campaign_rows("PAUSED", 500))
        with fake.installed():
            with self.assertRaises(limits.LimitExceeded) as ctx:
                enable(LOCKED, "42")
        self.assertEqual(ctx.exception.key, "allow_enable")
        self.assertEqual(fake.calls, [])

    def test_legacy_status_tool_always_plans(self):
        legacy = guarded("campaigns", "set_campaign_status")
        fake = FakeAds().on("FROM campaign WHERE", campaign_rows("ENABLED"))
        with fake.installed():
            plan = legacy(CID, "42", "PAUSED")
        self.assertEqual(plan["status"], plans.PENDING)
        self.assertEqual(fake.calls, [])
        with fake.installed():
            removal = legacy(CID, "42", "REMOVED")
        self.assertEqual(removal["class"], classes.DESTRUCTIVE)


class TestDestructiveFlow(FlowCase):
    def test_remove_keywords_needs_confirm_destructive_at_apply(self):
        remove = guarded("adgroups", "remove_keywords")
        apply_plan = guarded("plans", "apply_plan")
        fake = FakeAds()
        with fake.installed():
            plan = remove(CID, "7", ["1", "2"])  # no confirm_remove needed: the plan gates it
        self.assertEqual(plan["class"], classes.DESTRUCTIVE)
        self.assertEqual(len(plan["changes"]), 2)
        self.assertTrue(all(c["kind"] == "remove" for c in plan["changes"]))
        self.assertIn("confirm_destructive=True", plan["next_step"])
        with fake.installed():
            with self.assertRaises(plans.DestructiveConfirmationRequired):
                apply_plan(plan["plan_id"])
            self.assertEqual(fake.calls, [])
            result = apply_plan(plan["plan_id"], confirm_destructive=True)
        self.assertEqual(result["status"], plans.APPLIED)
        ops = fake.calls_to("AdGroupCriterionService.mutate_ad_group_criteria")[0]["operations"]
        self.assertEqual([o.remove for o in ops],
                         [f"customers/{CID}/adGroupCriteria/7~1", f"customers/{CID}/adGroupCriteria/7~2"])


class TestKeywordDirection(FlowCase):
    def _fake(self, bid_micros, match="PHRASE"):
        return FakeAds().on(
            "FROM ad_group_criterion",
            [row(ad_group_criterion={"criterion_id": 3, "status": "ENABLED",
                                     "cpc_bid_micros": bid_micros,
                                     "keyword": {"text": "tus kursu", "match_type": match}})],
        )

    def test_lower_bid_is_written_directly(self):
        update = guarded("adgroups", "update_keyword")
        fake = self._fake(5_000_000)
        with fake.installed():
            result = update(CID, "7", "3", cpc_bid=4)
        self.assertEqual(result["new_cpc_bid"], 4)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.plan_files(), [])

    def test_higher_bid_is_planned_and_checked_against_the_cpc_ceiling(self):
        update = guarded("adgroups", "update_keyword")
        with self._fake(5_000_000).installed():
            plan = update(CID, "7", "3", cpc_bid=6)
            self.assertEqual(plan["status"], plans.PENDING)
            with self.assertRaises(limits.LimitExceeded) as ctx:
                update(CID, "7", "3", cpc_bid=25)
        self.assertEqual(ctx.exception.key, "max_cpc_ceiling_try")

    def test_match_type_change_is_destructive(self):
        update = guarded("adgroups", "update_keyword")
        with self._fake(5_000_000).installed():
            plan = update(CID, "7", "3", match_type="EXACT")
        self.assertEqual(plan["class"], classes.DESTRUCTIVE)
        kinds = [c["kind"] for c in plan["changes"]]
        self.assertEqual(kinds, ["remove", "create"])
        self.assertIn("Not reversible", plan["rollback"][0])


class TestNoWriteAndDryRun(FlowCase):
    def test_spend_tool_that_writes_nothing_answers_directly(self):
        add_call = guarded("extensions", "add_call_extension")
        fake = FakeAds().on(
            "FROM campaign_asset",
            [row(campaign_asset={"field_type": "CALL", "status": "ENABLED",
                                 "resource_name": f"customers/{CID}/campaignAssets/42~1~CALL"},
                 asset={"id": 1, "name": "Call", "call_asset": {"phone_number": "5551234"}})],
        )
        with fake.installed():
            result = add_call(CID, "42", "0555 123 45 67")
        self.assertFalse(result["created"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.plan_files(), [])

    def test_require_dry_run_validates_protective_writes_first(self):
        pause = guarded("campaigns", "pause_campaign")
        fake = FakeAds().on("FROM campaign WHERE", campaign_rows("ENABLED"))
        with patch.dict(os.environ, {guard.REQUIRE_DRY_RUN_ENV: "true"}):
            with fake.installed():
                pause(CID, "42")
        calls = fake.calls_to("CampaignService.mutate_campaigns")
        self.assertEqual([bool(c.get("validate_only")) for c in calls], [True, False])

    def test_plan_tools_are_listed_with_their_annotations(self):
        import asyncio
        from ads_mcp.coordinator import mcp

        async def names():
            return {t.name: t.annotations for t in await mcp.list_tools()}

        tools = asyncio.run(names())
        self.assertTrue(tools["plans_apply_plan"].destructive_hint)
        self.assertTrue(tools["plans_get_plan"].read_only_hint)
        self.assertFalse(tools["plans_discard_plan"].destructive_hint)
