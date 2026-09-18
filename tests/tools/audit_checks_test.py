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

"""Each new audit check fires on the situation it was written for."""

import datetime
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from ads_mcp.guardrails import audit_log
from ads_mcp.tools import audit
from tests.fakes import FakeAds, row

CID = "1234567890"


def _customer():
    return [row(customer={"id": int(CID), "descriptive_name": "A", "currency_code": "TRY",
                          "time_zone": "Europe/Istanbul", "auto_tagging_enabled": True,
                          "conversion_tracking_setting": {"conversion_tracking_id": 1},
                          "call_reporting_setting": {"call_reporting_enabled": True,
                                                     "call_conversion_reporting_enabled": True}})]


def _campaign(name="C", bidding="TARGET_SPEND", ceiling=0, tis_fraction=0, rank_lost=0.0,
              budget_lost=0.0, partners=False, display=False, geo="PRESENCE", cid_=1):
    campaign = {"id": cid_, "name": name, "status": "ENABLED", "advertising_channel_type": "SEARCH",
                "bidding_strategy_type": bidding,
                "network_settings": {"target_search_network": partners, "target_content_network": display},
                "geo_target_type_setting": {"positive_geo_target_type": geo}}
    # The strategy settings are a oneof: set only the one that applies.
    if bidding == "TARGET_SPEND":
        campaign["target_spend"] = {"cpc_bid_ceiling_micros": ceiling}
    elif bidding == "TARGET_IMPRESSION_SHARE":
        campaign["target_impression_share"] = {"location_fraction_micros": tis_fraction}
    return row(
        campaign=campaign,
        campaign_budget={"id": 5, "amount_micros": 100_000_000},
        metrics={"cost_micros": 50_000_000, "clicks": 10, "conversions": 1.0,
                 "search_budget_lost_impression_share": budget_lost,
                 "search_rank_lost_impression_share": rank_lost,
                 "search_impression_share": 0.5},
    )


