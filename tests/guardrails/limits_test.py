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

"""No ceiling on file, no write; over the ceiling, a readable refusal."""

import datetime
import os
import pathlib
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from ads_mcp.guardrails import classes, guard, limits
from ads_mcp.guardrails.limits import Change

ACCOUNT = "4935403315"
OTHER = "6314904949"

EXAMPLE = textwrap.dedent(
    """
    defaults:
      max_budget_change_pct: 20
      min_days_between_budget_changes: 7
    accounts:
      "493-540-3315":
        max_daily_budget_try: 4500
        max_cpc_ceiling_try: 20
        allow_enable: true
      "6314904949":
        { max_daily_budget_try: 1500, max_cpc_ceiling_try: 45, allow_enable: false,
          max_budget_change_pct: 50 }
    """
)


class LimitsFileCase(unittest.TestCase):
    """Writes a limits file into a temp dir and points the loader at it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "limits.yaml"
        self.env = patch.dict(
            os.environ, {limits.LIMITS_PATH_ENV: str(self.path)}
        )
        self.env.start()
        limits.reset_cache()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(limits.reset_cache)

    def write(self, text=EXAMPLE):
        self.path.write_text(text)
        limits.reset_cache()


class TestLoading(LimitsFileCase):
    def test_missing_file_means_no_accounts(self):
        current = limits.current()
        self.assertEqual(current.accounts, {})
        self.assertIn("missing", current.source)
        self.assertEqual(current.defaults["max_budget_change_pct"], 20)

    def test_example_file_in_repo_parses(self):
        example = (
            pathlib.Path(limits.__file__).parent / "limits.example.yaml"
        )
        parsed = limits.Limits.load(example)
        self.assertGreaterEqual(len(parsed.accounts), 1)
        self.assertEqual(parsed.defaults["min_days_between_budget_changes"], 7)

    def test_dashes_in_account_keys_are_normalised(self):
        self.write()
        self.assertIsNotNone(limits.current().account("4935403315"))
        self.assertIsNotNone(limits.current().account("493-540-3315"))

    def test_per_account_override_beats_default(self):
        self.write()
        self.assertEqual(
            limits.current().setting(ACCOUNT, "max_budget_change_pct"), 20
        )
        self.assertEqual(
            limits.current().setting(OTHER, "max_budget_change_pct"), 50
        )

    def test_unknown_key_is_an_error(self):
        self.write("accounts:\n  '1': {max_daily_budget_usd: 5}\n")
        with self.assertRaises(ValueError) as ctx:
            limits.current()
        self.assertIn("max_daily_budget_usd", str(ctx.exception))

    def test_edits_are_picked_up_without_restart(self):
        self.write()
        self.assertIsNone(limits.current().account("111"))
        self.path.write_text(EXAMPLE + '  "111": {allow_enable: true}\n')
        os.utime(self.path, (1, 2_000_000_000))
        self.assertIsNotNone(limits.current().account("111"))

    def test_describe(self):
        self.write()
        info = limits.describe(ACCOUNT)
        self.assertTrue(info["configured"])
        self.assertEqual(info["max_daily_budget_try"], 4500)
        self.assertEqual(info["min_days_between_budget_changes"], 7)
        self.assertFalse(limits.describe("999")["configured"])


class TestAccountGate(LimitsFileCase):
    def test_unknown_account_is_refused_with_the_path(self):
        self.write()
        with self.assertRaises(limits.LimitsNotConfigured) as ctx:
            limits.require_account("999", tool="campaigns.update_campaign_budget")
        message = str(ctx.exception)
        self.assertIn("No limits are defined for account 999", message)
        self.assertIn(str(self.path), message)
        self.assertIn("campaigns.update_campaign_budget", message)

    def test_no_file_at_all_is_refused(self):
        with self.assertRaises(limits.LimitsNotConfigured):
            limits.require_account(ACCOUNT)

    def test_known_account_passes(self):
        self.write()
        self.assertEqual(limits.require_account(ACCOUNT)["allow_enable"], True)


def budget(amount, before=None, name="customers/4935403315/campaignBudgets/1",
           kind="update"):
    return Change(
        resource="campaign_budget",
        kind=kind,
        resource_name=name,
        fields={"amount_micros": int(amount * 1_000_000)},
        before={} if before is None else {"amount_micros": int(before * 1_000_000)},
    )


class TestCeilings(LimitsFileCase):
    def setUp(self):
        super().setUp()
        self.write()

    def test_budget_above_ceiling_is_refused_with_numbers(self):
        with self.assertRaises(limits.LimitExceeded) as ctx:
            limits.check_changes(ACCOUNT, [budget(6000, before=4000)])
        exc = ctx.exception
        self.assertEqual(exc.key, "max_daily_budget_try")
        self.assertEqual(exc.requested, 6000.0)
        self.assertEqual(exc.allowed, 4500.0)
        self.assertIn("Requested 6000.0, allowed 4500.0", str(exc))

    def test_forty_percent_raise_is_refused_fifteen_passes(self):
        with self.assertRaises(limits.LimitExceeded) as ctx:
            limits.check_changes(ACCOUNT, [budget(1400, before=1000)])
        self.assertEqual(ctx.exception.key, "max_budget_change_pct")
        self.assertEqual(ctx.exception.requested, "+40.0%")
        result = limits.check_changes(ACCOUNT, [budget(1150, before=1000)])
        self.assertTrue(result["ok"])
        self.assertIn("max_budget_change_pct", result["checked"])

    def test_percent_rule_is_per_account(self):
        result = limits.check_changes(
            OTHER,
            [budget(1400, before=1000, name="customers/6314904949/campaignBudgets/1")],
        )
        self.assertTrue(result["ok"])

    def test_lowering_is_never_checked_against_percent(self):
        result = limits.check_changes(ACCOUNT, [budget(100, before=1000)])
        self.assertNotIn("max_budget_change_pct", result["checked"])

    def test_too_soon_after_the_last_raise(self):
        now = datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc)
        recent = {budget(0).resource_name: now - datetime.timedelta(days=3)}
        with self.assertRaises(limits.LimitExceeded) as ctx:
            limits.check_changes(
                ACCOUNT, [budget(1100, before=1000)],
                last_budget_change=recent, now=now,
            )
        self.assertEqual(ctx.exception.key, "min_days_between_budget_changes")
        old = {budget(0).resource_name: now - datetime.timedelta(days=9)}
        self.assertTrue(
            limits.check_changes(
                ACCOUNT, [budget(1100, before=1000)],
                last_budget_change=old, now=now,
            )["ok"]
        )

    def test_new_budget_only_checked_against_the_ceiling(self):
        result = limits.check_changes(ACCOUNT, [budget(4500, kind="create")])
        self.assertEqual(result["checked"], ["max_daily_budget_try"])
        with self.assertRaises(limits.LimitExceeded):
            limits.check_changes(ACCOUNT, [budget(4500.01, kind="create")])

    def test_enable_refused_where_not_allowed(self):
        enable = Change("campaign", "update", "customers/6314904949/campaigns/5",
                        fields={"status": "ENABLED"})
        with self.assertRaises(limits.LimitExceeded) as ctx:
            limits.check_changes(OTHER, [enable])
        self.assertEqual(ctx.exception.key, "allow_enable")
        enable.resource_name = "customers/4935403315/campaigns/5"
        self.assertTrue(limits.check_changes(ACCOUNT, [enable])["ok"])

    def test_cpc_ceiling_covers_bids_and_strategy_ceilings(self):
        bid = Change("ad_group", "update", "customers/4935403315/adGroups/1",
                     fields={"cpc_bid_micros": 25_000_000})
        with self.assertRaises(limits.LimitExceeded) as ctx:
            limits.check_changes(ACCOUNT, [bid])
        self.assertEqual(ctx.exception.key, "max_cpc_ceiling_try")
        self.assertEqual(ctx.exception.requested, 25.0)
        ceiling = Change(
            "campaign", "update", "customers/4935403315/campaigns/1",
            fields={"target_spend.cpc_bid_ceiling_micros": 19_000_000},
        )
        self.assertTrue(limits.check_changes(ACCOUNT, [ceiling])["ok"])

    def test_unrelated_changes_pass_untouched(self):
        keyword = Change("ad_group_criterion", "create",
                         "customers/4935403315/adGroupCriteria/1~2",
                         fields={"keyword.text": "tus"})
        result = limits.check_changes(ACCOUNT, [keyword])
        self.assertEqual(result["checked"], [])


class TestGuardGate(LimitsFileCase):
    """The guard refuses SPEND and DESTRUCTIVE calls on unknown accounts."""

    def _guarded(self, cls, name="some_tool"):
        calls = []

        def tool(customer_id: str, amount: float = 1.0):
            calls.append((customer_id, amount))
            return {"ok": True}

        return guard.wrap(tool, namespace="t", tool_name=name, cls=cls), calls

    def test_spend_tool_refused_without_a_row(self):
        self.write()
        fn, calls = self._guarded(classes.SPEND)
        with self.assertRaises(limits.LimitsNotConfigured) as ctx:
            fn(customer_id="999", amount=5)
        self.assertIn("t.some_tool", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_destructive_tool_refused_without_a_row(self):
        self.write()
        fn, calls = self._guarded(classes.DESTRUCTIVE)
        with self.assertRaises(limits.LimitsNotConfigured):
            fn("999")
        self.assertEqual(calls, [])

    def test_spend_tool_runs_with_a_row(self):
        self.write()
        fn, calls = self._guarded(classes.SPEND)
        self.assertEqual(fn(ACCOUNT, 5)["ok"], True)
        self.assertEqual(calls, [(ACCOUNT, 5)])

    def test_read_and_protective_never_consult_the_file(self):
        for cls in (classes.READ, classes.PROTECTIVE):
            fn, calls = self._guarded(cls)
            self.assertEqual(fn(customer_id="999")["ok"], True)
        self.assertEqual(len(calls), 1)
