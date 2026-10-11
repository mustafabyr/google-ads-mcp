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

"""Network access is gated, and the day's quota is counted and warned about."""

import asyncio
import datetime
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ads_mcp import server
from ads_mcp.guardrails import access, guard, quota
from ads_mcp.tools import campaigns
from tests.fakes import FakeAds, row


class TestTransportGuard(unittest.TestCase):
    def test_http_without_oauth_is_refused(self):
        with self.assertRaises(server.NetworkAuthMissing) as ctx:
            server.require_network_auth({})
        self.assertIn("OAuth", str(ctx.exception))

    def test_http_without_allow_list_is_refused(self):
        env = {"GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "id", "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "s"}
        with self.assertRaises(server.NetworkAuthMissing) as ctx:
            server.require_network_auth(env)
        self.assertIn(access.ALLOWED_EMAILS_ENV, str(ctx.exception))

    def test_http_with_both_passes(self):
        env = {"GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "id", "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "s",
               access.ALLOWED_EMAILS_ENV: "a@example.com, b@example.com"}
        server.require_network_auth(env)

    def test_transport_choice(self):
        self.assertEqual(server.chosen_transport({}), "stdio")
        self.assertEqual(server.chosen_transport({"GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "i",
                                                  "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "s"}), "http")
        self.assertEqual(server.chosen_transport({"GOOGLE_ADS_MCP_TRANSPORT": "stdio",
                                                  "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "i",
                                                  "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "s"}), "stdio")
        with self.assertRaises(ValueError):
            server.chosen_transport({"GOOGLE_ADS_MCP_TRANSPORT": "carrier-pigeon"})


class TestAllowedEmails(unittest.TestCase):
    def _run(self, email, allowed):
        middleware = access.AllowedEmailsMiddleware()
        calls = []

        async def call_next(context):
            calls.append(context)
            return "ok"

        with patch.dict(os.environ, {access.ALLOWED_EMAILS_ENV: allowed}), \
             patch.object(access, "caller_email", return_value=email):
            result = asyncio.run(middleware.on_request(SimpleNamespace(), call_next))
        return result, calls

    def test_listed_email_passes(self):
        result, calls = self._run("me@example.com", "ME@example.com,other@example.com")
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)

    def test_unlisted_email_is_refused(self):
        with self.assertRaises(access.AccessDenied) as ctx:
            self._run("stranger@example.com", "me@example.com")
        self.assertIn("stranger@example.com", str(ctx.exception))

    def test_no_token_passes_stdio(self):
        result, _ = self._run(None, "me@example.com")
        self.assertEqual(result, "ok")

    def test_no_allow_list_is_inert(self):
        result, _ = self._run("anyone@example.com", "")
        self.assertEqual(result, "ok")

    def test_parsing(self):
        with patch.dict(os.environ, {access.ALLOWED_EMAILS_ENV: " A@x.com ; b@y.com,"}):
            self.assertEqual(access.allowed_emails(), frozenset({"a@x.com", "b@y.com"}))


class TestQuota(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            quota.QUOTA_DIR_ENV: str(pathlib.Path(self.tmp.name) / "quota"),
            quota.QUOTA_GCS_BUCKET_ENV: "",
            quota.DAILY_LIMIT_ENV: "100",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_counts_per_utc_day(self):
        day = datetime.datetime(2026, 9, 18, 23, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(quota.add(3, now=day), 3)
        self.assertEqual(quota.add(2, now=day), 5)
        self.assertEqual(quota.used(now=day), 5)
        next_day = day + datetime.timedelta(hours=2)
        self.assertEqual(quota.used(now=next_day), 0)
        self.assertEqual(quota.status(now=day)["used_pct"], 5.0)

    def test_warning_from_seventy_percent(self):
        self.assertIsNone(quota.warning(69))
        message = quota.warning(70)
        self.assertIn("70 of 100", message)
        self.assertIn("70%", message)

    def test_every_api_call_is_counted_and_results_carry_the_warning(self):
        fake = FakeAds().on("FROM campaign WHERE", [row(campaign={"id": 42, "name": "C", "status": "ENABLED"},
                                                        campaign_budget={"amount_micros": 1_000_000})])
        with fake.installed():
            # installed() points the counter at its own scratch dir.
            before = quota.used()
            campaigns.pause_campaign("1234567890", "42")  # 1 read + 1 write + 1 read-back
            self.assertEqual(quota.used() - before, 3)
            with patch.dict(os.environ, {quota.DAILY_LIMIT_ENV: "4"}):
                result = guard.with_quota_warning({"ok": True})
            self.assertIn("quota_warning", result)
            self.assertEqual(guard.with_quota_warning({"ok": True}).get("quota_warning"), None)
