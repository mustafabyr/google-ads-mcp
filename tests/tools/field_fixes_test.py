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

"""The gaps found in the field: batching, removing terms, call fixes, etc."""

import unittest

from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.tools import adgroups, agency, campaigns, conversions, extensions
from ads_mcp.tools import negatives, shared_sets
from tests.fakes import FakeAds, row

CID = "1234567890"


def _set_rows():
    return [row(shared_set={"id": 9, "name": "Master", "type_": "NEGATIVE_KEYWORDS",
                            "status": "ENABLED", "member_count": 3, "reference_count": 2})]


def _term_rows():
    return [
        row(shared_criterion={"criterion_id": 1, "resource_name": f"customers/{CID}/sharedCriteria/9~1",
                              "keyword": {"text": "Bedava", "match_type": "BROAD"}}),
        row(shared_criterion={"criterion_id": 2, "resource_name": f"customers/{CID}/sharedCriteria/9~2",
                              "keyword": {"text": "bedava", "match_type": "PHRASE"}}),
        row(shared_criterion={"criterion_id": 3, "resource_name": f"customers/{CID}/sharedCriteria/9~3",
                              "keyword": {"text": "iş ilanı", "match_type": "BROAD"}}),
    ]


class _FakeGoogleAdsException(GoogleAdsException):
    def __init__(self, message):
        class _Err:
            def __init__(self, m):
                self.message = m

        class _Failure:
            errors = [_Err(message)]

        self.failure = _Failure()
        self.request_id = "fake"

    def __str__(self):
        return self.failure.errors[0].message


class TestSharedListBatching(unittest.TestCase):
    def test_thirty_eight_terms_go_in_four_batches(self):
        fake = FakeAds().on("FROM shared_set", _set_rows()).on("FROM shared_criterion", [])
        terms = [f"terim {i}" for i in range(38)]
        with fake.installed():
            result = shared_sets.add_to_negative_keyword_list(CID, "9", terms)
        calls = fake.calls_to("SharedCriterionService.mutate_shared_criteria")
        self.assertEqual([len(c["operations"]) for c in calls], [10, 10, 10, 8])
        self.assertEqual(result["added"], 38)
        self.assertEqual(result["batches"], 4)
        self.assertNotIn("failed_batches", result)

    def test_a_refused_batch_is_reported_and_the_rest_still_go(self):
        fake = FakeAds().on("FROM shared_set", _set_rows()).on("FROM shared_criterion", [])
        calls_seen = {"n": 0}
        real_writer = fake.service("SharedCriterionService")._writer("mutate_shared_criteria")

        def flaky(request=None, **kwargs):
            calls_seen["n"] += 1
            if calls_seen["n"] == 2:
                fake.calls.append(("SharedCriterionService.mutate_shared_criteria", kwargs))
                raise _FakeGoogleAdsException("POLICY_VIOLATION on batch two")
            return real_writer(request=request, **kwargs)

        fake.service("SharedCriterionService").mutate_shared_criteria = flaky
        with fake.installed():
            result = shared_sets.add_to_negative_keyword_list(CID, "9", [f"t{i}" for i in range(25)])
        self.assertEqual(result["added"], 15)
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["failed_batches"][0]["batch"], 2)
        self.assertEqual(len(result["failed_batches"][0]["terms"]), 10)
        self.assertIn("POLICY_VIOLATION", result["failed_batches"][0]["error"])

    def test_already_present_terms_are_skipped_before_batching(self):
        fake = FakeAds().on("FROM shared_set", _set_rows()).on("FROM shared_criterion", _term_rows())
        with fake.installed():
            result = shared_sets.add_to_negative_keyword_list(CID, "9", ["BEDAVA", "yeni"], match_type="BROAD")
        self.assertEqual(result["already_present"], ["BEDAVA"])
        self.assertEqual(result["added"], 1)


class TestRemoveTerms(unittest.TestCase):
    def test_by_text_removes_every_match_type(self):
        fake = FakeAds().on("FROM shared_set", _set_rows()).on("FROM shared_criterion", _term_rows())
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                shared_sets.remove_terms(CID, "9", keywords=["bedava"])
            self.assertIn("2 campaign(s)", str(ctx.exception))
            self.assertFalse(fake.wrote)
            result = shared_sets.remove_terms(CID, "9", keywords=["bedava", "yok böyle"], confirm_remove=True)
        ops = fake.calls_to("SharedCriterionService.mutate_shared_criteria")[0]["operations"]
        self.assertEqual([o.remove for o in ops],
                         [f"customers/{CID}/sharedCriteria/9~1", f"customers/{CID}/sharedCriteria/9~2"])
        self.assertEqual([r["criterion_id"] for r in result["removed"]], ["1", "2"])
        self.assertEqual(result["not_found"], ["yok böyle"])

    def test_by_text_with_match_type_and_by_id(self):
        fake = FakeAds().on("FROM shared_set", _set_rows()).on("FROM shared_criterion", _term_rows())
        with fake.installed():
            result = shared_sets.remove_terms(CID, "9", keywords=["bedava"], match_type="PHRASE",
                                              criterion_ids=["3"], confirm_remove=True)
        self.assertEqual(sorted(r["criterion_id"] for r in result["removed"]), ["2", "3"])

    def test_nothing_found_changes_nothing(self):
        fake = FakeAds().on("FROM shared_set", _set_rows()).on("FROM shared_criterion", _term_rows())
        with fake.installed():
            result = shared_sets.remove_terms(CID, "9", keywords=["yok"])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["not_found"], ["yok"])
        self.assertFalse(fake.wrote)

    def test_needs_something_to_remove(self):
        with FakeAds().installed():
            with self.assertRaises(ValueError):
                shared_sets.remove_terms(CID, "9")


