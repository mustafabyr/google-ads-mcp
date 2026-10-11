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

"""The price extension: the only place a figure belongs in a search ad.

A price written into a headline goes stale silently. These tests pin the
things that make the asset version safe instead: the money reaches Google in
the account's own currency and in micros, the rows Google would reject are
refused here with their own text, and a refresh replaces the table rather
than adding a second one to the same slot.
"""

import unittest

from ads_mcp.tools import adgroups, extensions
from tests.fakes import FakeAds, row

CID = "1234567890"
URL = "https://www.example.com/landing/konut"


def _customer(currency="EUR"):
    return [row(customer={"id": int(CID), "currency_code": currency})]


def _offerings():
    return [
        {"header": "1+1 Daire", "description": "Pesin fiyat", "price": 2850000,
         "url": URL},
        {"header": "2+1 Daire", "description": "Pesin fiyat", "price": 4400000,
         "url": URL},
        {"header": "3+1 Daire", "description": "Pesin fiyat", "price": 7250000,
         "url": URL},
    ]


def _existing_price():
    return [row(
        campaign_asset={
            "field_type": "PRICE", "status": "ENABLED",
            "resource_name": f"customers/{CID}/campaignAssets/42~7~PRICE"},
        asset={"id": 7, "name": "Price PRODUCT_TIERS 2026-08-01 abc123",
               "price_asset": {
                   "type_": "PRODUCT_TIERS", "price_qualifier": "FROM",
                   "language_code": "tr",
                   "price_offerings": [{
                       "header": "1+1 Daire", "description": "Eski fiyat",
                       "price": {"currency_code": "TRY",
                                 "amount_micros": 2_700_000_000_000},
                       "final_url": URL}]}},
    )]


class TestMoneyAndCurrency(unittest.TestCase):
    def test_the_human_number_goes_out_as_micros(self):
        fake = FakeAds().on("FROM customer", _customer())
        with fake.installed():
            result = extensions.add_price_asset(CID, "42", _offerings())
        operation = fake.calls_to(
            "AssetService.mutate_assets"
        )[0]["operations"][0]
        rows = operation.create.price_asset.price_offerings
        self.assertEqual(
            [o.price.amount_micros for o in rows],
            [2_850_000_000_000, 4_400_000_000_000, 7_250_000_000_000],
        )
        self.assertEqual({o.price.currency_code for o in rows}, {"EUR"})
        self.assertEqual(result["currency_code"], "EUR")
        self.assertTrue(result["created"])

    def test_the_account_currency_is_taken_when_none_is_given(self):
        fake = FakeAds().on("FROM customer", _customer("SEK"))
        with fake.installed():
            result = extensions.add_price_asset(CID, "42", _offerings())
        self.assertEqual(result["currency_code"], "SEK")

    def test_another_currency_is_refused_rather_than_converted(self):
        fake = FakeAds().on("FROM customer", _customer("EUR"))
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_price_asset(
                    CID, "42", _offerings(), currency_code="USD"
                )
        self.assertIn("does not convert", str(ctx.exception))
        self.assertFalse(fake.wrote)


class TestMarketsGoogleDoesNotSupport(unittest.TestCase):
    """Google answers this with two opaque INVALID_ARGUMENTs, after writing
    nothing. The refusal belongs here, with the reason attached."""

    def test_a_try_account_cannot_have_a_price_extension_at_all(self):
        fake = FakeAds().on("FROM customer", _customer("TRY"))
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_price_asset(
                    CID, "42", _offerings(), language_code="en"
                )
        message = str(ctx.exception)
        self.assertIn("TRY", message)
        self.assertIn("this account's own currency", message)
        self.assertIn("sitelink descriptions or callouts", message)
        self.assertFalse(fake.wrote)

    def test_turkish_is_not_one_of_the_ten_languages(self):
        fake = FakeAds().on("FROM customer", _customer("EUR"))
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_price_asset(
                    CID, "42", _offerings(), language_code="tr"
                )
        self.assertIn(
            "does not support price assets in 'tr'", str(ctx.exception)
        )
        self.assertFalse(fake.wrote)

    def test_a_region_tag_is_read_by_its_language(self):
        fake = FakeAds().on("FROM customer", _customer("GBP"))
        with fake.installed():
            result = extensions.add_price_asset(
                CID, "42", _offerings(), language_code="en-GB"
            )
        self.assertTrue(result["created"])


