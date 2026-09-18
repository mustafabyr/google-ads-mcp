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

"""Per-account ceilings: no ceiling on file, no write.

The limits file is YAML, kept OUTSIDE the repository (the real numbers are
per client and private). Its path comes from ``GOOGLE_ADS_MCP_LIMITS_PATH``
and defaults to ``~/.google-ads-mcp/limits.yaml``; a copy of the shape is
in ``limits.example.yaml`` next to this module.

    defaults:
      max_budget_change_pct: 20          # one change may raise a budget by
      min_days_between_budget_changes: 7 # this much, this often
    accounts:
      "1234567890":
        max_daily_budget_try: 4500       # in the account's own currency
        max_cpc_ceiling_try: 20
        allow_enable: true

The rules, in the order they are applied:

  * READ and PROTECTIVE tools never consult this file.
  * A SPEND or DESTRUCTIVE call on an account with no row is refused
    outright: "no limits defined for this account". This is the contract
    that makes the file matter -- an account nobody has thought about
    cannot be written to.
  * A change that exceeds a ceiling is refused with the requested value,
    the allowed value and the key that decided it, so the refusal can be
    read and the file can be fixed.

Each account row may carry:

  max_daily_budget_try           highest daily budget any single budget may
                                 be set to (create or raise)
  max_cpc_ceiling_try            highest max-CPC bid or bid ceiling
  allow_enable                   whether campaigns may be enabled at all
  max_budget_change_pct          overrides the default for this account
  min_days_between_budget_changes  overrides the default

The ``_try`` suffix documents that the number is in the account's own
currency (this fork manages TRY accounts); the same key without the
suffix is accepted too. Nothing here converts currencies.
"""

import dataclasses
import datetime
import os
import pathlib
from typing import Any, Dict, List, Optional

import yaml

LIMITS_PATH_ENV = "GOOGLE_ADS_MCP_LIMITS_PATH"
DEFAULT_LIMITS_PATH = "~/.google-ads-mcp/limits.yaml"
# On Cloud Run the file is a mounted secret (LIMITS_PATH points at it) or
# an object in a bucket: "gs://bucket/limits.yaml". The object is
# re-read at most every LIMITS_GCS_TTL_SECONDS.
LIMITS_GCS_URI_ENV = "GOOGLE_ADS_MCP_LIMITS_GCS_URI"
LIMITS_GCS_TTL_SECONDS = 300

_MICROS = 1_000_000

# Keys that may appear on an account row, and the aliases we accept.
_ALIASES = {
    "max_daily_budget": "max_daily_budget_try",
    "max_cpc_ceiling": "max_cpc_ceiling_try",
}
_KNOWN_KEYS = {
    "max_daily_budget_try",
    "max_cpc_ceiling_try",
    "allow_enable",
    "max_budget_change_pct",
    "min_days_between_budget_changes",
    "name",
}
_DEFAULT_KEYS = {"max_budget_change_pct", "min_days_between_budget_changes"}

_BUILTIN_DEFAULTS = {
    "max_budget_change_pct": 20,
    "min_days_between_budget_changes": 7,
}

# Fields on a change that carry a cost-per-click ceiling, in micros.
_CPC_FIELDS = (
    "cpc_bid_micros",
    "target_spend.cpc_bid_ceiling_micros",
    "target_impression_share.cpc_bid_ceiling_micros",
)


class LimitsError(PermissionError):
    """Base class: the limits layer refused a write."""


class LimitsNotConfigured(LimitsError):
    """The account has no row in the limits file (or there is no file)."""


class LimitExceeded(LimitsError):
    """A change asks for more than the account's row allows."""

    def __init__(self, customer_id: str, key: str, requested: Any,
                 allowed: Any, what: str):
        self.customer_id = customer_id
        self.key = key
        self.requested = requested
        self.allowed = allowed
        self.what = what
        super().__init__(
            f"Refused by the limits file for account {customer_id}: "
            f"{what}. Requested {requested}, allowed {allowed} (key: {key}). "
            f"Raise the ceiling in the limits file if this is intended."
        )


@dataclasses.dataclass
class Change:
    """One captured write, reduced to what the limits care about.

    ``fields`` holds the leaf fields the operation sets, keyed by dotted
    path ("amount_micros", "target_spend.cpc_bid_ceiling_micros").
    ``before`` holds the same keys as read before the write, when known.
    """

    resource: str
    kind: str
    resource_name: str
    fields: Dict[str, Any] = dataclasses.field(default_factory=dict)
    before: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # Other sections of the same row, e.g. a campaign change carries its
    # budget's amount under "campaign_budget.amount_micros".
    context: Dict[str, Any] = dataclasses.field(default_factory=dict)


