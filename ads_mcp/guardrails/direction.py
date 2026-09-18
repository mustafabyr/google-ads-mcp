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

"""Which way a change cuts: the effective class of one call.

A tool's static class is what the client sees. The effective class is
what this call actually does, decided after the tool has run in plan mode
and its writes have been captured:

  * a static DESTRUCTIVE tool is DESTRUCTIVE;
  * a tool in REMOVE_ESCALATES that captured a remove is DESTRUCTIVE (the
    legacy status tools with REMOVED, a keyword whose match type changes);
  * a DIRECTIONAL tool whose every change is a decrease is PROTECTIVE and
    is written straight away, no plan and no ceiling;
  * everything else keeps its static class.

"Decrease" is decided per change from the before value: a lower budget,
a lower bid, a lower or unchanged modifier, PAUSED, an exclusion, PRESENCE
instead of interest. Anything the rules cannot place is an increase --
the conservative answer is always the plan.
"""

from typing import Any, Dict, List, Optional

from ads_mcp.guardrails import classes
from ads_mcp.guardrails.changes import row_for
from ads_mcp.guardrails.limits import Change

DECREASE = "decrease"
INCREASE = "increase"

_BID_FIELDS = (
    "cpc_bid_micros",
    "target_spend.cpc_bid_ceiling_micros",
    "target_impression_share.cpc_bid_ceiling_micros",
)


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _budget_amount(rows: List[Dict[str, Any]], resource_name: str) -> Optional[float]:
    row = row_for(rows, "campaign_budget", resource_name)
    if not row:
        return None
    return _num((row.get("campaign_budget") or {}).get("amount_micros"))


def classify(change: Change, rows: List[Dict[str, Any]]) -> str:
    """DECREASE when the change can only lower or protect spend."""
    fields, before = change.fields, change.before
    if change.kind == "remove":
        return INCREASE
    verdicts: List[str] = []

    if change.resource == "campaign_budget" and "amount_micros" in fields:
        new, old = _num(fields["amount_micros"]), _num(before.get("amount_micros"))
        verdicts.append(
            DECREASE if new is not None and old is not None and new <= old
            else INCREASE
        )

    if change.resource in ("campaign", "ad_group", "ad_group_ad", "ad_group_criterion"):
        if "status" in fields:
            verdicts.append(DECREASE if fields["status"] == "PAUSED" else INCREASE)

    for field in _BID_FIELDS:
        if field in fields:
            new, old = _num(fields[field]), _num(before.get(field))
            verdicts.append(
                DECREASE if new is not None and old is not None and old > 0
                and new <= old else INCREASE
            )

    if change.resource == "campaign_criterion":
        negative = fields.get("negative", before.get("negative", False))
        if "bid_modifier" in fields:
            new = _num(fields["bid_modifier"])
            old = _num(before.get("bid_modifier"))
            if old is None:
                old = 1.0
            verdicts.append(
                DECREASE if new is not None and new <= old else INCREASE
            )
        if change.kind == "create" and "bid_modifier" not in fields:
            verdicts.append(DECREASE if negative else INCREASE)

    if "geo_target_type_setting.positive_geo_target_type" in fields:
        verdicts.append(
            DECREASE
            if fields["geo_target_type_setting.positive_geo_target_type"]
            == "PRESENCE"
            else INCREASE
        )

    if change.resource == "campaign" and "campaign_budget" in fields:
        new = _budget_amount(rows, str(fields["campaign_budget"]))
        old = _num(change.context.get("campaign_budget.amount_micros"))
        verdicts.append(
            DECREASE if new is not None and old is not None and new <= old
            else INCREASE
        )

    # Fields the rules above did not place: name, dates, networks, URLs,
    # keywords, ads ... none of them is a known decrease.
    placed = {
        "amount_micros", "status", "negative", "bid_modifier",
        "geo_target_type_setting.positive_geo_target_type", "campaign_budget",
        *_BID_FIELDS,
    }
    # Sub-fields that merely identify the criterion (device type, gender,
    # age range, location) do not decide direction on their own.
    identity_prefixes = ("device.", "gender.", "age_range.", "location.",
                         "language.", "campaign", "ad_group")
    for key in fields:
        if key in placed or key.startswith(identity_prefixes):
            continue
        verdicts.append(INCREASE)

    if not verdicts:
        return INCREASE
    return DECREASE if all(v == DECREASE for v in verdicts) else INCREASE


def _languages_narrow(changes: List[Change], rows: List[Dict[str, Any]]) -> bool:
    """set_campaign_languages: narrowing needs the before set of languages."""
    before = {
        str(((r.get("campaign_criterion") or {}).get("language") or {})
            .get("language_constant") or "")
        for r in rows
        if (r.get("campaign_criterion") or {}).get("language")
    }
    before.discard("")
    after = {
        str(c.fields.get("language.language_constant"))
        for c in changes
        if c.kind == "create" and "language.language_constant" in c.fields
    }
    if not after:
        return False
    # No language criterion means ALL languages, so any set is a narrowing.
    return not before or after <= before


def effective_class(
    key: str,
    static_cls: str,
    changes: List[Change],
    rows: Optional[List[Dict[str, Any]]] = None,
) -> str:
    rows = rows or []
    if static_cls == classes.DESTRUCTIVE:
        return classes.DESTRUCTIVE
    if key in classes.REMOVE_ESCALATES and any(c.kind == "remove" for c in changes):
        return classes.DESTRUCTIVE
    if key not in classes.DIRECTIONAL or not changes:
        return static_cls
    if key == "campaigns.set_campaign_languages":
        return classes.PROTECTIVE if _languages_narrow(changes, rows) else static_cls
    if all(classify(c, rows) == DECREASE for c in changes):
        return classes.PROTECTIVE
    return static_cls
