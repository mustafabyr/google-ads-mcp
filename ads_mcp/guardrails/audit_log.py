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

"""Every write leaves a line behind.

One JSON line per write attempt -- PROTECTIVE writes included, plan
applies, validations and failures too -- with the time in UTC and in
Europe/Istanbul, the account, the tool and its class, the plan id, what
was read before, what was sent, the outcome and who asked.

Where the lines go:

  * locally, ``~/.google-ads-mcp/audit/<YYYY-MM>.jsonl``
    (``GOOGLE_ADS_MCP_AUDIT_DIR`` overrides the directory);
  * always to the ``ads_mcp.audit`` logger as a single JSON line, which
    Cloud Logging parses as a structured entry on Cloud Run. It goes to
    stderr, never stdout -- stdout is the MCP channel in stdio mode;
  * optionally to a GCS bucket (``GOOGLE_ADS_MCP_AUDIT_GCS_BUCKET``), one
    object per line, when google-cloud-storage is installed. A failure
    there is logged and never fails the write it describes.

The same file answers two questions later: "what did this server change
in this account since Monday" (``read``), and "when did we last touch this
budget" (``last_budget_changes``), which the limits use to space budget
raises out.
"""

import datetime
import json
import logging
import os
import pathlib
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("ads_mcp.audit")

AUDIT_DIR_ENV = "GOOGLE_ADS_MCP_AUDIT_DIR"
DEFAULT_AUDIT_DIR = "~/.google-ads-mcp/audit"
GCS_BUCKET_ENV = "GOOGLE_ADS_MCP_AUDIT_GCS_BUCKET"
GCS_PREFIX = "audit"

# A tool's output can be a whole read-back; the log keeps the shape, not
# every row.
MAX_OUTPUT_CHARS = 8000

_lock = threading.Lock()

try:  # Istanbul has been fixed at UTC+3 since 2016; the fallback is exact.
    from zoneinfo import ZoneInfo

    ISTANBUL = ZoneInfo("Europe/Istanbul")
except Exception:  # noqa: BLE001 - no tz database on a slim image
    ISTANBUL = datetime.timezone(datetime.timedelta(hours=3), "Europe/Istanbul")


def audit_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(AUDIT_DIR_ENV) or DEFAULT_AUDIT_DIR
    ).expanduser()


def _parse_time(value: Any) -> Optional[datetime.datetime]:
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        when = value
    else:
        text = str(value).strip()
        try:
            when = datetime.datetime.fromisoformat(text)
        except ValueError:
            try:
                when = datetime.datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc)


def _cap_output(output: Any) -> Any:
    if output is None:
        return None
    text = json.dumps(output, default=str)
    if len(text) <= MAX_OUTPUT_CHARS:
        return json.loads(text)
    if isinstance(output, dict):
        kept: Dict[str, Any] = {"truncated": True, "keys": sorted(output)}
        verified = output.get("verified")
        if isinstance(verified, list):
            kept["verified"] = json.loads(json.dumps(verified[:5], default=str))
        return kept
    return {"truncated": True, "chars": len(text)}


def _entry_from_event(event: Dict[str, Any]) -> Dict[str, Any]:
    now = _parse_time(event.get("time_utc")) or datetime.datetime.now(
        datetime.timezone.utc
    )
    return {
        "time_utc": now.isoformat(timespec="seconds"),
        "time_istanbul": now.astimezone(ISTANBUL).isoformat(timespec="seconds"),
        "customer_id": str(event.get("customer_id") or ""),
        "tool": event.get("tool"),
        "tool_class": event.get("tool_class"),
        "plan_id": event.get("plan_id"),
        "result": event.get("result"),
        "validate_only": bool(event.get("validate_only")),
        "actor": event.get("actor"),
        "arguments": json.loads(json.dumps(event.get("arguments"), default=str)),
        "before": json.loads(json.dumps(event.get("before"), default=str)),
        "after": json.loads(json.dumps(event.get("after"), default=str)),
        "output": _cap_output(event.get("output")),
        "error": event.get("error"),
        "source": event.get("source", "server"),
        "note": event.get("note"),
    }


def _append_local(entry: Dict[str, Any]) -> pathlib.Path:
    directory = audit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    month = entry["time_utc"][:7]
    path = directory / f"{month}.jsonl"
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return path


def _write_gcs(entry: Dict[str, Any]) -> None:
    bucket_name = os.environ.get(GCS_BUCKET_ENV)
    if not bucket_name:
        return
    try:
        from google.cloud import storage  # optional dependency
    except ImportError:
        logger.warning(
            "%s is set but google-cloud-storage is not installed; the audit "
            "line stays local only.", GCS_BUCKET_ENV,
        )
        return
    try:
        client = storage.Client()
        stamp = entry["time_utc"].replace(":", "").replace("+", "Z")
        name = (
            f"{GCS_PREFIX}/{entry['time_utc'][:7]}/{stamp}-"
            f"{entry.get('customer_id') or 'na'}-"
            f"{(entry.get('tool') or 'tool').replace('.', '_')}.json"
        )
        client.bucket(bucket_name).blob(name).upload_from_string(
            json.dumps(entry, ensure_ascii=False, default=str),
            content_type="application/json",
        )
    except Exception:  # noqa: BLE001 - the write already happened; log, go on
        logger.exception("Could not write the audit line to gs://%s", bucket_name)