def clean_id(value: Any) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def limits_path() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(LIMITS_PATH_ENV) or DEFAULT_LIMITS_PATH
    ).expanduser()


class Limits:
    """The parsed limits file."""

    def __init__(self, data: Optional[Dict[str, Any]], source: str = ""):
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError(f"Limits file {source} must be a YAML mapping.")
        self.source = source
        self.defaults: Dict[str, Any] = dict(_BUILTIN_DEFAULTS)
        for key, value in (data.get("defaults") or {}).items():
            if key not in _DEFAULT_KEYS:
                raise ValueError(
                    f"Limits file {source}: unknown default {key!r}. "
                    f"Known: {sorted(_DEFAULT_KEYS)}."
                )
            self.defaults[key] = value
        self.accounts: Dict[str, Dict[str, Any]] = {}
        for raw_id, row in (data.get("accounts") or {}).items():
            cid = clean_id(raw_id)
            if not cid:
                raise ValueError(
                    f"Limits file {source}: account key {raw_id!r} has no "
                    f"digits."
                )
            row = dict(row or {})
            for alias, canonical in _ALIASES.items():
                if alias in row:
                    row[canonical] = row.pop(alias)
            unknown = set(row) - _KNOWN_KEYS
            if unknown:
                raise ValueError(
                    f"Limits file {source}: account {raw_id} has unknown "
                    f"key(s) {sorted(unknown)}. Known: {sorted(_KNOWN_KEYS)}."
                )
            self.accounts[cid] = row

    @classmethod
    def load(cls, path: Optional[pathlib.Path] = None) -> "Limits":
        path = path or limits_path()
        if not path.exists():
            return cls({}, source=f"{path} (missing)")
        with open(path, "r", encoding="utf-8") as handle:
            return cls(yaml.safe_load(handle) or {}, source=str(path))

    def account(self, customer_id: str) -> Optional[Dict[str, Any]]:
        return self.accounts.get(clean_id(customer_id))

    def setting(self, customer_id: str, key: str) -> Any:
        row = self.account(customer_id) or {}
        if key in row:
            return row[key]
        return self.defaults.get(key)


# --------------------------------------------------------------------------
# Loading, with a cheap mtime cache so edits are picked up without a restart
# --------------------------------------------------------------------------

_cache: Dict[str, Any] = {"path": None, "mtime": None, "limits": None}


def _load_gcs(uri: str) -> Limits:
    from google.cloud import storage  # optional dependency

    bucket_name, _, blob_name = uri[len("gs://") :].partition("/")
    text = storage.Client().bucket(bucket_name).blob(blob_name).download_as_text()
    return Limits(yaml.safe_load(text) or {}, source=uri)


