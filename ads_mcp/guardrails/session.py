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

"""A write session: what a tool read, what it tried to write, in what mode.

Every tool talks to Google through ``utils.get_googleads_service``. While a
session is active that call hands back a proxy instead of the raw service,
and the proxy sees every read and every write the tool makes. The tool
itself does not change; the session decides what a write means:

  plan    nothing is sent. Each write is captured as an operation and the
          tool gets back a plausible response with placeholder resource
          names, so it runs to the end and its own validation runs too.
          Reads pass through and are recorded: they are the "before".
  apply   reads made BEFORE the first write are compared with the ones the
          plan recorded; a difference means the account changed since the
          plan was made and the write is refused before it happens. Writes
          are sent for real -- or, with ``validate_only``, sent for
          validation only and answered with placeholders again.
  direct  writes go straight through; reads before the first write are
          recorded for the audit line.

The proxy never rewrites an operation. It records a rendering of it for
the plan and the audit log, and passes the original object to Google.
"""

import contextvars
import dataclasses
import functools
import json
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import proto

PLAN = "plan"
APPLY = "apply"
DIRECT = "direct"

READ_METHODS = frozenset({"search", "search_stream"})

# Write RPCs that do not follow the mutate_* naming.
_OTHER_WRITES = frozenset(
    {
        "mutate",
        "upload_click_conversions",
        "upload_call_conversions",
        "schedule_experiment",
        "end_experiment",
        "graduate_experiment",
        "promote_experiment",
    }
)

# Request types for those, and the request field that carries the payload.
_REQUEST_TYPES = {
    "mutate": "MutateGoogleAdsRequest",
    "upload_click_conversions": "UploadClickConversionsRequest",
    "upload_call_conversions": "UploadCallConversionsRequest",
    "schedule_experiment": "ScheduleExperimentRequest",
    "end_experiment": "EndExperimentRequest",
    "graduate_experiment": "GraduateExperimentRequest",
    "promote_experiment": "PromoteExperimentRequest",
}

# Resource -> (collection segment in a resource name, parent field on the
# message whose id forms the first half of a compound id).
_COLLECTIONS = {
    "campaign_budget": ("campaignBudgets", None),
    "campaign": ("campaigns", None),
    "ad_group": ("adGroups", None),
    "ad_group_ad": ("adGroupAds", "ad_group"),
    "ad_group_criterion": ("adGroupCriteria", "ad_group"),
    "campaign_criterion": ("campaignCriteria", "campaign"),
    "shared_criterion": ("sharedCriteria", "shared_set"),
    "shared_set": ("sharedSets", None),
    "campaign_shared_set": ("campaignSharedSets", "campaign"),
    "asset": ("assets", None),
    "campaign_asset": ("campaignAssets", "campaign"),
    "campaign_asset_set": ("campaignAssetSets", "campaign"),
    "conversion_action": ("conversionActions", None),
    "label": ("labels", None),
    "campaign_label": ("campaignLabels", "campaign"),
    "experiment": ("experiments", None),
    "experiment_arm": ("experimentArms", "experiment"),
    "asset_group": ("assetGroups", None),
    "asset_group_signal": ("assetGroupSignals", "asset_group"),
    "asset_group_asset": ("assetGroupAssets", "asset_group"),
    "ad": ("ads", None),
    "campaign_conversion_goal": ("campaignConversionGoals", "campaign"),
}

# Longer strings than this in a rendered operation are replaced by a
# length note: an image asset carries the whole file as base64.
_MAX_RENDERED_STRING = 400

_SESSION: contextvars.ContextVar[Optional["WriteSession"]] = (
    contextvars.ContextVar("ads_mcp_write_session", default=None)
)


class PlanPreconditionFailed(RuntimeError):
    """The account no longer looks the way the plan assumed."""


@dataclasses.dataclass
class ReadRecord:
    customer_id: str
    query: str
    rows: List[Dict[str, Any]]

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "ReadRecord":
        return cls(
            customer_id=str(data.get("customer_id", "")),
            query=str(data.get("query", "")),
            rows=list(data.get("rows") or []),
        )


@dataclasses.dataclass
class WriteRecord:
    service: str
    method: str
    customer_id: str
    operations: List[Dict[str, Any]]
    results: List[str]
    validate_only: bool = False
    request_fields: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "WriteRecord":
        return cls(
            service=data.get("service", ""),
            method=data.get("method", ""),
            customer_id=str(data.get("customer_id", "")),
            operations=list(data.get("operations") or []),
            results=list(data.get("results") or []),
            validate_only=bool(data.get("validate_only")),
            request_fields=dict(data.get("request_fields") or {}),
        )