class TestNegativeBatching(unittest.TestCase):
    def test_campaign_negatives_are_split(self):
        fake = FakeAds()
        with fake.installed():
            result = adgroups.add_negative_keywords(CID, [f"n{i}" for i in range(25)], campaign_id="42")
        calls = fake.calls_to("CampaignCriterionService.mutate_campaign_criteria")
        self.assertEqual([len(c["operations"]) for c in calls], [10, 10, 5])
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["added"], 25)


class TestCallExtensionFix(unittest.TestCase):
    def _existing(self, state="USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION", action=""):
        return [row(
            campaign_asset={"field_type": "CALL", "status": "ENABLED",
                            "resource_name": f"customers/{CID}/campaignAssets/42~7~CALL"},
            asset={"id": 7, "name": "Call", "call_asset": {
                "phone_number": "5551234", "country_code": "TR",
                "call_conversion_reporting_state": state,
                "call_conversion_action": action}},
        )]

    def test_replace_existing_no_longer_raises_key_error(self):
        fake = (FakeAds()
                .on("FROM campaign_asset", self._existing())
                .on("FROM customer", [row(customer={"id": int(CID), "call_reporting_setting": {
                    "call_reporting_enabled": True, "call_conversion_reporting_enabled": True}})]))
        with fake.installed():
            result = extensions.add_call_extension(CID, "42", "0530 407 49 50", replace_existing=True)
        self.assertTrue(result["created"])
        removal = fake.calls_to("CampaignAssetService.mutate_campaign_assets")[0]["operations"][0]
        self.assertEqual(removal.remove, f"customers/{CID}/campaignAssets/42~7~CALL")
        self.assertEqual(result["warnings"], [])

    def test_empty_conversion_action_is_flagged_in_listings(self):
        fake = FakeAds().on("FROM campaign_asset", self._existing("USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"))
        with fake.installed():
            rows = extensions.list_campaign_extensions(CID, "42")
        self.assertIn("counted nowhere", rows[0]["warning"])
        fake = FakeAds().on("FROM campaign_asset", self._existing(
            "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION", f"customers/{CID}/conversionActions/5"))
        with fake.installed():
            rows = extensions.list_campaign_extensions(CID, "42")
        self.assertNotIn("warning", rows[0])


class TestConversionStatus(unittest.TestCase):
    def test_status_is_refused_with_the_panel_note(self):
        fake = FakeAds()
        with fake.installed():
            for status in ("PAUSED", "HIDDEN", "REMOVED"):
                with self.assertRaises(ValueError) as ctx:
                    conversions.update_conversion_action(CID, "5", status=status, confirm_remove=True)
                self.assertIn("Google Ads interface", str(ctx.exception))
                self.assertIn("no PAUSED", str(ctx.exception))
        self.assertFalse(fake.wrote)
        self.assertEqual(fake.reads, [])

    def test_renaming_still_works(self):
        fake = FakeAds().on("FROM conversion_action", [row(conversion_action={
            "id": 5, "name": "Old", "status": "ENABLED", "category": "SUBMIT_LEAD_FORM"})])
        with fake.installed():
            result = conversions.update_conversion_action(CID, "5", name="New")
        op = fake.calls_to("ConversionActionService.mutate_conversion_actions")[0]["operations"][0]
        self.assertEqual(list(op.update_mask.paths), ["name"])
        self.assertEqual(result["requested_fields"], ["name"])


class TestExpandTurkish(unittest.TestCase):
    def test_back_vowel_consonant_final(self):
        forms = negatives.expand("kurs")["forms"]
        self.assertEqual(forms, {"locative": "kursta", "locative_relative": "kurstaki",
                                 "possessive_locative": "kursunda", "plural": "kurslar"})

    def test_vowel_final_and_front_vowel(self):
        self.assertEqual(negatives.expand("araba")["forms"]["possessive_locative"], "arabasında")
        self.assertEqual(negatives.expand("araba")["forms"]["locative"], "arabada")
        ev = negatives.expand("ev")["forms"]
        self.assertEqual((ev["locative"], ev["possessive_locative"], ev["plural"]), ("evde", "evinde", "evler"))
        self.assertEqual(negatives.expand("okul")["forms"]["possessive_locative"], "okulunda")
        self.assertEqual(negatives.expand("kitap")["forms"]["locative"], "kitapta")
        self.assertEqual(negatives.expand("göz")["forms"]["possessive_locative"], "gözünde")

    def test_multi_word_and_ascii(self):
        result = negatives.expand("Ücretsiz  Kurs")
        self.assertEqual(result["term"], "ücretsiz kurs")
        self.assertIn("ücretsiz kursta", result["suffixed"])
        self.assertIn("ucretsiz kursta", result["all"])
        self.assertIn("ucretsiz kurs", result["ascii"])
        self.assertEqual(len(result["all"]), len(set(result["all"])))
        self.assertEqual(result["count"], 10)

    def test_tool_adds_nothing(self):
        fake = FakeAds()
        with fake.installed():
            negatives.expand_turkish("bedava")
        self.assertEqual(fake.calls, [])
        with self.assertRaises(ValueError):
            negatives.expand("  ")


