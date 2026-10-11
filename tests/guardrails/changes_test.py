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

"""Changes, their direction, and what they cost."""

import unittest

from ads_mcp.guardrails import changes as C
from ads_mcp.guardrails import classes, direction, impact
from ads_mcp.guardrails.limits import Change

CID = "1234567890"
BUDGET = f"customers/{CID}/campaignBudgets/55"
CAMPAIGN = f"customers/{CID}/campaigns/9"


def _op(resource, kind, name, fields, mask=None):
    return {"resource": resource, "kind": kind, "resource_name": name,
            "fields": fields, "mask": mask or []}


ROWS = [
    {"campaign_budget": {"id": "55", "name": "B", "amount_micros": "5000000"}},
    {"campaign": {"id": "9", "name": "C", "status": "PAUSED"},
     "campaign_budget": {"amount_micros": "5000000"}},
    {"campaign_budget": {"id": "56", "amount_micros": "8000000"}},
]


class TestChanges(unittest.TestCase):
    def test_flatten_turns_int_strings_into_ints(self):
        flat = C.flatten({"a": {"b": "12", "c": "x"}, "d": ["1", {"e": "2"}]})
        self.assertEqual(flat, {"a.b": 12, "a.c": "x", "d": [1, {"e": 2}]})

    def test_masked_keeps_named_leaves_and_marks_cleared_ones(self):
        fields = {"target_spend.cpc_bid_ceiling_micros": 5, "name": "x"}
        self.assertEqual(
            C.masked(fields, ["target_spend.cpc_bid_ceiling_micros"]),
            {"target_spend.cpc_bid_ceiling_micros": 5},
        )
        self.assertEqual(
            C.masked({}, ["maximize_conversions.target_cpa_micros"]),
            {"maximize_conversions.target_cpa_micros": None},
        )

    def test_row_for_matches_ids_including_compound_ones(self):
        self.assertIsNotNone(C.row_for(ROWS, "campaign_budget", BUDGET))
        self.assertIsNone(C.row_for(ROWS, "campaign_budget", f"customers/{CID}/campaignBudgets/1"))
        rows = [{"ad_group_criterion": {"criterion_id": "77", "status": "ENABLED"}}]
        self.assertIsNotNone(C.row_for(rows, "ad_group_criterion", f"customers/{CID}/adGroupCriteria/1~77"))
        rows = [{"ad_group_ad": {"ad": {"id": "5"}, "status": "ENABLED"}}]
        self.assertIsNotNone(C.row_for(rows, "ad_group_ad", f"customers/{CID}/adGroupAds/1~5"))

    def test_before_and_context_are_joined(self):
        ops = [_op("campaign", "update", CAMPAIGN, {"status": "ENABLED"}, ["status"])]
        change = C.from_operations(ops, ROWS)[0]
        self.assertEqual(change.before["status"], "PAUSED")
        self.assertEqual(change.context["campaign_budget.amount_micros"], 5000000)
        self.assertEqual(C.to_json(change)["after"], {"status": "ENABLED"})


