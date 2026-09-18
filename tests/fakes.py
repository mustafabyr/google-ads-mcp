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

"""An offline stand-in for the Google Ads client, for tool tests.

The real client library is used for everything that needs no network --
types, enums, resource-name helpers, ``copy_from`` -- so operations built by
the tools are real protobuf messages. Only the two things that would reach
Google are faked: ``search_stream`` answers from a script of rows, and every
write method records the request and answers with plausible resource names.

Usage::

    fake = FakeAds()
    fake.on("FROM campaign_budget", [row(campaign_budget={"id": 1, ...})])
    with fake.installed():
        result = update_campaign_budget("123", "1", 400)
    fake.calls  # -> [("CampaignBudgetService.mutate_campaign_budgets", req)]
"""

import contextlib
import re
import warnings
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from unittest.mock import patch

import proto
from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials

warnings.filterwarnings("ignore", category=DeprecationWarning)

# One offline client for every test: building it is slow-ish, and it holds
# no state that matters.
_OFFLINE_CLIENT: Optional[GoogleAdsClient] = None

WRITE_METHODS = {
    "mutate",
    "upload_click_conversions",
    "upload_call_conversions",
    "schedule_experiment",
    "end_experiment",
    "graduate_experiment",
    "promote_experiment",
}


def offline_client() -> GoogleAdsClient:
    global _OFFLINE_CLIENT
    if _OFFLINE_CLIENT is None:
        _OFFLINE_CLIENT = GoogleAdsClient(
            credentials=Credentials(token="offline"),
            developer_token="offline",
            use_proto_plus=True,
        )
    return _OFFLINE_CLIENT


def row(**sections: Dict[str, Any]) -> proto.Message:
    """Builds a GoogleAdsRow from plain dicts, e.g. row(campaign={"id": 1})."""
    Row = type(offline_client().get_type("GoogleAdsRow"))
    return Row(sections)


RowsSpec = Union[List[Any], Callable[[str], List[Any]]]


class FakeService:
    """Stands in for one GAPIC service client."""

    def __init__(self, name: str, ads: "FakeAds"):
        self._name = name
        self._ads = ads
        self._real = offline_client().get_service(name)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("mutate") or attr in WRITE_METHODS:
            return self._writer(attr)
        return getattr(self._real, attr)

    def search_stream(self, customer_id=None, query=None, request=None):
        if request is not None:
            customer_id = request.customer_id
            query = request.query
        rows = self._ads.rows_for(query)
        self._ads.reads.append((customer_id, " ".join(query.split())))
        return [SimpleNamespace(results=rows, field_mask=None)]

    def search(self, customer_id=None, query=None, request=None):
        for batch in self.search_stream(customer_id, query, request):
            for r in batch.results:
                yield r

    def _writer(self, method: str) -> Callable[..., Any]:
        def call(request=None, **kwargs):
            if request is not None:
                # Requests built by the guardrail proxy: unpack the fields
                # the fake cares about so tests can assert on them.
                kwargs = {"request": request}
                for field in ("customer_id", "operations", "mutate_operations",
                              "conversions", "validate_only", "partial_failure"):
                    if hasattr(request, field):
                        kwargs[field] = getattr(request, field)
            self._ads.calls.append((f"{self._name}.{method}", kwargs))
            failure = self._ads.failures.get(f"{self._name}.{method}")
            if failure is not None:
                raise failure
            return self._ads.response_for(self._name, method, kwargs)

        return call


class FakeClient:
    """Stands in for GoogleAdsClient: real types, fake services."""

    def __init__(self, ads: "FakeAds"):
        self._ads = ads
        self._real = offline_client()

    def get_service(self, name: str, interceptors=None) -> FakeService:
        return self._ads.service(name)

    def get_type(self, name: str):
        return self._real.get_type(name)

    @property
    def enums(self):
        return self._real.enums

    def copy_from(self, dst, src):
        return self._real.copy_from(dst, src)


class FakeAds:
    """Scripted reads, recorded writes."""

    def __init__(self) -> None:
        self._reads: List[Tuple[Any, RowsSpec]] = []
        self.reads: List[Tuple[str, str]] = []
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.failures: Dict[str, Exception] = {}
        self._services: Dict[str, FakeService] = {}
        self._counter = 1000

    # --- scripting ----------------------------------------------------------
    def on(self, pattern: str, rows: RowsSpec) -> "FakeAds":
        """Answers any query matching ``pattern`` (regex, whitespace-insensitive)."""
        self._reads.append((re.compile(pattern, re.S), rows))
        return self

    def fail(self, service_method: str, exc: Exception) -> "FakeAds":
        self.failures[service_method] = exc
        return self

    # --- plumbing -----------------------------------------------------------
    def service(self, name: str) -> FakeService:
        if name not in self._services:
            self._services[name] = FakeService(name, self)
        return self._services[name]

    def rows_for(self, query: str) -> List[Any]:
        flat = " ".join(query.split())
        for pattern, rows in self._reads:
            if pattern.search(flat):
                return list(rows(flat)) if callable(rows) else list(rows)
        return []

    def response_for(self, service: str, method: str, kwargs: Dict[str, Any]):
        client = offline_client()
        if method == "mutate":
            responses = []
            for op in kwargs.get("mutate_operations") or []:
                kind = op._pb.WhichOneof("operation")
                inner = getattr(op, kind)
                name = self._resource_name(inner, kind.replace("_operation", ""))
                result_field = kind.replace("_operation", "_result")
                pb = SimpleNamespace(HasField=lambda f, rf=result_field: f == rf)
                responses.append(
                    SimpleNamespace(
                        _pb=pb, **{result_field: SimpleNamespace(resource_name=name)}
                    )
                )
            return SimpleNamespace(
                mutate_operation_responses=responses, partial_failure_error=None
            )
        if method.startswith("upload_"):
            results = [
                SimpleNamespace(conversion_date_time=c.conversion_date_time)
                for c in kwargs.get("conversions") or []
            ]
            return SimpleNamespace(results=results, partial_failure_error=None)
        if method in ("schedule_experiment", "end_experiment",
                      "graduate_experiment", "promote_experiment"):
            return SimpleNamespace(name="operations/fake")
        results = []
        for op in kwargs.get("operations") or []:
            kind = method[len("mutate_"):]
            results.append(
                SimpleNamespace(resource_name=self._resource_name(op, kind))
            )
        return SimpleNamespace(results=results, partial_failure_error=None)

    def _resource_name(self, op: Any, kind: str) -> str:
        which = op._pb.WhichOneof("operation")
        if which == "remove":
            return op.remove
        if which == "update" and op.update.resource_name:
            return op.update.resource_name
        if which == "create" and getattr(op.create, "resource_name", ""):
            return op.create.resource_name
        self._counter += 1
        collection = "".join(
            part.capitalize() if i else part
            for i, part in enumerate(kind.rstrip("s").split("_"))
        ) + "s"
        return f"customers/0/{collection}/{self._counter}"

    @contextlib.contextmanager
    def installed(self):
        """Routes ads_mcp.utils at this fake for the duration."""
        client = FakeClient(self)
        with patch("ads_mcp.utils._get_googleads_client", return_value=client):
            yield self

    # --- assertions ---------------------------------------------------------
    def calls_to(self, service_method: str) -> List[Dict[str, Any]]:
        return [kw for name, kw in self.calls if name == service_method]

    @property
    def wrote(self) -> bool:
        return bool(self.calls)
