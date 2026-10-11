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

"""Portfolio bidding strategies: the settings that are not on the campaign.

Every case here comes from one live account: a Maximize Conversions
portfolio strategy aligned to a shared budget, carrying two campaigns,
with no CPC ceiling and MISCONFIGURED_CONVERSION_TYPES on both. The
campaign-level tool was called on it and Google answered
BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED, pointing at a field the
operation never set.
"""

import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from ads_mcp.guardrails import classes, direction, limits
from ads_mcp.tools import _bidding, bidding_strategies, campaigns
from tests.fakes import FakeAds, row

CID = "6314904949"
STRATEGY_ID = 11215843012
BUDGET_ID = 13886713159
STRATEGY_NAME = f"customers/{CID}/biddingStrategies/{STRATEGY_ID}"


def strategy_row(
    kind="MAXIMIZE_CONVERSIONS",
    aligned=BUDGET_ID,
    ceiling_micros=0,
    campaigns_on_it=2,
):
    strategy = {
        "resource_name": STRATEGY_NAME,
        "id": STRATEGY_ID,
        "name": "Portfolio Maximize Conversions",
        "status": "ENABLED",
        "type_": kind,
        "effective_currency_code": "TRY",
        "aligned_campaign_budget_id": aligned,
        "campaign_count": campaigns_on_it,
        "non_removed_campaign_count": campaigns_on_it,
    }
    if ceiling_micros:
        block = _bidding.PORTFOLIO_CEILING_PATHS[kind].split(".", 1)[0]
        strategy[block] = {"cpc_bid_ceiling_micros": ceiling_micros}
    return row(bidding_strategy=strategy)


def campaign_row(
    campaign_id=20476100135,
    name="Search general",
    status="ENABLED",
    on_portfolio=True,
    budget_id=BUDGET_ID,
    system_status="MISCONFIGURED_CONVERSION_TYPES",
):
    return row(
        campaign={
            "id": campaign_id,
            "name": name,
            "status": status,
            "advertising_channel_type": "SEARCH",
            "bidding_strategy": STRATEGY_NAME if on_portfolio else "",
            "bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
            "bidding_strategy_system_status": system_status,
        },
        campaign_budget={
            "id": budget_id,
            "name": "Shared budget",
            "amount_micros": 350_000_000,
            "explicitly_shared": budget_id == BUDGET_ID,
        },
    )


def account(campaign_rows=None, strategy_rows=None):
    """A fake wired for both of this module's queries."""
    campaign_rows = (
        campaign_rows
        if campaign_rows is not None
        else [campaign_row(), campaign_row(21626338285, "telko", "PAUSED")]
    )
    strategy_rows = (
        strategy_rows if strategy_rows is not None else [strategy_row()]
    )

    def campaigns_for(query):
        if "campaign.id =" not in query:
            return campaign_rows
        wanted = query.split("campaign.id =")[1].split()[0]
        return [
            r
            for r in campaign_rows
            if str(r.campaign.id) == wanted.strip()
        ]

    return (
        FakeAds()
        .on(r"FROM bidding_strategy", strategy_rows)
        .on(r"FROM campaign WHERE", campaigns_for)
    )


class TestList(unittest.TestCase):
    def test_lists_the_strategy_its_campaigns_and_what_is_wrong(self):
        fake = account()
        with fake.installed():
            result = bidding_strategies.list_bidding_strategies(CID)

        self.assertEqual(result["portfolio_count"], 1)
        portfolio = result["portfolio_strategies"][0]
        self.assertEqual(portfolio["type"], "MAXIMIZE_CONVERSIONS")
        self.assertEqual(portfolio["aligned_campaign_budget_id"], str(BUDGET_ID))
        self.assertEqual(len(portfolio["campaigns"]), 2)
        notes = " ".join(portfolio["notes"])
        self.assertIn("No CPC ceiling", notes)
        self.assertIn("BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED", notes)
        self.assertIn("optimising towards nothing", notes)
        self.assertFalse(fake.wrote)

    def test_type_is_read_through_the_proto_plus_rename(self):
        """A message's 'type' arrives as 'type_'; reading 'type' is silent None."""
        self.assertEqual(
            _bidding.read_type({"type_": "TARGET_SPEND"}), "TARGET_SPEND"
        )
        self.assertEqual(_bidding.read_type({}), "")

    def test_settings_hold_only_the_fields_this_type_carries(self):
        fake = account(
            strategy_rows=[
                strategy_row("TARGET_SPEND", aligned=0, ceiling_micros=45_000_000)
            ]
        )
        with fake.installed():
            result = bidding_strategies.list_bidding_strategies(CID)
        portfolio = result["portfolio_strategies"][0]
        self.assertEqual(portfolio["cpc_bid_ceiling"], 45.0)
        self.assertEqual(
            sorted(portfolio["settings"]),
            ["cpc_bid_ceiling", "target_spend"],
        )
        self.assertIsNone(portfolio["aligned_campaign_budget_id"])

    def test_empty_account_says_so_rather_than_nothing(self):
        fake = account(strategy_rows=[])
        with fake.installed():
            result = bidding_strategies.list_bidding_strategies(CID)
        self.assertIn("No portfolio bidding strategy", result["summary"])


