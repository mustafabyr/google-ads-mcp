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

"""Account-level extensions: the ones every campaign quietly inherits.

Found live (Oct 2026, a local-service account): two new campaigns were about to launch
without sitelinks of their own and would have inherited account-level
sitelinks saying "Garantili" and "%100 Musteri Memnuniyeti", pointing at
301-redirected URLs. list_campaign_extensions showed the campaigns clean.
These tests pin the listing and the unlink, plain and through the plan.
"""

import os
import pathlib
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from ads_mcp.guardrails import audit_log, classes, guard, limits, plans
from ads_mcp.tools import extensions
from tests.fakes import FakeAds, row
from tests.guardrails.guard_flow_test import guarded

CID = "1234567890"


def _sitelink(asset_id=7, text="Garantili Tank Degisimi"):
    return row(
        customer_asset={
            "field_type": "SITELINK", "status": "ENABLED",
            "resource_name": f"customers/{CID}/customerAssets/{asset_id}~SITELINK"},
        asset={"id": asset_id, "name": "",
               "sitelink_asset": {"link_text": text,
                                  "description1": "Garantilidir"},
               "final_urls": ["https://www.example.com/eski/"]},
    )


class TestListAccountExtensions(unittest.TestCase):
    def test_rows_are_flat_and_keep_the_account_link_name(self):
        fake = FakeAds().on("FROM customer_asset", [_sitelink()])
        with fake.installed():
            out = extensions.list_account_extensions(CID)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "SITELINK")
        self.assertEqual(out[0]["link_text"], "Garantili Tank Degisimi")
        self.assertEqual(
            out[0]["customer_asset_resource_name"],
            f"customers/{CID}/customerAssets/7~SITELINK",
        )
        self.assertNotIn("campaign_asset_resource_name", out[0])
        query = fake.reads[0][1]
        self.assertIn("FROM customer_asset", query)
        self.assertIn("customer_asset.status != 'REMOVED'", query)

    def test_campaign_rows_keep_their_own_key(self):
        flat = extensions._flatten_extension({
            "campaign_asset": {"field_type": "CALLOUT", "status": "ENABLED",
                               "resource_name": "x"},
            "asset": {"id": 1, "callout_asset": {"callout_text": "Esenler"}},
        })
        self.assertEqual(flat["campaign_asset_resource_name"], "x")
        self.assertNotIn("customer_asset_resource_name", flat)


class TestRemoveAccountExtension(unittest.TestCase):
    def test_sends_one_remove_on_the_account_link(self):
        fake = FakeAds().on("FROM customer_asset", [_sitelink()])
        with fake.installed():
            out = extensions.remove_account_extension(
                CID, "7", "sitelink", confirm_remove=True
            )
        op = fake.calls_to(
            "CustomerAssetService.mutate_customer_assets"
        )[0]["operations"][0]
        self.assertEqual(op.remove, f"customers/{CID}/customerAssets/7~SITELINK")
        self.assertEqual(out["removed"]["link_text"], "Garantili Tank Degisimi")
        before_query = fake.reads[0][1]
        self.assertIn("asset.id = 7", before_query)
        self.assertIn("customer_asset.field_type = 'SITELINK'", before_query)

    def test_needs_the_confirmation(self):
        fake = FakeAds()
        with fake.installed():
            with self.assertRaises(ValueError):
                extensions.remove_account_extension(CID, "7", "SITELINK")
        self.assertFalse(fake.wrote)

    def test_a_link_that_is_not_there_is_refused_without_writing(self):
        fake = FakeAds().on("FROM customer_asset", [])
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.remove_account_extension(
                    CID, "7", "CALLOUT", confirm_remove=True
                )
        self.assertIn("remove_campaign_extension", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_unknown_field_type_is_refused(self):
        fake = FakeAds()
        with fake.installed():
            with self.assertRaises(ValueError):
                extensions.remove_account_extension(
                    CID, "7", "MOBILE_APP", confirm_remove=True
                )
        self.assertFalse(fake.wrote)

    def test_class_is_destructive_and_listing_is_read(self):
        self.assertEqual(
            classes.TOOL_CLASSES["extensions.remove_account_extension"],
            classes.DESTRUCTIVE,
        )
        self.assertEqual(
            classes.TOOL_CLASSES["extensions.list_account_extensions"], classes.READ
        )


class TestRemoveAccountExtensionThroughThePlan(unittest.TestCase):
    def setUp(self):
        import ads_mcp.coordinator  # noqa: F401 - mounts and guards every tool

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        (root / "limits.yaml").write_text(textwrap.dedent(f"""
            accounts:
              "{CID}": {{max_daily_budget_try: 4500, max_cpc_ceiling_try: 20}}
        """))
        env = patch.dict(os.environ, {
            limits.LIMITS_PATH_ENV: str(root / "limits.yaml"),
            plans.PLAN_DIR_ENV: str(root / "plans"),
            audit_log.AUDIT_DIR_ENV: str(root / "audit"),
            guard.REQUIRE_DRY_RUN_ENV: "",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_plans_then_applies_only_with_confirm_destructive(self):
        remove = guarded("extensions", "remove_account_extension")
        apply_plan = guarded("plans", "apply_plan")
        fake = FakeAds().on("FROM customer_asset", [_sitelink()])
        with fake.installed():
            plan = remove(CID, "7", "SITELINK", confirm_remove=True)
        self.assertEqual(plan["class"], classes.DESTRUCTIVE)
        self.assertEqual(fake.calls, [])
        change = plan["changes"][0]
        self.assertEqual(change["kind"], "remove")
        self.assertEqual(
            change["resource_name"], f"customers/{CID}/customerAssets/7~SITELINK"
        )
        with fake.installed():
            with self.assertRaises(plans.DestructiveConfirmationRequired):
                apply_plan(plan["plan_id"])
            self.assertEqual(fake.calls, [])
            result = apply_plan(plan["plan_id"], confirm_destructive=True)
        self.assertEqual(result["status"], plans.APPLIED)
        self.assertTrue(fake.wrote)


class TestInheritedAccountExtensionsAudit(unittest.TestCase):
    def _fake(self, own_rows):
        return (
            FakeAds()
            .on("FROM customer_asset", [_sitelink()])
            .on("FROM campaign_asset", own_rows)
            .on("FROM campaign WHERE", [
                row(campaign={"id": 1, "name": "Servis", "status": "ENABLED"}),
                row(campaign={"id": 2, "name": "Tank", "status": "ENABLED"}),
            ])
        )

    def test_campaign_without_own_sitelinks_is_flagged(self):
        from ads_mcp.tools import audit

        own = [row(campaign={"id": 2},
                   campaign_asset={"field_type": "SITELINK", "status": "ENABLED"})]
        f = audit._Findings()
        with self._fake(own).installed():
            audit._check_inherited_account_extensions(CID, f)
        self.assertEqual(len(f.items), 1)
        self.assertEqual(f.items[0]["evidence"]["campaigns"], ["Servis"])
        self.assertEqual(
            f.items[0]["evidence"]["account_assets"][0]["text"],
            "Garantili Tank Degisimi",
        )

    def test_every_campaign_with_its_own_is_quiet(self):
        from ads_mcp.tools import audit

        own = [row(campaign={"id": i},
                   campaign_asset={"field_type": "SITELINK", "status": "ENABLED"})
               for i in (1, 2)]
        f = audit._Findings()
        with self._fake(own).installed():
            audit._check_inherited_account_extensions(CID, f)
        self.assertEqual(f.items, [])


if __name__ == "__main__":
    unittest.main()