class TestRowsGoogleWouldReject(unittest.TestCase):
    def _refuses(self, offerings, fragment, **kwargs):
        fake = FakeAds().on("FROM customer", _customer())
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_price_asset(CID, "42", offerings, **kwargs)
        self.assertIn(fragment, str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_two_rows_are_not_a_table(self):
        self._refuses(_offerings()[:2], "3-8 rows")

    def test_nine_rows_are_too_many(self):
        rows = [
            {"header": f"Tip {i}", "description": "Pesin fiyat",
             "price": 1000 * (i + 1), "url": URL}
            for i in range(9)
        ]
        self._refuses(rows, "3-8 rows")

    def test_description_cannot_repeat_the_header(self):
        rows = _offerings()
        rows[0]["description"] = rows[0]["header"]
        self._refuses(rows, "PRICE_HEADER_SAME_AS_DESCRIPTION")

    def test_two_rows_cannot_share_a_header(self):
        rows = _offerings()
        rows[1]["header"] = rows[0]["header"]
        self._refuses(rows, "carry the header")

    def test_a_long_header_fails_with_its_own_length(self):
        rows = _offerings()
        rows[0]["header"] = "1+1 Daire Merkez Saray Projesi"
        self._refuses(rows, "limit is 25")

    def test_a_row_without_a_url_is_refused(self):
        rows = _offerings()
        rows[2].pop("url")
        self._refuses(rows, "'header', 'description' and 'url'")

    def test_a_price_of_zero_is_refused(self):
        rows = _offerings()
        rows[0]["price"] = 0
        self._refuses(rows, "above zero")

    def test_an_invented_price_type_is_refused(self):
        self._refuses(_offerings(), "price_type must be", price_type="FLATS")

    def test_an_invented_unit_is_refused(self):
        rows = _offerings()
        rows[0]["unit"] = "PER_SQUARE_METRE"
        self._refuses(rows, "'unit' must be one of")


class TestOneTablePerCampaign(unittest.TestCase):
    def test_a_second_table_is_not_added_silently(self):
        fake = (FakeAds()
                .on("FROM customer", _customer())
                .on("FROM campaign_asset", _existing_price()))
        with fake.installed():
            result = extensions.add_price_asset(CID, "42", _offerings())
        self.assertFalse(result["created"])
        self.assertIn("replace_existing=True", result["note"])
        self.assertFalse(fake.wrote)

    def test_a_refresh_unlinks_the_old_table(self):
        fake = (FakeAds()
                .on("FROM customer", _customer())
                .on("FROM campaign_asset", _existing_price()))
        with fake.installed():
            result = extensions.add_price_asset(
                CID, "42", _offerings(), replace_existing=True
            )
        links = fake.calls_to("CampaignAssetService.mutate_campaign_assets")
        # The new table is linked before the old one goes, so the campaign is
        # never a moment without a price extension.
        self.assertEqual(
            links[0]["operations"][0].create.field_type.name, "PRICE"
        )
        self.assertEqual(
            links[1]["operations"][0].remove,
            f"customers/{CID}/campaignAssets/42~7~PRICE",
        )
        # proto-plus renders an int64 id as a string in the dict.
        self.assertEqual(
            [str(i) for i in result["replaced_asset_ids"]], ["7"]
        )
        self.assertTrue(result["created"])

    def test_the_generated_name_carries_a_digest_of_the_rows(self):
        fake = FakeAds().on("FROM customer", _customer())
        with fake.installed():
            first = extensions.add_price_asset(CID, "42", _offerings())
            dearer = _offerings()
            dearer[0]["price"] = 2900000
            second = extensions.add_price_asset(CID, "42", dearer)
        # Asset names are immutable and must be unique in the account, so a
        # changed list must not reuse the name of the one it replaces.
        self.assertNotEqual(first["asset_name"], second["asset_name"])


class TestListingReadsTheTableBack(unittest.TestCase):
    def test_money_comes_back_out_of_micros(self):
        fake = FakeAds().on("FROM campaign_asset", _existing_price())
        with fake.installed():
            rows = extensions.list_campaign_extensions(CID, "42")
        table = rows[0]
        self.assertEqual(table["type"], "PRICE")
        self.assertEqual(table["price_type"], "PRODUCT_TIERS")
        self.assertEqual(table["offerings"][0]["price"], 2_700_000)
        self.assertEqual(table["offerings"][0]["currency"], "TRY")
        self.assertEqual(table["offerings"][0]["header"], "1+1 Daire")
        # The price extension's own type must not masquerade as the record's
        # asset field type, and the raw proto list must not survive.
        self.assertNotIn("type_", table)
        self.assertNotIn("price_offerings", table)


class TestPriceLinkIsRemovable(unittest.TestCase):
    def test_price_can_be_unlinked_like_any_other_extension(self):
        fake = FakeAds().on("FROM campaign_asset", [])
        with fake.installed():
            extensions.remove_campaign_extension(
                CID, "42", "7", "PRICE", confirm_remove=True
            )
        operation = fake.calls_to(
            "CampaignAssetService.mutate_campaign_assets"
        )[0]["operations"][0]
        self.assertEqual(
            operation.remove, f"customers/{CID}/campaignAssets/42~7~PRICE"
        )

    def test_removal_still_needs_the_confirmation(self):
        fake = FakeAds()
        with fake.installed():
            with self.assertRaises(ValueError):
                extensions.remove_campaign_extension(CID, "42", "7", "PRICE")
        self.assertFalse(fake.wrote)


class TestAdCanBeBornPaused(unittest.TestCase):
    def _ad(self, **kwargs):
        return adgroups.create_responsive_search_ad(
            CID, "9", URL,
            headlines=["Merkez Saray", "Daire Fiyatlari", "Hemen Basvur"],
            descriptions=["Ornek aciklama bir.", "Ornek aciklama iki."],
            **kwargs,
        )

    def test_default_is_still_enabled(self):
        fake = FakeAds().on("FROM ad_group_ad", [])
        with fake.installed():
            result = self._ad()
        operation = fake.calls_to(
            "AdGroupAdService.mutate_ad_group_ads"
        )[0]["operations"][0]
        self.assertEqual(operation.create.status.name, "ENABLED")
        self.assertEqual(result["status"], "ENABLED")

    def test_paused_is_reachable_without_a_live_window(self):
        fake = FakeAds().on("FROM ad_group_ad", [])
        with fake.installed():
            result = self._ad(status="PAUSED")
        operation = fake.calls_to(
            "AdGroupAdService.mutate_ad_group_ads"
        )[0]["operations"][0]
        self.assertEqual(operation.create.status.name, "PAUSED")
        self.assertEqual(result["status"], "PAUSED")
        self.assertIn("Created PAUSED", result["note"])

    def test_removed_is_not_a_status_an_ad_is_created_with(self):
        fake = FakeAds()
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                self._ad(status="REMOVED")
        self.assertIn("remove operation", str(ctx.exception))
        self.assertFalse(fake.wrote)


if __name__ == "__main__":
    unittest.main()