def _titles(result):
    return [f["finding"] for f in result["findings"]]


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {audit_log.AUDIT_DIR_ENV: str(pathlib.Path(self.tmp.name) / "audit")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake(self):
        return FakeAds().on("customer.descriptive_name", _customer())

    def run_audit(self, fake, **kwargs):
        with fake.installed():
            return audit._audit_account(CID, skip_search_terms=True, **kwargs)


class TestBiddingChecks(AuditCase):
    def test_maximize_clicks_without_ceiling(self):
        fake = self.fake().on("campaign.target_spend.cpc_bid_ceiling_micros", [_campaign(ceiling=0)])
        result = self.run_audit(fake)
        self.assertTrue(any("no CPC ceiling" in t for t in _titles(result)), _titles(result))
        fake = self.fake().on("campaign.target_spend.cpc_bid_ceiling_micros", [_campaign(ceiling=5_000_000)])
        self.assertFalse(any("no CPC ceiling" in t for t in _titles(self.run_audit(fake))))

    def test_stuck_target_impression_share(self):
        stuck = _campaign(bidding="TARGET_IMPRESSION_SHARE", tis_fraction=300_000, rank_lost=0.45, budget_lost=0.01)
        fake = self.fake().on("campaign.target_spend.cpc_bid_ceiling_micros", [stuck])
        titles = _titles(self.run_audit(fake))
        self.assertTrue(any("aim low, lose to rank" in t for t in titles), titles)
        fine = _campaign(bidding="TARGET_IMPRESSION_SHARE", tis_fraction=800_000, rank_lost=0.45, budget_lost=0.01)
        fake = self.fake().on("campaign.target_spend.cpc_bid_ceiling_micros", [fine])
        self.assertFalse(any("aim low" in t for t in _titles(self.run_audit(fake))))

    def test_networks_and_brand_exclusion(self):
        rows = [_campaign(name="Brand", geo="PRESENCE_OR_INTEREST", cid_=1, partners=True),
                _campaign(name="Generic", geo="PRESENCE_OR_INTEREST", cid_=2, display=True, ceiling=1)]
        fake = self.fake().on("campaign.target_spend.cpc_bid_ceiling_micros", rows)
        result = self.run_audit(fake, brand_campaign_ids=["1"])
        titles = _titles(result)
        interest = next(f for f in result["findings"] if "PRESENCE_OR_INTEREST" in f["finding"])
        self.assertEqual(interest["evidence"], ["Generic"])
        self.assertTrue(any("Display" in t for t in titles), titles)
        self.assertTrue(any("search partners" in t for t in titles), titles)


class TestConversionChecks(AuditCase):
    def _goals(self, biddable):
        return [row(customer_conversion_goal={"category": c, "origin": "WEBSITE", "biddable": b})
                for c, b in biddable]

    def test_only_biddable_goal_recorded_nothing(self):
        fake = (self.fake()
                .on("FROM customer_conversion_goal", self._goals([("SUBMIT_LEAD_FORM", True), ("CONTACT", False)]))
                .on("segments.conversion_action_category", [
                    row(segments={"conversion_action": "customers/1/conversionActions/9",
                                  "conversion_action_name": "Call", "conversion_action_category": "CONTACT"},
                        metrics={"conversions": 0.0, "all_conversions": 12.0})]))
        result = self.run_audit(fake)
        titles = _titles(result)
        self.assertTrue(any("only goal the account bids toward recorded nothing" in t for t in titles), titles)
        self.assertEqual(result["measurement"]["recorded_by_category_30d"], {"CONTACT": 12.0})

    def test_contract_form_only_but_calls_biddable(self):
        fake = self.fake().on("FROM customer_conversion_goal",
                              self._goals([("SUBMIT_LEAD_FORM", True), ("PHONE_CALL_LEAD", True)]))
        titles = _titles(self.run_audit(fake, lead_contract="form_only"))
        self.assertTrue(any("forms only, but call goals are biddable" in t for t in titles), titles)
        titles = _titles(self.run_audit(fake))
        self.assertFalse(any("forms only" in t for t in titles))
        with self.assertRaises(ValueError):
            audit._check_conversions(CID, audit._Findings(), lead_contract="MAYBE")

    def test_duplicate_outbound_click_actions(self):
        fake = self.fake().on("FROM conversion_action", [
            row(conversion_action={"resource_name": "customers/1/conversionActions/1", "name": "WhatsApp",
                                   "category": "OUTBOUND_CLICK", "status": "ENABLED", "origin": "WEBSITE"}),
            row(conversion_action={"resource_name": "customers/1/conversionActions/2", "name": "WhatsApp (GA4)",
                                   "category": "OUTBOUND_CLICK", "status": "ENABLED", "origin": "WEBSITE"}),
        ])
        finding = next(f for f in self.run_audit(fake)["findings"] if "OUTBOUND_CLICK" in f["finding"])
        self.assertEqual(finding["evidence"], ["WhatsApp", "WhatsApp (GA4)"])


class TestStructureChecks(AuditCase):
    def test_thin_rsa_and_single_ad(self):
        fake = self.fake().on("FROM ad_group_ad", [
            row(campaign={"name": "C"}, ad_group={"id": 1, "name": "G", "status": "ENABLED"},
                ad_group_ad={"status": "ENABLED", "ad": {"id": 11, "type_": "RESPONSIVE_SEARCH_AD",
                             "responsive_search_ad": {"headlines": [{"text": f"h{i}"} for i in range(5)]}}}),
        ])
        result = self.run_audit(fake)
        thin = next(f for f in result["findings"] if "fewer than 12 headlines" in f["finding"])
        self.assertEqual(thin["evidence"][0]["headlines"], 5)
        self.assertTrue(any("single ad" in t for t in _titles(result)))

    def test_ad_group_spend_hog(self):
        fake = self.fake().on("metrics.cost_micros, metrics.conversions FROM ad_group", [
            row(campaign={"id": 1, "name": "C"}, ad_group={"id": 1, "name": "Hog"},
                metrics={"cost_micros": 800_000_000, "conversions": 0.0}),
            row(campaign={"id": 1, "name": "C"}, ad_group={"id": 2, "name": "Good"},
                metrics={"cost_micros": 200_000_000, "conversions": 3.0}),
        ])
        finding = next(f for f in self.run_audit(fake)["findings"] if "take over 50%" in f["finding"])
        self.assertEqual(finding["evidence"][0]["ad_group"], "Hog")
        self.assertEqual(finding["evidence"][0]["share_of_campaign"], 80.0)
        # Below the minimum cost it is noise, not a finding.
        self.assertFalse(any("take over 50%" in t for t in _titles(self.run_audit(fake, min_wasted_cost=1000))))

    def test_call_extension_counting_nowhere(self):
        fake = self.fake().on("asset.call_asset.call_conversion_reporting_state", [
            row(campaign={"id": 1, "name": "C"}, asset={"id": 7, "call_asset": {
                "call_conversion_reporting_state": "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
                "call_conversion_action": ""}}, campaign_asset={"field_type": "CALL"}),
        ])
        finding = next(f for f in self.run_audit(fake)["findings"] if "no conversion action" in f["finding"])
        self.assertEqual(finding["evidence"], [{"campaign": "C", "asset_id": "7"}])


class TestNegativeChecks(AuditCase):
    def test_one_word_broad_and_conflicts(self):
        fake = (self.fake()
                .on("FROM shared_criterion", [
                    row(shared_set={"id": 9, "name": "Master"},
                        shared_criterion={"keyword": {"text": "kurs", "match_type": "BROAD"}}),
                    row(shared_set={"id": 9, "name": "Master"},
                        shared_criterion={"keyword": {"text": "iş ilanı", "match_type": "PHRASE"}}),
                ])
                .on("FROM campaign_shared_set", [row(campaign={"id": 1}, shared_set={"id": 9, "type_": "NEGATIVE_KEYWORDS"})])
                .on("campaign_criterion.negative = true", [
                    row(campaign={"id": 1}, campaign_criterion={"keyword": {"text": "tus kampı", "match_type": "EXACT"}, "negative": True})])
                .on("ad_group_criterion.negative = true", [])
                .on("ad_group_criterion.negative = false", [
                    row(campaign={"id": 1, "name": "C"}, ad_group={"id": 1},
                        ad_group_criterion={"keyword": {"text": "tus hazırlık kursu", "match_type": "PHRASE"}}),
                    row(campaign={"id": 1, "name": "C"}, ad_group={"id": 1},
                        ad_group_criterion={"keyword": {"text": "tus kampı", "match_type": "EXACT"}}),
                    row(campaign={"id": 1, "name": "C"}, ad_group={"id": 1},
                        ad_group_criterion={"keyword": {"text": "yks kursu", "match_type": "PHRASE"}}),
                ]))
        result = self.run_audit(fake)
        broad = next(f for f in result["findings"] if "one-word BROAD" in f["finding"])
        self.assertEqual(broad["evidence"], [{"list": "Master", "term": "kurs"}])
        clash = next(f for f in result["findings"] if "blocked by the account's own negatives" in f["finding"])
        blocked = {(e["keyword"], e["negative"]) for e in clash["evidence"]}
        self.assertEqual(blocked, {("tus kampı", "tus kampı")})

    def test_negative_blocks_rules(self):
        self.assertTrue(audit._negative_blocks("kurs", "BROAD", "tus kurs fiyat"))
        self.assertFalse(audit._negative_blocks("kurs", "BROAD", "tus kursu fiyat"))
        self.assertTrue(audit._negative_blocks("iş ilanı", "PHRASE", "tus iş ilanı ankara"))
        self.assertFalse(audit._negative_blocks("iş ilanı", "PHRASE", "ilanı iş"))
        self.assertTrue(audit._negative_blocks("Tus Kampı", "EXACT", "tus kampı"))
        self.assertFalse(audit._negative_blocks("tus", "EXACT", "tus kampı"))


class TestUnloggedChanges(AuditCase):
    def _events(self):
        today = datetime.date.today()
        stamp = f"{(today - datetime.timedelta(days=1)).isoformat()} 10:00:00"
        return [
            row(change_event={"change_date_time": stamp, "change_resource_type": "CAMPAIGN_BUDGET",
                              "resource_change_operation": "UPDATE", "client_type": "GOOGLE_ADS_WEB_CLIENT",
                              "user_email": "someone@example.com", "campaign": "customers/1/campaigns/42",
                              "changed_fields": {"paths": ["amount_micros"]}}),
            row(change_event={"change_date_time": stamp, "change_resource_type": "AD_GROUP_AD",
                              "resource_change_operation": "CREATE", "client_type": "GOOGLE_ADS_WEB_CLIENT",
                              "user_email": "someone@example.com", "campaign": "customers/1/campaigns/42",
                              "changed_fields": {"paths": []}}),
        ]

    def test_interface_changes_without_a_log_entry(self):
        fake = self.fake().on("client_type = 'GOOGLE_ADS_WEB_CLIENT'", self._events())
        finding = next(f for f in self.run_audit(fake)["findings"] if "no entry in the decision log" in f["finding"])
        self.assertIn("2 change(s)", finding["finding"])
        self.assertEqual({e["resource"] for e in finding["evidence"]}, {"CAMPAIGN_BUDGET", "AD_GROUP_AD"})

    def test_a_manual_entry_documents_the_change(self):
        audit_log.record_manual(CID, "Bütçe müşteri isteğiyle düşürüldü", resource_type="CAMPAIGN_BUDGET")
        fake = self.fake().on("client_type = 'GOOGLE_ADS_WEB_CLIENT'", self._events())
        finding = next(f for f in self.run_audit(fake)["findings"] if "no entry in the decision log" in f["finding"])
        self.assertIn("1 change(s)", finding["finding"])
        self.assertEqual(finding["evidence"][0]["resource"], "AD_GROUP_AD")
        audit_log.record_manual(CID, "Yeni reklam panelden eklendi")  # any resource type
        self.assertFalse(any("decision log" in t for t in _titles(self.run_audit(fake))))


class TestPortfolioContracts(AuditCase):
    def test_contract_map_is_matched_by_cleaned_id(self):
        seen = {}

        def fake_audit(cid, **kwargs):
            seen[cid] = kwargs.get("lead_contract")
            return {"counts": {"critical": 0, "warning": 0, "note": 0}, "verdict": "ok", "findings": []}

        fake = FakeAds().on("FROM customer_client", [
            row(customer_client={"id": int(CID), "descriptive_name": "A", "manager": False, "status": "ENABLED"})])
        with fake.installed(), patch.object(audit, "_audit_account", fake_audit):
            audit.run_portfolio_audit("9999999999", lead_contracts={"123-456-7890": "FORM_ONLY"})
        self.assertEqual(seen, {CID: "FORM_ONLY"})
