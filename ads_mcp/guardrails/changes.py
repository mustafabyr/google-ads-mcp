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

"""From captured writes and reads to a flat list of before/after changes.

A tool's reads before its first write are the "before". Its captured
operations are the "after". This module joins the two on the object's id,
so the limits, the direction rules and the impact estimate all work on the
same simple shape: ``Change(resource, kind, resource_name, fields, before,
context)`` where every dict is flat and dotted
(``"target_spend.cpc_bid_ceiling_micros"``).
"""

from typing import Any, Dict, List, Optional

from ads_mcp.guardrails.limits import Change
from ads_mcp.guardrails.session import WriteSession

_ID_KEYS = ("id", "criterion_id", "experiment_id")


def _number(value: Any) -> Any:
    """proto-plus renders int64 as strings; turn those back into ints."""
    if isinstance(value, str) and value.lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            return value
    return value


def flatten(data: Any, prefix: str = "") -> Dict[str, Any]:
    """Nested dict -> {"a.b": value}. Lists are kept as values."""
    out: Dict[str, Any] = {}
    if not isinstance(data, dict):
        return {prefix: _number(data)} if prefix else {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, path))
        elif isinstance(value, list):
            out[path] = [_number(v) if not isinstance(v, dict) else flatten(v) for v in value]
        else:
            out[path] = _number(value)
    return out


def object_id(resource_name: str) -> str:
    """The id that identifies the object: the last segment, after any '~'."""
    tail = str(resource_name or "").rsplit("/", 1)[-1]
    return tail.rsplit("~", 1)[-1]


def row_for(
    rows: List[Dict[str, Any]], resource: str, resource_name: str
) -> Optional[Dict[str, Any]]:
    """The recorded row whose ``resource`` section is this object."""
    wanted = object_id(resource_name)
    if not wanted:
        return None
    for row in rows:
        section = row.get(resource)
        if not isinstance(section, dict):
            continue
        candidates = [section.get(k) for k in _ID_KEYS]
        ad = section.get("ad")
        if isinstance(ad, dict):
            candidates.append(ad.get("id"))
        if section.get("resource_name") == resource_name:
            return row
        if any(str(c) == wanted for c in candidates if c is not None):
            return row
    return None


def masked(fields: Dict[str, Any], mask: List[str]) -> Dict[str, Any]:
    """Keeps the fields an update mask names; names a cleared leaf as None."""
    if not mask:
        return fields
    kept: Dict[str, Any] = {}
    for path in mask:
        hit = False
        for key, value in fields.items():
            if key == path or key.startswith(path + "."):
                kept[key] = value
                hit = True
        if not hit:
            kept[path] = None
    return kept


def from_operations(
    operations: List[Dict[str, Any]], rows: List[Dict[str, Any]]
) -> List[Change]:
    out: List[Change] = []
    for op in operations:
        resource = op.get("resource") or ""
        kind = op.get("kind", "")
        resource_name = op.get("resource_name", "")
        fields = flatten(op.get("fields") or {})
        if kind == "update":
            fields = masked(fields, op.get("mask") or [])
        before: Dict[str, Any] = {}
        context: Dict[str, Any] = {}
        row = row_for(rows, resource, resource_name)
        if row is not None:
            for section, value in row.items():
                if section == resource:
                    before = flatten(value)
                elif section not in ("metrics", "segments"):
                    context.update(flatten(value, section))
        out.append(
            Change(
                resource=resource,
                kind=kind,
                resource_name=resource_name,
                fields=fields,
                before=before,
                context=context,
            )
        )
    return out


def from_session(session: WriteSession) -> List[Change]:
    rows = session.rows
    operations: List[Dict[str, Any]] = []
    for write in session.writes:
        operations.extend(write.operations)
    return from_operations(operations, rows)


def to_json(change: Change) -> Dict[str, Any]:
    return {
        "resource": change.resource,
        "kind": change.kind,
        "resource_name": change.resource_name,
        "before": change.before,
        "after": change.fields,
    }