class TestCeiling(unittest.TestCase):
    def test_raising_refuses_without_confirmation_and_names_the_campaigns(self):
        fake = account()
        with fake.installed():
            with self.assertRaises(
                bidding_strategies.SpendConfirmationRequired
            ) as ctx:
                bidding_strategies.update_portfolio_bid_ceiling(
                    CID, str(STRATEGY_ID), 45
                )
        message = str(ctx.exception)
        self.assertIn("Search general", message)
        self.assertNotIn("telko", message)  # PAUSED campaigns spend nothing
        self.assertFalse(fake.wrote)

    def test_writes_the_leaf_of_the_strategys_own_scheme(self):
        fake = account()
        with fake.installed():
            result = bidding_strategies.update_portfolio_bid_ceiling(
                CID, str(STRATEGY_ID), 45, confirm_spend=True
            )
        calls = fake.calls_to("BiddingStrategyService.mutate_bidding_strategies")
        self.assertEqual(len(calls), 1)
        operation = calls[0]["operations"][0]
        self.assertEqual(
            list(operation.update_mask.paths),
            ["maximize_conversions.cpc_bid_ceiling_micros"],
        )
        self.assertEqual(
            operation.update.maximize_conversions.cpc_bid_ceiling_micros,
            45_000_000,
        )
        self.assertEqual(operation.update.resource_name, STRATEGY_NAME)
        self.assertEqual(result["new_cpc_bid_ceiling"], 45)
        self.assertEqual(result["enabled_campaigns_affected"], 1)

    def test_target_spend_strategy_writes_the_target_spend_leaf(self):
        fake = account(strategy_rows=[strategy_row("TARGET_SPEND", aligned=0)])
        with fake.installed():
            bidding_strategies.update_portfolio_bid_ceiling(
                CID, str(STRATEGY_ID), 20, confirm_spend=True
            )
        operation = fake.calls_to(
            "BiddingStrategyService.mutate_bidding_strategies"
        )[0]["operations"][0]
        self.assertEqual(
            list(operation.update_mask.paths),
            ["target_spend.cpc_bid_ceiling_micros"],
        )

    def test_a_strategy_with_no_ceiling_field_is_refused_before_the_write(self):
        fake = account(strategy_rows=[strategy_row("ENHANCED_CPC", aligned=0)])
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                bidding_strategies.update_portfolio_bid_ceiling(
                    CID, str(STRATEGY_ID), 20, confirm_spend=True
                )
        self.assertIn("carries no cpc_bid_ceiling", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_unknown_strategy_is_named_not_guessed(self):
        fake = account(strategy_rows=[])
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                bidding_strategies.update_portfolio_bid_ceiling(
                    CID, "999", 20, confirm_spend=True
                )
        self.assertIn("No bidding strategy 999", str(ctx.exception))


class TestDetach(unittest.TestCase):
    def test_refuses_until_the_caller_picks_a_route(self):
        fake = account()
        with fake.installed():
            with self.assertRaises(
                bidding_strategies.PortfolioAlignmentRequired
            ) as ctx:
                bidding_strategies.detach_campaign_from_portfolio(
                    CID,
                    "20476100135",
                    "MAXIMIZE_CLICKS",
                    cpc_bid_ceiling=45,
                    confirm_spend=True,
                )
        message = str(ctx.exception)
        self.assertIn("new_budget_id", message)
        self.assertIn("unalign_shared_budget", message)
        self.assertIn("telko", message)  # the campaign the other route touches
        self.assertFalse(fake.wrote)

    def test_clears_the_portfolio_and_sets_the_new_scheme_in_one_operation(self):
        fake = account()
        with fake.installed():
            result = bidding_strategies.detach_campaign_from_portfolio(
                CID,
                "20476100135",
                "MAXIMIZE_CLICKS",
                cpc_bid_ceiling=45,
                new_budget_id="15858086847",
                confirm_spend=True,
            )
        calls = fake.calls_to("CampaignService.mutate_campaigns")
        self.assertEqual(len(calls), 1, "one operation, not two")
        operation = calls[0]["operations"][0]
        self.assertEqual(
            list(operation.update_mask.paths),
            [
                "bidding_strategy",
                "target_spend.cpc_bid_ceiling_micros",
                "campaign_budget",
            ],
        )
        self.assertEqual(operation.update.bidding_strategy, "")
        self.assertEqual(
            operation.update.target_spend.cpc_bid_ceiling_micros, 45_000_000
        )
        self.assertEqual(
            operation.update.campaign_budget,
            f"customers/{CID}/campaignBudgets/15858086847",
        )
        self.assertEqual(result["moved_to_budget_id"], "15858086847")
        self.assertFalse(result["shared_budget_unaligned"])
        self.assertEqual(result["also_affected"], [])

    def test_unaligning_is_a_separate_write_and_names_who_else_it_hits(self):
        fake = account()
        with fake.installed():
            result = bidding_strategies.detach_campaign_from_portfolio(
                CID,
                "20476100135",
                "MAXIMIZE_CLICKS",
                cpc_bid_ceiling=45,
                unalign_shared_budget=True,
                confirm_spend=True,
            )
        strategy_calls = fake.calls_to(
            "BiddingStrategyService.mutate_bidding_strategies"
        )
        self.assertEqual(len(strategy_calls), 1)
        operation = strategy_calls[0]["operations"][0]
        self.assertEqual(
            list(operation.update_mask.paths), ["aligned_campaign_budget_id"]
        )
        self.assertEqual(operation.update.aligned_campaign_budget_id, 0)
        campaign_operation = fake.calls_to("CampaignService.mutate_campaigns")[
            0
        ]["operations"][0]
        self.assertEqual(
            list(campaign_operation.update_mask.paths),
            ["bidding_strategy", "target_spend.cpc_bid_ceiling_micros"],
        )
        self.assertTrue(result["shared_budget_unaligned"])
        self.assertEqual(result["also_affected"], ["telko"])

    def test_no_alignment_means_no_route_question(self):
        fake = account(strategy_rows=[strategy_row(aligned=0)])
        with fake.installed():
            bidding_strategies.detach_campaign_from_portfolio(
                CID,
                "20476100135",
                "MAXIMIZE_CLICKS",
                cpc_bid_ceiling=45,
                confirm_spend=True,
            )
        self.assertEqual(
            len(fake.calls_to("CampaignService.mutate_campaigns")), 1
        )
        self.assertEqual(
            fake.calls_to("BiddingStrategyService.mutate_bidding_strategies"),
            [],
        )

    def test_enabled_campaign_needs_confirmation_first(self):
        fake = account()
        with fake.installed():
            with self.assertRaises(
                bidding_strategies.SpendConfirmationRequired
            ) as ctx:
                bidding_strategies.detach_campaign_from_portfolio(
                    CID, "20476100135", "MAXIMIZE_CLICKS", cpc_bid_ceiling=45
                )
        self.assertIn("350.0/day", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_a_campaign_with_its_own_strategy_is_sent_to_the_other_tool(self):
        fake = account(
            campaign_rows=[
                campaign_row(on_portfolio=False, budget_id=1, system_status="ENABLED")
            ]
        )
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                bidding_strategies.detach_campaign_from_portfolio(
                    CID,
                    "20476100135",
                    "MAXIMIZE_CLICKS",
                    cpc_bid_ceiling=45,
                    confirm_spend=True,
                )
        self.assertIn("campaigns.update_bid_strategy", str(ctx.exception))
        self.assertFalse(fake.wrote)


class TestAttach(unittest.TestCase):
    def test_is_the_undo_of_detach(self):
        fake = account(
            campaign_rows=[
                campaign_row(
                    on_portfolio=False,
                    budget_id=BUDGET_ID,
                    system_status="ENABLED",
                ),
                campaign_row(21626338285, "telko", "PAUSED"),
            ]
        )
        with fake.installed():
            result = bidding_strategies.attach_campaign_to_portfolio(
                CID, "20476100135", str(STRATEGY_ID), confirm_spend=True
            )
        operation = fake.calls_to("CampaignService.mutate_campaigns")[0][
            "operations"
        ][0]
        self.assertEqual(list(operation.update_mask.paths), ["bidding_strategy"])
        self.assertEqual(operation.update.bidding_strategy, STRATEGY_NAME)
        self.assertTrue(result["changed"])
        self.assertIsNone(result["budget_change"])

    def test_a_different_budget_has_to_be_agreed_to(self):
        fake = account(
            campaign_rows=[
                campaign_row(
                    on_portfolio=False,
                    budget_id=15858086847,
                    system_status="ENABLED",
                ),
                campaign_row(21626338285, "telko", "PAUSED"),
            ]
        )
        with fake.installed():
            with self.assertRaises(
                bidding_strategies.PortfolioAlignmentRequired
            ) as ctx:
                bidding_strategies.attach_campaign_to_portfolio(
                    CID, "20476100135", str(STRATEGY_ID), confirm_spend=True
                )
        self.assertIn("move_to_aligned_budget=True", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_already_attached_writes_nothing(self):
        fake = account()
        with fake.installed():
            result = bidding_strategies.attach_campaign_to_portfolio(
                CID, "20476100135", str(STRATEGY_ID), confirm_spend=True
            )
        self.assertFalse(result["changed"])
        self.assertFalse(fake.wrote)


class TestCampaignLevelToolRefuses(unittest.TestCase):
    """The refusal that made this module necessary, caught before Google."""

    def test_update_bid_strategy_names_the_portfolio_instead_of_failing(self):
        fake = FakeAds().on(
            r"FROM campaign WHERE",
            [
                row(
                    campaign={
                        "id": 20476100135,
                        "name": "Search general",
                        "status": "ENABLED",
                        "bidding_strategy": STRATEGY_NAME,
                        "bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
                    },
                    campaign_budget={"amount_micros": 350_000_000},
                )
            ],
        )
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                campaigns.update_bid_strategy(
                    CID,
                    "20476100135",
                    "MAXIMIZE_CLICKS",
                    cpc_bid_ceiling=45,
                    confirm_spend=True,
                )
        message = str(ctx.exception)
        self.assertIn(str(STRATEGY_ID), message)
        self.assertIn("BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED", message)
        self.assertIn("update_portfolio_bid_ceiling", message)
        self.assertIn("detach_campaign_from_portfolio", message)
        self.assertFalse(fake.wrote)


class TestGuardrails(unittest.TestCase):
    def test_every_portfolio_ceiling_path_is_checked_against_the_limits(self):
        """A path the limits layer does not know is a ceiling nobody caps."""
        for path in _bidding.PORTFOLIO_CEILING_PATHS.values():
            self.assertIn(path, limits._CPC_FIELDS, path)

    def test_every_portfolio_ceiling_path_decides_direction(self):
        for path in _bidding.PORTFOLIO_CEILING_PATHS.values():
            self.assertIn(path, direction._BID_FIELDS, path)

    def test_lowering_a_portfolio_ceiling_is_protective(self):
        change = limits.Change(
            resource="bidding_strategy",
            kind="update",
            resource_name=STRATEGY_NAME,
            fields={"maximize_conversions.cpc_bid_ceiling_micros": 20_000_000},
            before={"maximize_conversions.cpc_bid_ceiling_micros": 45_000_000},
        )
        self.assertEqual(
            direction.effective_class(
                "bidding_strategies.update_portfolio_bid_ceiling",
                classes.SPEND,
                [change],
                [],
            ),
            classes.PROTECTIVE,
        )

    def test_raising_a_portfolio_ceiling_stays_a_spend_change(self):
        change = limits.Change(
            resource="bidding_strategy",
            kind="update",
            resource_name=STRATEGY_NAME,
            fields={"maximize_conversions.cpc_bid_ceiling_micros": 60_000_000},
            before={"maximize_conversions.cpc_bid_ceiling_micros": 45_000_000},
        )
        self.assertEqual(
            direction.effective_class(
                "bidding_strategies.update_portfolio_bid_ceiling",
                classes.SPEND,
                [change],
                [],
            ),
            classes.SPEND,
        )

    def test_a_ceiling_over_the_account_ceiling_is_refused(self):
        change = limits.Change(
            resource="bidding_strategy",
            kind="update",
            resource_name=STRATEGY_NAME,
            fields={"maximize_conversions.cpc_bid_ceiling_micros": 90_000_000},
            before={"maximize_conversions.cpc_bid_ceiling_micros": 45_000_000},
        )
        with tempfile.TemporaryDirectory() as scratch:
            path = pathlib.Path(scratch) / "limits.yaml"
            path.write_text(
                f"accounts:\n  \"{CID}\":\n    max_cpc_ceiling_try: 60\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {limits.LIMITS_PATH_ENV: str(path)}):
                limits.reset_cache()
                self.addCleanup(limits.reset_cache)
                with self.assertRaises(limits.LimitExceeded) as ctx:
                    limits.check_changes(CID, [change])
                self.assertEqual(ctx.exception.key, "max_cpc_ceiling_try")
                self.assertEqual(ctx.exception.requested, 90.0)

    def test_detach_and_attach_are_a_pair(self):
        """Every tool that changes structure here has its undo."""
        for key in (
            "bidding_strategies.detach_campaign_from_portfolio",
            "bidding_strategies.attach_campaign_to_portfolio",
        ):
            self.assertEqual(classes.TOOL_CLASSES[key], classes.SPEND)


if __name__ == "__main__":
    unittest.main()