def record(event: Dict[str, Any]) -> Dict[str, Any]:
    """Writes one line everywhere it should go; returns the entry."""
    entry = _entry_from_event(event)
    try:
        _append_local(entry)
    except OSError:
        logger.exception("Could not append to the local audit log")
    # stderr via logging, never print(): stdout is the MCP transport.
    audit_logger.info(json.dumps(entry, ensure_ascii=False, default=str))
    _write_gcs(entry)
    return entry


def record_manual(
    customer_id: str,
    description: str,
    *,
    resource_type: Optional[str] = None,
    campaign_id: Optional[str] = None,
    when: Optional[str] = None,
    actor: str = "",
) -> Dict[str, Any]:
    """A decision made in the Google Ads interface, written down here."""
    entry = record(
        {
            "time_utc": (_parse_time(when) or datetime.datetime.now(
                datetime.timezone.utc
            )).isoformat(timespec="seconds"),
            "customer_id": "".join(ch for ch in str(customer_id) if ch.isdigit()),
            "tool": "manual",
            "tool_class": "MANUAL",
            "plan_id": None,
            "result": "recorded",
            "actor": actor,
            "arguments": {
                "resource_type": resource_type,
                "campaign_id": campaign_id,
            },
            "before": [],
            "after": [],
            "output": None,
            "error": None,
            "source": "manual",
            "note": description,
        }
    )
    return entry


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _months_between(since: datetime.datetime, until: datetime.datetime) -> List[str]:
    months = []
    cursor = datetime.date(since.year, since.month, 1)
    end = datetime.date(until.year, until.month, 1)
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = datetime.date(cursor.year + 1, 1, 1)
        else:
            cursor = datetime.date(cursor.year, cursor.month + 1, 1)
    return months


def read(
    customer_id: Optional[str] = None,
    since: Any = None,
    until: Any = None,
    tool: Optional[str] = None,
    limit: int = 500,
    now: Optional[datetime.datetime] = None,
) -> List[Dict[str, Any]]:
    """Entries newest first, from the local files."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = _parse_time(since) or (now - datetime.timedelta(days=30))
    end = _parse_time(until) or now + datetime.timedelta(days=1)
    wanted = "".join(ch for ch in str(customer_id or "") if ch.isdigit())
    directory = audit_dir()
    out: List[Dict[str, Any]] = []
    sequence = 0
    for month in _months_between(start, end):
        path = directory / f"{month}.jsonl"
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                when = _parse_time(entry.get("time_utc"))
                if when is None or when < start or when > end:
                    continue
                if wanted and entry.get("customer_id") != wanted:
                    continue
                if tool and entry.get("tool") != tool:
                    continue
                sequence += 1
                out.append((entry.get("time_utc", ""), sequence, entry))
    # Newest first; two lines in the same second keep their written order,
    # so the later one still comes first.
    out.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in out[: max(int(limit), 1)]]


def last_budget_changes(
    customer_id: str,
    days: int = 60,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, datetime.datetime]:
    """Budget resource name -> when this server last changed its amount."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=days)
    found: Dict[str, datetime.datetime] = {}
    for entry in read(customer_id, since=since, limit=10_000, now=now):
        if entry.get("result") != "applied":
            continue
        when = _parse_time(entry.get("time_utc"))
        if when is None:
            continue
        for write in entry.get("after") or []:
            for op in write.get("operations") or []:
                if op.get("resource") != "campaign_budget":
                    continue
                if "amount_micros" not in (op.get("fields") or {}):
                    continue
                name = op.get("resource_name") or ""
                if name and (name not in found or when > found[name]):
                    found[name] = when
    return found


def summarize(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The short form a report wants: no row dumps."""
    changes = []
    for write in entry.get("after") or []:
        for op in write.get("operations") or []:
            changes.append(
                {
                    "resource": op.get("resource"),
                    "kind": op.get("kind"),
                    "resource_name": op.get("resource_name"),
                    "fields": op.get("fields"),
                }
            )
    return {
        "time_utc": entry.get("time_utc"),
        "time_istanbul": entry.get("time_istanbul"),
        "customer_id": entry.get("customer_id"),
        "tool": entry.get("tool"),
        "tool_class": entry.get("tool_class"),
        "plan_id": entry.get("plan_id"),
        "result": entry.get("result"),
        "validate_only": entry.get("validate_only"),
        "actor": entry.get("actor"),
        "source": entry.get("source"),
        "note": entry.get("note"),
        "changes": changes,
        "error": entry.get("error"),
    }


_installed = False


def install() -> None:
    """Registers the log as a write listener and as the budget history."""
    global _installed
    from ads_mcp.guardrails import guard

    if _installed or record in guard.WRITE_LISTENERS:
        return
    guard.WRITE_LISTENERS.append(record)
    guard.BUDGET_HISTORY = last_budget_changes
    _installed = True
