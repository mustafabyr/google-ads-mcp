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

"""The proxy: plan mode sends nothing, apply mode checks before it writes."""

import unittest

from google.protobuf import field_mask_pb2

import ads_mcp.utils as utils
from ads_mcp.guardrails import session as S
from tests.fakes import FakeAds, offline_client, row

CID = "1234567890"


def _budget_update(amount_micros=7_000_000, budget_id="55"):
    client = offline_client()
    op = client.get_type("CampaignBudgetOperation")
    op.update.resource_name = f"customers/{CID}/campaignBudgets/{budget_id}"
    op.update.amount_micros = amount_micros
    client.copy_from(
        op.update_mask, field_mask_pb2.FieldMask(paths=["amount_micros"])
    )
    return op


def _budget_create(name="Yeni"):
    client = offline_client()
    op = client.get_type("CampaignBudgetOperation")
    op.create.name = name
    op.create.amount_micros = 100_000_000
    return op


def _read(customer_id, query):
    service = utils.get_googleads_service("GoogleAdsService")
    return [
        r for batch in service.search_stream(customer_id=customer_id, query=query)
        for r in batch.results
    ]


class TestRendering(unittest.TestCase):
    def test_update_renders_masked_fields_only(self):
        rendered = S.render_operation(_budget_update())
        self.assertEqual(rendered["kind"], "update")
        self.assertEqual(rendered["resource_name"], f"customers/{CID}/campaignBudgets/55")
        self.assertEqual(rendered["fields"], {"amount_micros": "7000000"})
        self.assertEqual(rendered["mask"], ["amount_micros"])

    def test_create_and_remove(self):
        created = S.render_operation(_budget_create())
        self.assertEqual(created["kind"], "create")
        self.assertEqual(created["resource_name"], "")
        self.assertEqual(created["fields"]["name"], "Yeni")
        client = offline_client()
        op = client.get_type("CampaignOperation")
        op.remove = f"customers/{CID}/campaigns/9"
        removed = S.render_operation(op)
        self.assertEqual(removed, {"kind": "remove", "resource_name": op.remove,
                                   "fields": {}, "mask": []})

    def test_long_strings_are_shortened(self):
        client = offline_client()
        op = client.get_type("AssetOperation")
        op.create.image_asset.data = b"\x01" * 5000
        rendered = S.render_operation(op)
        self.assertIn("characters omitted", rendered["fields"]["image_asset"]["data"])

    def test_resource_for(self):
        self.assertEqual(S.resource_for("mutate_campaign_budgets"), "campaign_budget")
        self.assertEqual(S.resource_for("mutate_ad_group_criteria"), "ad_group_criterion")
        self.assertEqual(S.resource_for("mutate_campaign_criteria"), "campaign_criterion")
        self.assertEqual(S.resource_for("mutate_ad_group_ads"), "ad_group_ad")
        self.assertEqual(S.resource_for("mutate", "asset_group_operation"), "asset_group")
        self.assertEqual(S.resource_for("upload_click_conversions"), "click_conversion")
        self.assertEqual(S.resource_for("schedule_experiment"), "experiment")

    def test_placeholders_are_sentinels_and_keep_the_parent(self):
        self.assertEqual(
            S.placeholder_name(CID, "campaign_budget", {}, 1),
            f"customers/{CID}/campaignBudgets/9990000000000001",
        )
        self.assertEqual(
            S.placeholder_name(CID, "ad_group_ad", {"ad_group": f"customers/{CID}/adGroups/77"}, 2),
            f"customers/{CID}/adGroupAds/77~9990000000000002",
        )

    def test_compare_reads_ignores_metrics_and_order(self):
        a = S.ReadRecord(CID, "SELECT x", [{"campaign": {"id": 1}, "metrics": {"clicks": 5}},
                                          {"campaign": {"id": 2}}])
        b = S.ReadRecord(CID, "SELECT   x", [{"campaign": {"id": 2}},
                                            {"campaign": {"id": 1}, "metrics": {"clicks": 9}}])
        self.assertIsNone(S.compare_reads(a, b))
        c = S.ReadRecord(CID, "SELECT x", [{"campaign": {"id": 1, "status": "PAUSED"}},
                                          {"campaign": {"id": 2}}])
        problem = S.compare_reads(a, c)
        self.assertIn("account changed", problem)
        self.assertIn("PAUSED", problem)
        d = S.ReadRecord(CID, "SELECT y", [])
        self.assertIn("different query", S.compare_reads(a, d))