def normalize_query(query: Any) -> str:
    return " ".join(str(query or "").split())


def row_to_dict(row: Any) -> Dict[str, Any]:
    """A compact, JSON-safe rendering of a GoogleAdsRow."""
    if isinstance(row, proto.Message):
        return proto.Message.to_dict(
            row,
            use_integers_for_enums=False,
            always_print_fields_with_no_presence=False,
        )
    if isinstance(row, dict):
        return row
    return {"value": str(row)}


def _shrink(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_RENDERED_STRING:
        return f"<{len(value)} characters omitted>"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def is_write_method(name: str) -> bool:
    return name.startswith("mutate") or name in _OTHER_WRITES


def resource_for(method: str, operation_oneof: Optional[str] = None) -> str:
    """The resource a write method touches, as a GAQL-style name."""
    if method == "mutate" and operation_oneof:
        return operation_oneof[: -len("_operation")]
    if method.startswith("upload_"):
        return method[len("upload_") : -1]
    if method.endswith("_experiment"):
        return "experiment"
    plural = method[len("mutate_") :] if method.startswith("mutate_") else method
    if plural.endswith("criteria"):
        return plural[: -len("criteria")] + "criterion"
    if plural.endswith("ies"):
        return plural[:-3] + "y"
    if plural.endswith("s"):
        return plural[:-1]
    return plural


def render_operation(operation: Any) -> Dict[str, Any]:
    """Turns a *Operation message into {kind, resource_name, fields, mask}."""
    which = operation._pb.WhichOneof("operation")
    if which == "remove":
        return {
            "kind": "remove",
            "resource_name": operation.remove,
            "fields": {},
            "mask": [],
        }
    message = getattr(operation, which)
    fields = proto.Message.to_dict(
        message,
        use_integers_for_enums=False,
        always_print_fields_with_no_presence=False,
    )
    resource_name = fields.pop("resource_name", "")
    mask = []
    if which == "update":
        mask = list(operation.update_mask.paths)
    return {
        "kind": which,
        "resource_name": resource_name,
        "fields": _shrink(fields),
        "mask": mask,
    }


def render_message(message: Any) -> Dict[str, Any]:
    if isinstance(message, proto.Message):
        return _shrink(
            proto.Message.to_dict(
                message,
                use_integers_for_enums=False,
                always_print_fields_with_no_presence=False,
            )
        )
    return {"value": _shrink(message)}


def placeholder_name(
    customer_id: str, resource: str, fields: Dict[str, Any], n: int
) -> str:
    """A resource name for an object that does not exist yet.

    Negative ids are Google's own convention for temporary resources, and
    GAQL accepts them in a WHERE clause and matches nothing, so a tool's
    read-back after a captured create returns no rows instead of failing.
    """
    collection, parent = _COLLECTIONS.get(resource, (resource + "s", None))
    if parent and fields.get(parent):
        parent_id = str(fields[parent]).rsplit("/", 1)[-1]
        return f"customers/{customer_id}/{collection}/{parent_id}~-{n}"
    return f"customers/{customer_id}/{collection}/-{n}"


def _strip_volatile(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ("metrics", "segments")}


def _canonical(rows: List[Dict[str, Any]]) -> List[str]:
    return sorted(
        json.dumps(_strip_volatile(r), sort_keys=True, default=str)
        for r in rows
    )


def compare_reads(expected: ReadRecord, actual: ReadRecord) -> Optional[str]:
    """None when the read still matches the plan, else why not."""
    if normalize_query(expected.query) != normalize_query(actual.query):
        return (
            "The tool now runs a different query than when the plan was "
            "made (was the server updated in between?). Make a new plan.\n"
            f"planned: {expected.query[:300]}\n"
            f"now:     {actual.query[:300]}"
        )
    before, now = _canonical(expected.rows), _canonical(actual.rows)
    if before == now:
        return None
    gone = [r for r in before if r not in now]
    new = [r for r in now if r not in before]
    return (
        "The account changed after the plan was made, so the plan's "
        "'before' no longer holds. Read the current state and make a new "
        "plan.\n"
        f"query: {expected.query[:200]}\n"
        f"planned state: {'; '.join(gone)[:600] or '(no rows)'}\n"
        f"current state: {'; '.join(new)[:600] or '(no rows)'}"
    )


class WriteSession:
    """Context manager that puts the proxy in front of every service."""

    def __init__(
        self,
        mode: str,
        *,
        tool: str = "",
        customer_id: str = "",
        validate_only: bool = False,
        expected_reads: Optional[List[ReadRecord]] = None,
    ) -> None:
        if mode not in (PLAN, APPLY, DIRECT):
            raise ValueError(f"Unknown session mode {mode!r}.")
        self.mode = mode
        self.tool = tool
        self.customer_id = customer_id
        self.validate_only = validate_only
        self.expected_reads: List[ReadRecord] = list(expected_reads or [])
        self.reads: List[ReadRecord] = []
        self.writes: List[WriteRecord] = []
        self.wrote = False
        self.api_calls = 0
        self._placeholders = 0
        self._token: Any = None

    def __enter__(self) -> "WriteSession":
        self._token = _SESSION.set(self)
        return self

    def __exit__(self, *exc: Any) -> None:
        _SESSION.reset(self._token)

    @staticmethod
    def current() -> Optional["WriteSession"]:
        return _SESSION.get()

    def next_placeholder(self) -> int:
        self._placeholders += 1
        return self._placeholders

    def record_read(
        self, customer_id: Any, query: Any, rows: List[Dict[str, Any]]
    ) -> None:
        self.api_calls += 1
        if self.wrote:
            return
        record = ReadRecord(str(customer_id or ""), normalize_query(query), rows)
        if self.mode == APPLY and len(self.reads) < len(self.expected_reads):
            problem = compare_reads(self.expected_reads[len(self.reads)], record)
            if problem:
                raise PlanPreconditionFailed(problem)
        self.reads.append(record)

    def record_write(self, record: WriteRecord) -> None:
        self.api_calls += 1
        self.writes.append(record)
        self.wrote = True

    @property
    def rows(self) -> List[Dict[str, Any]]:
        return [row for read in self.reads for row in read.rows]

    def summary(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "reads": len(self.reads),
            "writes": len(self.writes),
            "operations": sum(len(w.operations) for w in self.writes),
        }


def current() -> Optional[WriteSession]:
    return _SESSION.get()


def wrap_service(service: Any, name: str) -> Any:
    session = _SESSION.get()
    if session is None:
        return service
    return ServiceProxy(service, name, session)


def _camel(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


class _HasField:
    def __init__(self, field: str) -> None:
        self._field = field

    def HasField(self, name: str) -> bool:  # noqa: N802 - protobuf API
        return name == self._field

    def WhichOneof(self, _name: str) -> str:  # noqa: N802 - protobuf API
        return self._field


class ServiceProxy:
    """Sees every call a tool makes on one service."""

    def __init__(self, real: Any, name: str, session: WriteSession) -> None:
        self._real = real
        self._name = name
        self._session = session

    def __getattr__(self, attr: str) -> Any:
        if attr in READ_METHODS:
            return functools.partial(self._read, attr)
        if is_write_method(attr):
            return functools.partial(self._write, attr)
        return getattr(self._real, attr)

    # --- reads --------------------------------------------------------------
    def _read(self, method: str, request: Any = None, **kwargs: Any) -> Any:
        customer_id = kwargs.get("customer_id")
        query = kwargs.get("query")
        if request is not None:
            customer_id = getattr(request, "customer_id", customer_id)
            query = getattr(request, "query", query)
        real = getattr(self._real, method)
        result = real(request, **kwargs) if request is not None else real(**kwargs)
        if method == "search_stream":
            batches = list(result)
            rows = [row for batch in batches for row in batch.results]
            self._session.record_read(
                customer_id, query, [row_to_dict(r) for r in rows]
            )
            return batches
        rows = list(result)
        self._session.record_read(
            customer_id, query, [row_to_dict(r) for r in rows]
        )
        return rows

    # --- writes -------------------------------------------------------------
    def _unpack(self, method: str, request: Any, kwargs: Dict[str, Any]):
        fields: Dict[str, Any] = dict(kwargs)
        if request is not None:
            for name in (
                "customer_id",
                "operations",
                "mutate_operations",
                "conversions",
                "partial_failure",
                "validate_only",
                "resource_name",
                "experiment",
                "campaign_budget_mappings",
            ):
                if hasattr(request, name):
                    fields.setdefault(name, getattr(request, name))
        customer_id = str(fields.get("customer_id") or self._session.customer_id)
        if method == "mutate":
            payload = list(fields.get("mutate_operations") or [])
        elif method.startswith("upload_"):
            payload = list(fields.get("conversions") or [])
        elif method.endswith("_experiment"):
            payload = []
        else:
            payload = list(fields.get("operations") or [])
        extra = {
            k: v
            for k, v in fields.items()
            if k in ("partial_failure", "validate_only")
        }
        if method.endswith("_experiment"):
            extra["target"] = str(
                fields.get("resource_name") or fields.get("experiment") or ""
            )
        return customer_id, payload, extra

    def _render(self, method: str, payload: List[Any], extra: Dict[str, Any]):
        rendered: List[Dict[str, Any]] = []
        if method == "mutate":
            for operation in payload:
                oneof = operation._pb.WhichOneof("operation")
                inner = render_operation(getattr(operation, oneof))
                inner["resource"] = resource_for(method, oneof)
                inner["operation_type"] = oneof
                rendered.append(inner)
        elif method.startswith("upload_"):
            for conversion in payload:
                rendered.append(
                    {
                        "kind": "upload",
                        "resource": resource_for(method),
                        "resource_name": getattr(
                            conversion, "conversion_action", ""
                        ),
                        "fields": render_message(conversion),
                        "mask": [],
                    }
                )
        elif method.endswith("_experiment"):
            rendered.append(
                {
                    "kind": method.split("_", 1)[0],
                    "resource": "experiment",
                    "resource_name": extra.get("target", ""),
                    "fields": {},
                    "mask": [],
                }
            )
        else:
            resource = resource_for(method)
            for operation in payload:
                inner = render_operation(operation)
                inner["resource"] = resource
                rendered.append(inner)
        return rendered

    def _placeholders(self, customer_id: str, rendered: List[Dict[str, Any]]):
        names = []
        for op in rendered:
            if op["resource_name"]:
                names.append(op["resource_name"])
            else:
                names.append(
                    placeholder_name(
                        customer_id,
                        op["resource"],
                        op["fields"],
                        self._session.next_placeholder(),
                    )
                )
        return names

    @staticmethod
    def _synthetic(method: str, rendered: List[Dict[str, Any]], names: List[str],
                   payload: List[Any]) -> Any:
        if method == "mutate":
            responses = []
            for op, name in zip(rendered, names):
                result_field = op["operation_type"].replace("_operation", "_result")
                responses.append(
                    SimpleNamespace(
                        _pb=_HasField(result_field),
                        **{result_field: SimpleNamespace(resource_name=name)},
                    )
                )
            return SimpleNamespace(
                mutate_operation_responses=responses, partial_failure_error=None
            )
        if method.startswith("upload_"):
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        conversion_date_time=getattr(c, "conversion_date_time", ""),
                        gclid=getattr(c, "gclid", ""),
                    )
                    for c in payload
                ],
                partial_failure_error=None,
            )
        if method.endswith("_experiment"):
            return SimpleNamespace(name="operations/planned")
        return SimpleNamespace(
            results=[SimpleNamespace(resource_name=n) for n in names],
            partial_failure_error=None,
        )

    @staticmethod
    def _names_from(method: str, response: Any) -> List[str]:
        try:
            if method == "mutate":
                names = []
                for item in response.mutate_operation_responses:
                    which = item._pb.WhichOneof("response")
                    names.append(getattr(item, which).resource_name if which else "")
                return names
            if hasattr(response, "results"):
                return [
                    getattr(r, "resource_name", "") for r in response.results
                ]
        except Exception:  # noqa: BLE001 - recording must never break a write
            pass
        return []

    def _validate_request(self, method: str, request: Any, kwargs: Dict[str, Any]):
        """Builds the request object with validate_only set."""
        if request is None:
            import ads_mcp.utils as utils

            client = utils.get_googleads_client()
            type_name = _REQUEST_TYPES.get(method) or (
                "Mutate" + _camel(method[len("mutate_") :]) + "Request"
            )
            request = client.get_type(type_name)
            for key, value in kwargs.items():
                if key in ("retry", "timeout", "metadata"):
                    continue
                setattr(request, key, value)
        request.validate_only = True
        return request

    def _write(self, method: str, request: Any = None, **kwargs: Any) -> Any:
        session = self._session
        customer_id, payload, extra = self._unpack(method, request, kwargs)
        rendered = self._render(method, payload, extra)
        real = getattr(self._real, method)

        if session.mode == PLAN:
            names = self._placeholders(customer_id, rendered)
            session.record_write(
                WriteRecord(
                    self._name, method, customer_id, rendered, names,
                    validate_only=False, request_fields=extra,
                )
            )
            return self._synthetic(method, rendered, names, payload)

        if session.validate_only:
            validated = self._validate_request(method, request, kwargs)
            real(request=validated)
            names = self._placeholders(customer_id, rendered)
            session.record_write(
                WriteRecord(
                    self._name, method, customer_id, rendered, names,
                    validate_only=True, request_fields=extra,
                )
            )
            return self._synthetic(method, rendered, names, payload)

        response = (
            real(request=request, **kwargs) if request is not None
            else real(**kwargs)
        )
        names = self._names_from(method, response)
        session.record_write(
            WriteRecord(
                self._name, method, customer_id, rendered, names,
                validate_only=False, request_fields=extra,
            )
        )
        return response