def current() -> Limits:
    uri = os.environ.get(LIMITS_GCS_URI_ENV)
    if uri:
        import time

        stamp = int(time.time() // LIMITS_GCS_TTL_SECONDS)
        if (
            _cache["limits"] is None
            or _cache["path"] != uri
            or _cache["mtime"] != stamp
        ):
            _cache.update(
                {"path": uri, "mtime": stamp, "limits": _load_gcs(uri)}
            )
        return _cache["limits"]

    path = limits_path()
    mtime = path.stat().st_mtime if path.exists() else None
    if (
        _cache["limits"] is None
        or _cache["path"] != str(path)
        or _cache["mtime"] != mtime
    ):
        _cache.update(
            {"path": str(path), "mtime": mtime, "limits": Limits.load(path)}
        )
    return _cache["limits"]


def reset_cache() -> None:
    _cache.update({"path": None, "mtime": None, "limits": None})


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------


def require_account(customer_id: str, tool: str = "") -> Dict[str, Any]:
    """Refuses a SPEND or DESTRUCTIVE call on an account with no row."""
    limits = current()
    row = limits.account(customer_id)
    if row is None:
        where = limits.source
        raise LimitsNotConfigured(
            f"No limits are defined for account {clean_id(customer_id)}"
            f"{' (' + tool + ')' if tool else ''}, so nothing that can spend "
            f"or remove is written to it. Add the account to the limits "
            f"file at {where} with its max_daily_budget_try, "
            f"max_cpc_ceiling_try and allow_enable, then call again. READ "
            f"and PROTECTIVE tools keep working without a row."
        )
    return row


def _money(micros: Any) -> float:
    return round(int(micros or 0) / _MICROS, 2)


def check_changes(
    customer_id: str,
    changes: List[Change],
    last_budget_change: Optional[Dict[str, datetime.datetime]] = None,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Applies the account's ceilings to a list of captured changes.

    Raises LimitExceeded on the first violation; returns a summary of what
    was checked otherwise. ``last_budget_change`` maps a budget resource
    name to when this server last applied a change to it, from the audit
    log.
    """
    cid = clean_id(customer_id)
    limits = current()
    row = require_account(cid)
    checked: List[str] = []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    last_budget_change = last_budget_change or {}

    max_budget = row.get("max_daily_budget_try")
    max_cpc = row.get("max_cpc_ceiling_try")
    allow_enable = row.get("allow_enable", False)
    max_pct = limits.setting(cid, "max_budget_change_pct")
    min_days = limits.setting(cid, "min_days_between_budget_changes")

    for change in changes:
        fields = change.fields
        before = change.before

        # --- enabling ------------------------------------------------------
        if change.resource == "campaign" and fields.get("status") == "ENABLED":
            checked.append("allow_enable")
            if not allow_enable:
                raise LimitExceeded(
                    cid, "allow_enable", "ENABLED", "not allowed",
                    f"enabling {change.resource_name}",
                )

        # --- budgets -------------------------------------------------------
        if change.resource == "campaign_budget" and "amount_micros" in fields:
            requested = _money(fields["amount_micros"])
            previous = before.get("amount_micros")
            if max_budget is not None:
                checked.append("max_daily_budget_try")
                if requested > float(max_budget):
                    raise LimitExceeded(
                        cid, "max_daily_budget_try", requested,
                        float(max_budget),
                        f"daily budget on {change.resource_name}",
                    )
            if change.kind == "update" and previous is not None:
                old = _money(previous)
                if old > 0 and requested > old and max_pct is not None:
                    checked.append("max_budget_change_pct")
                    pct = round((requested - old) / old * 100, 1)
                    if pct > float(max_pct):
                        raise LimitExceeded(
                            cid, "max_budget_change_pct", f"+{pct}%",
                            f"+{max_pct}% per change",
                            f"raising {change.resource_name} from {old} "
                            f"to {requested} in one step",
                        )
                if requested > old and min_days is not None:
                    when = last_budget_change.get(change.resource_name)
                    if when is not None:
                        checked.append("min_days_between_budget_changes")
                        if when.tzinfo is None:
                            when = when.replace(
                                tzinfo=datetime.timezone.utc
                            )
                        days = (now - when).total_seconds() / 86400
                        if days < float(min_days):
                            raise LimitExceeded(
                                cid, "min_days_between_budget_changes",
                                f"{days:.1f} days since the last change",
                                f"at least {min_days} days",
                                f"raising {change.resource_name} again",
                            )

        # --- bids and ceilings ---------------------------------------------
        if max_cpc is not None:
            for field in _CPC_FIELDS:
                if field in fields and fields[field]:
                    checked.append("max_cpc_ceiling_try")
                    requested = _money(fields[field])
                    if requested > float(max_cpc):
                        raise LimitExceeded(
                            cid, "max_cpc_ceiling_try", requested,
                            float(max_cpc),
                            f"{field} on {change.resource_name}",
                        )

    return {
        "account": cid,
        "source": limits.source,
        "checked": sorted(set(checked)),
        "ok": True,
    }


def describe(customer_id: str) -> Dict[str, Any]:
    """The account's effective settings, for plan output and diagnostics."""
    limits = current()
    row = limits.account(customer_id)
    if row is None:
        return {"configured": False, "source": limits.source}
    return {
        "configured": True,
        "source": limits.source,
        "max_daily_budget_try": row.get("max_daily_budget_try"),
        "max_cpc_ceiling_try": row.get("max_cpc_ceiling_try"),
        "allow_enable": bool(row.get("allow_enable", False)),
        "max_budget_change_pct": limits.setting(
            customer_id, "max_budget_change_pct"
        ),
        "min_days_between_budget_changes": limits.setting(
            customer_id, "min_days_between_budget_changes"
        ),
    }