class TestDirection(unittest.TestCase):
    KEY = "campaigns.update_campaign_budget"

    def _budget_change(self, new):
        return C.from_operations(
            [_op("campaign_budget", "update", BUDGET, {"amount_micros": str(new)}, ["amount_micros"])],
            ROWS,
        )

    def test_lower_budget_is_protective(self):
        self.assertEqual(
            direction.effective_class(self.KEY, classes.SPEND, self._budget_change(4_000_000), ROWS),
            classes.PROTECTIVE,
        )

    def test_higher_budget_stays_spend(self):
        self.assertEqual(
            direction.effective_class(self.KEY, classes.SPEND, self._budget_change(6_000_000), ROWS),
            classes.SPEND,
        )

    def test_unknown_before_is_an_increase(self):
        changes = C.from_operations(
            [_op("campaign_budget", "update", f"customers/{CID}/campaignBudgets/1",
                 {"amount_micros": "1"}, ["amount_micros"])], ROWS)
        self.assertEqual(direction.classify(changes[0], ROWS), direction.INCREASE)

    def test_non_directional_tool_keeps_its_class(self):
        self.assertEqual(
            direction.effective_class("campaigns.create_campaign_budget", classes.SPEND,
                                      self._budget_change(1), ROWS),
            classes.SPEND,
        )

    def test_status_paused_is_a_decrease_enabled_is_not(self):
        paused = Change("campaign", "update", CAMPAIGN, {"status": "PAUSED"}, {"status": "ENABLED"})
        enabled = Change("campaign", "update", CAMPAIGN, {"status": "ENABLED"}, {"status": "PAUSED"})
        self.assertEqual(direction.classify(paused, []), direction.DECREASE)
        self.assertEqual(direction.classify(enabled, []), direction.INCREASE)

    def test_bid_modifiers(self):
        crit = f"customers/{CID}/campaignCriteria/9~1"
        lower = Change("campaign_criterion", "update", crit, {"bid_modifier": 0.8}, {"bid_modifier": 1.2})
        same = Change("campaign_criterion", "create", "", {"bid_modifier": 1.0, "device.type_": "MOBILE",
                                                          "campaign": CAMPAIGN})
        higher = Change("campaign_criterion", "create", "", {"bid_modifier": 1.3, "device.type_": "MOBILE",
                                                            "campaign": CAMPAIGN})
        exclusion = Change("campaign_criterion", "create", "", {"negative": True, "location.geo_target_constant": "geoTargetConstants/1", "campaign": CAMPAIGN})
        target = Change("campaign_criterion", "create", "", {"negative": False, "location.geo_target_constant": "geoTargetConstants/1", "campaign": CAMPAIGN})
        self.assertEqual(direction.classify(lower, []), direction.DECREASE)
        self.assertEqual(direction.classify(same, []), direction.DECREASE)
        self.assertEqual(direction.classify(higher, []), direction.INCREASE)
        self.assertEqual(direction.classify(exclusion, []), direction.DECREASE)
        self.assertEqual(direction.classify(target, []), direction.INCREASE)

    def test_presence_narrows_interest_widens(self):
        key = "campaigns.set_campaign_geo_target_type"
        presence = Change("campaign", "update", CAMPAIGN,
                          {"geo_target_type_setting.positive_geo_target_type": "PRESENCE"})
        interest = Change("campaign", "update", CAMPAIGN,
                          {"geo_target_type_setting.positive_geo_target_type": "PRESENCE_OR_INTEREST"})
        self.assertEqual(direction.effective_class(key, classes.SPEND, [presence], []), classes.PROTECTIVE)
        self.assertEqual(direction.effective_class(key, classes.SPEND, [interest], []), classes.SPEND)

    def test_budget_move_compares_the_two_budgets(self):
        key = "campaigns.update_campaign"
        to_bigger = C.from_operations(
            [_op("campaign", "update", CAMPAIGN, {"campaign_budget": f"customers/{CID}/campaignBudgets/56"}, ["campaign_budget"])], ROWS)
        self.assertEqual(direction.effective_class(key, classes.SPEND, to_bigger, ROWS), classes.SPEND)
        to_same = C.from_operations(
            [_op("campaign", "update", CAMPAIGN, {"campaign_budget": BUDGET}, ["campaign_budget"])], ROWS)
        self.assertEqual(direction.effective_class(key, classes.SPEND, to_same, ROWS), classes.PROTECTIVE)
        with_rename = C.from_operations(
            [_op("campaign", "update", CAMPAIGN, {"campaign_budget": BUDGET, "name": "x"}, ["campaign_budget", "name"])], ROWS)
        self.assertEqual(direction.effective_class(key, classes.SPEND, with_rename, ROWS), classes.SPEND)

    def test_remove_escalates_legacy_status_tools(self):
        removal = Change("campaign", "remove", CAMPAIGN)
        self.assertEqual(
            direction.effective_class("campaigns.set_campaign_status", classes.SPEND, [removal], []),
            classes.DESTRUCTIVE,
        )
        # A "set" tool that replaces criteria is not destructive.
        self.assertEqual(
            direction.effective_class("campaigns.set_campaign_ad_schedule", classes.SPEND,
                                      [Change("campaign_criterion", "remove", "x")], []),
            classes.SPEND,
        )

    def test_languages_narrowing(self):
        key = "campaigns.set_campaign_languages"
        creates = [Change("campaign_criterion", "create", "",
                          {"language.language_constant": "languageConstants/1037", "campaign": CAMPAIGN})]
        # No language criterion before means ALL languages: any set narrows.
        self.assertEqual(direction.effective_class(key, classes.SPEND, creates, []), classes.PROTECTIVE)
        rows = [{"campaign_criterion": {"criterion_id": "1", "language": {"language_constant": "languageConstants/1000"}}}]
        self.assertEqual(direction.effective_class(key, classes.SPEND, creates, rows), classes.SPEND)
        rows.append({"campaign_criterion": {"criterion_id": "2", "language": {"language_constant": "languageConstants/1037"}}})
        self.assertEqual(direction.effective_class(key, classes.SPEND, creates, rows), classes.PROTECTIVE)

    def test_destructive_stays_destructive(self):
        self.assertEqual(
            direction.effective_class("adgroups.remove_keywords", classes.DESTRUCTIVE, [], []),
            classes.DESTRUCTIVE,
        )


class TestImpact(unittest.TestCase):
    def test_budget_raise_has_daily_and_monthly_delta(self):
        changes = C.from_operations(
            [_op("campaign_budget", "update", BUDGET, {"amount_micros": "6000000"}, ["amount_micros"])], ROWS)
        est = impact.estimate(changes, ROWS)
        self.assertEqual(est["daily_delta"], 1.0)
        self.assertEqual(est["monthly_delta"], 30.4)
        self.assertEqual(est["details"][0]["from"], 5.0)
        self.assertEqual(est["details"][0]["to"], 6.0)

    def test_enabling_starts_spending_the_budget(self):
        changes = C.from_operations(
            [_op("campaign", "update", CAMPAIGN, {"status": "ENABLED"}, ["status"])], ROWS)
        est = impact.estimate(changes, ROWS)
        self.assertEqual(est["daily_delta"], 5.0)
        self.assertEqual(est["details"][0]["campaign_daily_budget"], 5.0)

    def test_new_budget_and_bid_have_no_daily_figure(self):
        changes = C.from_operations(
            [_op("campaign_budget", "create", "", {"amount_micros": "3000000", "name": "n"}),
             _op("ad_group", "update", f"customers/{CID}/adGroups/1", {"cpc_bid_micros": "2500000"}, ["cpc_bid_micros"])],
            ROWS)
        est = impact.estimate(changes, ROWS)
        self.assertIsNone(est["daily_delta"])
        self.assertEqual(est["details"][0]["what"], "new budget")
        self.assertEqual(est["details"][1]["to"], 2.5)

    def test_rollback_lines(self):
        changes = C.from_operations(
            [_op("campaign_budget", "update", BUDGET, {"amount_micros": "6000000"}, ["amount_micros"]),
             _op("campaign", "remove", CAMPAIGN, {}),
             _op("ad_group", "create", "", {"name": "g"})],
            ROWS)
        steps = impact.rollback(changes, "campaigns.update_campaign_budget")
        self.assertIn("back to {'amount_micros': 5000000}", steps[0])
        self.assertIn("Not reversible", steps[1])
        self.assertIn("Remove or pause the created ad_group", steps[2])
