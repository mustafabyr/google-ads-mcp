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

"""Image assets: the ratio decides the slot, and decides it before upload.

The mistake this guards against is real: a brand wordmark is 4:1, Google's
format policy refuses "brand logos used as the image", and an account that
uploads one only finds out when the asset is disapproved. Here the shape is
read from the file's own header and a wrong one never leaves the process.
"""

import base64
import struct
import unittest
import zlib

from ads_mcp.tools import extensions
from tests.fakes import FakeAds, row

CID = "1234567890"


def _png(width: int, height: int) -> bytes:
    """A real PNG of the given size -- header parsing is what is under test."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _jpeg(width: int, height: int) -> bytes:
    """Enough of a JPEG for the frame header to be found."""
    return (
        b"\xff\xd8"
        + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
        + b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 3)
        + b"\x00" * 6
        + b"\xff\xd9"
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class TestShapeReading(unittest.TestCase):
    def test_png_and_jpeg_headers_give_the_same_answer(self):
        self.assertEqual(extensions._image_size(_png(1200, 628)), (1200, 628))
        self.assertEqual(extensions._image_size(_jpeg(1200, 628)), (1200, 628))
        self.assertEqual(
            extensions._image_size(_png(1200, 1200)), (1200, 1200)
        )

    def test_another_format_is_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            extensions._image_size(b"GIF89a\x00\x00")
        self.assertIn("PNG and JPEG", str(ctx.exception))


class TestSlotRouting(unittest.TestCase):
    def test_a_square_goes_in_the_square_slot(self):
        self.assertEqual(
            extensions._image_field_type(1200, 1200),
            "SQUARE_MARKETING_IMAGE",
        )

    def test_a_landscape_goes_in_the_landscape_slot(self):
        self.assertEqual(
            extensions._image_field_type(1200, 628), "MARKETING_IMAGE"
        )

    def test_a_four_to_one_wordmark_is_refused_as_a_logo(self):
        with self.assertRaises(ValueError) as ctx:
            extensions._image_field_type(1200, 300)
        message = str(ctx.exception)
        self.assertIn("4:1", message)
        self.assertIn("LOGO", message)

    def test_a_square_below_the_minimum_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            extensions._image_field_type(200, 200)
        self.assertIn("at least 300x300", str(ctx.exception))

    def test_a_landscape_below_the_minimum_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            extensions._image_field_type(400, 209)
        self.assertIn("at least 600x314", str(ctx.exception))


class TestLinking(unittest.TestCase):
    def _fake(self, existing=None):
        return FakeAds().on("FROM campaign_asset", existing or [])

    def test_each_image_is_linked_in_its_own_slot(self):
        fake = self._fake()
        with fake.installed():
            result = extensions.add_image_assets(
                CID,
                "42",
                images=[
                    {"name": "AVM 1:1", "base64": _b64(_png(1200, 1200))},
                    {"name": "AVM 1.91:1", "base64": _b64(_jpeg(1200, 628))},
                ],
            )
        slots = [i["field_type"] for i in result["uploaded"]]
        self.assertEqual(
            slots, ["SQUARE_MARKETING_IMAGE", "MARKETING_IMAGE"]
        )
        links = fake.calls_to("CampaignAssetService.mutate_campaign_assets")
        self.assertEqual(
            [c["operations"][0].create.field_type.name for c in links],
            ["SQUARE_MARKETING_IMAGE", "MARKETING_IMAGE"],
        )
        self.assertEqual(result["warnings"], [])

    def test_a_logo_is_refused_before_anything_is_uploaded(self):
        fake = self._fake()
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_image_assets(
                    CID,
                    "42",
                    images=[
                        {"name": "iyi", "base64": _b64(_png(1200, 1200))},
                        {"name": "logo", "base64": _b64(_png(1200, 300))},
                    ],
                )
        self.assertIn("LOGO", str(ctx.exception))
        # The good image in the same batch must not have been written either.
        self.assertFalse(fake.wrote)

    def test_landscape_only_is_linked_but_flagged(self):
        fake = self._fake()
        with fake.installed():
            result = extensions.add_image_assets(
                CID,
                "42",
                images=[{"name": "yatay", "base64": _b64(_png(1200, 628))}],
            )
        self.assertIn("no SQUARE image", result["warnings"][0])

    def test_an_image_already_on_the_campaign_is_not_linked_twice(self):
        existing = [row(
            campaign_asset={
                "field_type": "SQUARE_MARKETING_IMAGE", "status": "ENABLED",
                "resource_name":
                    f"customers/{CID}/campaignAssets/42~7~"
                    f"SQUARE_MARKETING_IMAGE"},
            asset={"id": 7, "name": "AVM 1:1", "image_asset": {
                "full_size": {"width_pixels": 1200, "height_pixels": 1200}}},
        )]
        fake = self._fake(existing)
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_image_assets(CID, "42", asset_ids=["7"])
        self.assertIn("Already on this campaign", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_the_twenty_image_ceiling_is_counted_with_what_is_there(self):
        existing = [
            row(
                campaign_asset={
                    "field_type": "SQUARE_MARKETING_IMAGE",
                    "status": "ENABLED",
                    "resource_name":
                        f"customers/{CID}/campaignAssets/42~{i}~"
                        f"SQUARE_MARKETING_IMAGE"},
                asset={"id": i, "name": f"g{i}", "image_asset": {
                    "full_size": {
                        "width_pixels": 1200, "height_pixels": 1200}}},
            )
            for i in range(19)
        ]
        fake = self._fake(existing)
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_image_assets(
                    CID,
                    "42",
                    images=[
                        {"name": "a", "base64": _b64(_png(1200, 1200))},
                        {"name": "b", "base64": _b64(_png(1200, 1200))},
                    ],
                )
        self.assertIn("at most 20", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_nothing_to_do_is_an_error_not_a_silent_success(self):
        fake = FakeAds()
        with fake.installed():
            with self.assertRaises(ValueError):
                extensions.add_image_assets(CID, "42")
        self.assertFalse(fake.wrote)

    def test_a_path_reads_the_file_off_the_server_disk(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "kare.png")
            with open(path, "wb") as handle:
                handle.write(_png(1200, 1200))
            fake = self._fake()
            with fake.installed():
                result = extensions.add_image_assets(
                    CID, "42", images=[{"name": "kare", "path": path}]
                )
        self.assertEqual(
            result["uploaded"][0]["field_type"], "SQUARE_MARKETING_IMAGE"
        )
        self.assertEqual(result["uploaded"][0]["width"], 1200)

    def test_a_path_is_refused_when_the_server_is_serving_http(self):
        import os
        from unittest.mock import patch

        fake = self._fake()
        with fake.installed():
            with patch.dict(
                os.environ, {"GOOGLE_ADS_MCP_TRANSPORT": "http"}
            ):
                with self.assertRaises(ValueError) as ctx:
                    extensions.add_image_assets(
                        CID, "42",
                        images=[{"name": "kare", "path": "/tmp/nope.png"}],
                    )
        self.assertIn("somebody else's machine", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_a_missing_file_says_so(self):
        fake = self._fake()
        with fake.installed():
            with self.assertRaises(ValueError) as ctx:
                extensions.add_image_assets(
                    CID, "42",
                    images=[{"name": "yok", "path": "/tmp/yok-boyle-dosya"}],
                )
        self.assertIn("No file at", str(ctx.exception))
        self.assertFalse(fake.wrote)

    def test_an_image_link_can_be_removed_like_any_other(self):
        fake = self._fake()
        with fake.installed():
            extensions.remove_campaign_extension(
                CID, "42", "7", "SQUARE_MARKETING_IMAGE",
                confirm_remove=True,
            )
        operation = fake.calls_to(
            "CampaignAssetService.mutate_campaign_assets"
        )[0]["operations"][0]
        self.assertTrue(operation.remove.endswith("SQUARE_MARKETING_IMAGE"))


if __name__ == "__main__":
    unittest.main()