class TestPortfolioCallsAndSpend(unittest.TestCase):
    def test_new_fields(self):
        fake = (FakeAds()
                .on("customer.descriptive_name", [row(customer={"id": int(CID), "descriptive_name": "A",
                                                                "currency_code": "TRY", "auto_tagging_enabled": True})])
                .on("segments.click_type = 'CALLS'", [row(metrics={"clicks": 4}, segments={"click_type": "CALLS"})])
                .on("metrics.phone_calls FROM campaign", [row(campaign={"id": 1}, metrics={"phone_calls": 2}),
                                                          row(campaign={"id": 2}, metrics={"phone_calls": 3})])
                .on("metrics.all_conversions FROM customer", [row(metrics={"cost_micros": 250_000_000, "clicks": 50,
                                                                            "conversions": 2.0, "all_conversions": 2.0})])
                .on("SELECT metrics.cost_micros FROM customer WHERE segments.date DURING LAST_7_DAYS",
                    [row(metrics={"cost_micros": 0})]))
        with fake.installed():
            result = agency.account_portfolio_overview(customer_ids=[CID])
        account = result["accounts"][0]
        self.assertEqual(account["call_extension_clicks"], 4)
        self.assertEqual(account["phone_calls"], 5)
        self.assertEqual(account["cost_last_7_days"], 0.0)
        self.assertFalse(account["spending_last_7_days"])
        self.assertIn("no spend in the last 7 days", account["flags"])
        self.assertEqual(account["cost"], 250.0)


class TestCreateCallCampaign(unittest.TestCase):
    def test_builds_everything_in_order(self):
        fake = (FakeAds()
                .on("FROM campaign_budget", [row(campaign_budget={"id": 1001, "amount_micros": 300_000_000})])
                .on("FROM campaign WHERE", [row(campaign={"id": 1002, "name": "Çilingir", "status": "PAUSED"})])
                .on("FROM ad_group WHERE", [row(ad_group={"id": 1003, "status": "ENABLED"})])
                .on("FROM ad_group_ad", [row(ad_group_ad={"ad": {"id": 1004}, "status": "ENABLED"})])
                .on("FROM campaign_asset", [])
                .on("FROM customer", [row(customer={"id": int(CID), "call_reporting_setting": {
                    "call_reporting_enabled": True, "call_conversion_reporting_enabled": False}})]))
        with fake.installed():
            result = campaigns.create_call_campaign(
                CID, "Çilingir", "0530 407 49 50", "https://example.com/",
                ["Hemen Arayın", "7/24 Çilingir", "Kapıda Ödeme"], ["Dakikalar içinde kapınızdayız.", "Kadıköy"],
                daily_budget=300, keywords=[{"text": "acil çilingir"}],
            )
        order = [name for name, _ in fake.calls]
        self.assertEqual(order, [
            "CampaignBudgetService.mutate_campaign_budgets",
            "CampaignService.mutate_campaigns",
            "AdGroupService.mutate_ad_groups",
            "AdGroupAdService.mutate_ad_group_ads",
            "AssetService.mutate_assets",
            "CampaignAssetService.mutate_campaign_assets",
            "AdGroupCriterionService.mutate_ad_group_criteria",
        ])
        campaign_op = fake.calls[1][1]["operations"][0]
        self.assertEqual(campaign_op.create.status.name, "PAUSED")
        self.assertFalse(campaign_op.create.network_settings.target_search_network)
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["budget_created"], "1001")
        self.assertEqual(result["keywords_added"], 1)
        self.assertIn("no call-only ad type", result["note"])
        self.assertTrue(any("CONVERSION reporting is OFF" in w for w in result["warnings"]))

    def test_exactly_one_budget_source(self):
        with FakeAds().installed():
            with self.assertRaises(ValueError):
                campaigns.create_call_campaign(CID, "x", "555", "https://e.com", ["a", "b", "c"], ["d", "e"])
            with self.assertRaises(ValueError):
                campaigns.create_call_campaign(CID, "x", "555", "https://e.com", ["a", "b", "c"], ["d", "e"],
                                               daily_budget=1, budget_resource_name="customers/1/campaignBudgets/2")
