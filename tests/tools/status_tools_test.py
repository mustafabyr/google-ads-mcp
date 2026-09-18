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

"""The split status tools: pausing asks nothing, enabling a campaign does."""

import unittest

from ads_mcp.tools import adgroups, campaigns
from tests.fakes import FakeAds, row

CID = "1234567890"


def _campaign_rows(status="ENABLED"):
    return [
        row(
            campaign={"id": 42, "name": "Brand", "status": status},
            campaign_budget={"amount_micros": 500_000_000},
        )
    ]


class TestCampaignStatus(unittest.TestCase):
    def test_pause_writes_paused_without_confirmation(self):
        fake = FakeAds().on("FROM campaign WHERE", _campaign_rows("ENABLED"))
        with fake.installed():
            result = campaigns.pause_campaign(CID, "42")
        calls = fake.calls_to("CampaignService.mutate_campaigns")
        self.assertEqual(len(calls), 1)
        op = calls[0]["operations"][0]
        self.assertEqual(op.update.status.name, "PAUSED")
        self.assertEqual(list(op.update_mask.paths), ["status"])
        self.assertEqual(result["previous_status"], "ENABLED")
        self.assertEqual(result["new_status"], "PAUSED")

    def test_enable_refuses_without_confirmation_and_names_the_money(self):
        fake = FakeAds().on("FROM campaign WHERE", _campaign_rows("PAUSED"))
        with fake.installed():
            with self.assertRaises(campaigns.SpendConfirmationRequired) as ctx:
                campaigns.enable_campaign(CID, "42")
        self.assertIn("500.0/day", str(ctx.exception))
        self.assertIn("15200.0/month", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_enable_writes_enabled_with_confirmation(self):
        fake = FakeAds().on("FROM campaign WHERE", _campaign_rows("PAUSED"))
        with fake.installed():
            result = campaigns.enable_campaign(CID, "42", confirm_spend=True)
        op = fake.calls_to("CampaignService.mutate_campaigns")[0]["operations"][0]
        self.assertEqual(op.update.status.name, "ENABLED")
        self.assertEqual(result["daily_budget"], 500.0)

    def test_legacy_tool_still_removes_with_confirmation(self):
        fake = FakeAds().on("FROM campaign WHERE", _campaign_rows("PAUSED"))
        with fake.installed():
            campaigns.set_campaign_status(CID, "42", "REMOVED", confirm_remove=True)
        op = fake.calls_to("CampaignService.mutate_campaigns")[0]["operations"][0]
        self.assertEqual(op._pb.WhichOneof("operation"), "remove")
        self.assertEqual(op.remove, f"customers/{CID}/campaigns/42")

    def test_unknown_campaign_is_an_error(self):
        fake = FakeAds()
        with fake.installed():
            with self.assertRaises(ValueError):
                campaigns.pause_campaign(CID, "42")
        self.assertFalse(fake.wrote)


class TestAdGroupStatus(unittest.TestCase):
    def _fake(self, status):
        return FakeAds().on(
            "FROM ad_group WHERE",
            [row(ad_group={"id": 7, "name": "G", "status": status})],
        )

    def test_pause_and_enable(self):
        fake = self._fake("ENABLED")
        with fake.installed():
            paused = adgroups.pause_ad_group(CID, "7")
            enabled = adgroups.enable_ad_group(CID, "7")
        ops = [c["operations"][0] for c in fake.calls_to("AdGroupService.mutate_ad_groups")]
        self.assertEqual([o.update.status.name for o in ops], ["PAUSED", "ENABLED"])
        self.assertEqual(paused["previous_status"], "ENABLED")
        self.assertEqual(enabled["new_status"], "ENABLED")

    def test_legacy_remove_needs_confirmation(self):
        fake = self._fake("ENABLED")
        with fake.installed():
            with self.assertRaises(ValueError):
                adgroups.set_ad_group_status(CID, "7", "REMOVED")
        self.assertFalse(fake.wrote)


class TestKeywordStatus(unittest.TestCase):
    def test_pause_keywords_updates_each(self):
        fake = FakeAds().on(
            "FROM ad_group_criterion",
            [
                row(ad_group_criterion={"criterion_id": 1, "status": "ENABLED",
                                        "keyword": {"text": "a", "match_type": "PHRASE"}}),
                row(ad_group_criterion={"criterion_id": 2, "status": "ENABLED",
                                        "keyword": {"text": "b", "match_type": "EXACT"}}),
            ],
        )
        with fake.installed():
            result = adgroups.pause_keywords(CID, "7", ["1", "2"])
        ops = fake.calls_to("AdGroupCriterionService.mutate_ad_group_criteria")[0]["operations"]
        self.assertEqual(len(ops), 2)
        self.assertTrue(all(o.update.status.name == "PAUSED" for o in ops))
        self.assertEqual(result["changed"], 2)
        self.assertEqual([b["keyword"]["text"] for b in result["before"]], ["a", "b"])

    def test_enable_keywords_needs_ids(self):
        with FakeAds().installed():
            with self.assertRaises(ValueError):
                adgroups.enable_keywords(CID, "7", [])


class TestAdStatus(unittest.TestCase):
    def test_pause_ad(self):
        fake = FakeAds().on(
            "FROM ad_group_ad",
            [row(ad_group_ad={"ad": {"id": 99}, "status": "ENABLED"})],
        )
        with fake.installed():
            result = adgroups.pause_ad(CID, "7", "99")
        op = fake.calls_to("AdGroupAdService.mutate_ad_group_ads")[0]["operations"][0]
        self.assertEqual(op.update.status.name, "PAUSED")
        self.assertEqual(op.update.resource_name, f"customers/{CID}/adGroupAds/7~99")
        self.assertEqual(result["previous_status"], "ENABLED")

    def test_legacy_removed_is_a_remove_operation(self):
        fake = FakeAds().on(
            "FROM ad_group_ad",
            [row(ad_group_ad={"ad": {"id": 99}, "status": "ENABLED"})],
        )
        with fake.installed():
            adgroups.set_ad_status(CID, "7", "99", "REMOVED", confirm_remove=True)
        op = fake.calls_to("AdGroupAdService.mutate_ad_group_ads")[0]["operations"][0]
        self.assertEqual(op._pb.WhichOneof("operation"), "remove")