class TestPlanMode(unittest.TestCase):
    def test_nothing_is_sent_and_reads_are_recorded(self):
        fake = FakeAds().on("FROM campaign_budget", [row(campaign_budget={"id": 55, "amount_micros": 5_000_000})])
        with fake.installed():
            with S.WriteSession(S.PLAN, tool="t", customer_id=CID) as session:
                rows = _read(CID, "SELECT campaign_budget.id FROM campaign_budget WHERE campaign_budget.id = 55")
                self.assertEqual(len(rows), 1)
                service = utils.get_googleads_service("CampaignBudgetService")
                response = service.mutate_campaign_budgets(
                    customer_id=CID, operations=[_budget_update(), _budget_create()]
                )
                after = _read(CID, "SELECT campaign_budget.id FROM campaign_budget WHERE campaign_budget.id = 9990000000000001")
        self.assertEqual(fake.calls, [])  # no write reached the fake
        self.assertEqual(len(session.reads), 1)  # the read after the write is not a "before"
        self.assertEqual(session.reads[0].rows[0]["campaign_budget"]["amount_micros"], "5000000")
        self.assertTrue(session.wrote)
        self.assertEqual(response.results[0].resource_name, f"customers/{CID}/campaignBudgets/55")
        self.assertEqual(response.results[1].resource_name, f"customers/{CID}/campaignBudgets/9990000000000001")
        write = session.writes[0]
        self.assertEqual(write.method, "mutate_campaign_budgets")
        self.assertEqual(write.operations[0]["resource"], "campaign_budget")
        self.assertEqual(write.operations[1]["kind"], "create")
        self.assertEqual(session.api_calls, 3)

    def test_google_ads_service_mutate_gets_result_fields(self):
        client = offline_client()
        op = client.get_type("MutateOperation")
        op.campaign_operation.create.resource_name = f"customers/{CID}/campaigns/-1"
        op.campaign_operation.create.name = "PMax"
        op2 = client.get_type("MutateOperation")
        op2.asset_group_operation.create.resource_name = f"customers/{CID}/assetGroups/-2"
        fake = FakeAds()
        with fake.installed():
            with S.WriteSession(S.PLAN, tool="t", customer_id=CID) as session:
                service = utils.get_googleads_service("GoogleAdsService")
                response = service.mutate(customer_id=CID, mutate_operations=[op, op2])
        self.assertEqual(response.mutate_operation_responses[0].campaign_result.resource_name,
                         f"customers/{CID}/campaigns/-1")
        self.assertTrue(response.mutate_operation_responses[1]._pb.HasField("asset_group_result"))
        self.assertFalse(response.mutate_operation_responses[1]._pb.HasField("campaign_result"))
        self.assertEqual(session.writes[0].operations[1]["resource"], "asset_group")
        self.assertEqual(fake.calls, [])

    def test_experiment_calls_are_captured(self):
        fake = FakeAds()
        with fake.installed():
            with S.WriteSession(S.PLAN, tool="t", customer_id=CID) as session:
                service = utils.get_googleads_service("ExperimentService")
                service.schedule_experiment(resource_name=f"customers/{CID}/experiments/3")
        self.assertEqual(fake.calls, [])
        op = session.writes[0].operations[0]
        self.assertEqual((op["kind"], op["resource"]), ("schedule", "experiment"))
        self.assertEqual(op["resource_name"], f"customers/{CID}/experiments/3")


class TestApplyMode(unittest.TestCase):
    def _expected(self, amount):
        return [S.ReadRecord(
            CID,
            "SELECT campaign_budget.id FROM campaign_budget WHERE campaign_budget.id = 55",
            [{"campaign_budget": {"id": "55", "amount_micros": str(amount)}}],
        )]

    def test_matching_reads_let_the_write_through(self):
        fake = FakeAds().on("FROM campaign_budget", [row(campaign_budget={"id": 55, "amount_micros": 5_000_000})])
        with fake.installed():
            with S.WriteSession(S.APPLY, tool="t", customer_id=CID,
                                expected_reads=self._expected(5_000_000)) as session:
                _read(CID, "SELECT campaign_budget.id FROM campaign_budget WHERE campaign_budget.id = 55")
                service = utils.get_googleads_service("CampaignBudgetService")
                response = service.mutate_campaign_budgets(customer_id=CID, operations=[_budget_update()])
        self.assertEqual(len(fake.calls_to("CampaignBudgetService.mutate_campaign_budgets")), 1)
        self.assertEqual(response.results[0].resource_name, f"customers/{CID}/campaignBudgets/55")
        self.assertEqual(session.writes[0].results, [f"customers/{CID}/campaignBudgets/55"])
        self.assertFalse(session.writes[0].validate_only)

    def test_changed_state_refuses_before_any_write(self):
        fake = FakeAds().on("FROM campaign_budget", [row(campaign_budget={"id": 55, "amount_micros": 9_000_000})])
        with fake.installed():
            with self.assertRaises(S.PlanPreconditionFailed):
                with S.WriteSession(S.APPLY, tool="t", customer_id=CID,
                                    expected_reads=self._expected(5_000_000)):
                    _read(CID, "SELECT campaign_budget.id FROM campaign_budget WHERE campaign_budget.id = 55")
                    utils.get_googleads_service("CampaignBudgetService").mutate_campaign_budgets(
                        customer_id=CID, operations=[_budget_update()])
        self.assertEqual(fake.calls, [])

    def test_validate_only_builds_a_request_and_returns_placeholders(self):
        fake = FakeAds()
        with fake.installed():
            with S.WriteSession(S.APPLY, tool="t", customer_id=CID, validate_only=True) as session:
                service = utils.get_googleads_service("CampaignBudgetService")
                response = service.mutate_campaign_budgets(customer_id=CID, operations=[_budget_create()])
        call = fake.calls_to("CampaignBudgetService.mutate_campaign_budgets")[0]
        self.assertTrue(call["validate_only"])
        self.assertEqual(call["customer_id"], CID)
        self.assertEqual(len(call["operations"]), 1)
        self.assertEqual(response.results[0].resource_name, f"customers/{CID}/campaignBudgets/9990000000000001")
        self.assertTrue(session.writes[0].validate_only)

    def test_direct_mode_records_reads_before_the_first_write_only(self):
        fake = FakeAds().on("FROM campaign", [row(campaign={"id": 1, "status": "ENABLED"})])
        with fake.installed():
            with S.WriteSession(S.DIRECT, tool="t", customer_id=CID) as session:
                _read(CID, "SELECT campaign.id FROM campaign WHERE campaign.id = 1")
                utils.get_googleads_service("CampaignBudgetService").mutate_campaign_budgets(
                    customer_id=CID, operations=[_budget_update()])
                _read(CID, "SELECT campaign.id FROM campaign WHERE campaign.id = 1")
        self.assertEqual(len(session.reads), 1)
        self.assertEqual(len(session.writes), 1)
        self.assertEqual(len(fake.calls), 1)

    def test_no_session_means_no_proxy(self):
        fake = FakeAds()
        with fake.installed():
            service = utils.get_googleads_service("CampaignBudgetService")
        self.assertNotIsInstance(service, S.ServiceProxy)
        self.assertIsNone(S.current())
