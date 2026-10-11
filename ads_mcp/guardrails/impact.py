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

"""What a plan costs, in the account's own currency, and how to undo it.

The estimate is honest about what it knows. A budget change is a real
number: old daily amount to new daily amount. Enabling a campaign starts
spending its budget. A bid change is reported as old to new per click and
carries no daily figure, because clicks are not known in advance. Anything
else is listed with no number rather than a made-up one.

Monthly figures use 30.4 days, the same convention every tool here uses
in its confirmation messages.
"""

from typing import Any, Dict, List, Optional

from ads_mcp.guardrails.changes import row_for
from ads_mcp.guardrails.limits import Change

_MICROS = 1_000_000
DAYS_PER_MONTH = 30.4

_BID_FIELDS = (
    "cpc_bid_micros",
    "target_spend.cpc_bid_ceiling_micros",
    "target_impression_share.cpc_bid_ceiling_micros",
    "maximize_conversions.target_cpa_micros",
    # Portfolio (shared) bidding_strategy schemes.
    "maximize_conversions.cpc_bid_ceiling_micros",
    "maximize_conversion_value.cpc_bid_ceiling_micros",
    "maximize_conversion_value.target_roas",
    "target_cpa.target_cpa_micros",
    "target_cpa.cpc_bid_ceiling_micros",
    "target_roas.target_roas",
    "target_roas.cpc_bid_ceiling_micros",
)


def _money(micros: Any) -> Optional[float]:
    try:
        return round(int(micros) / _MICROS, 2)
    except (TypeError, ValueError):
        return None


def _campaign_budget(change: Change, rows: List[Dict[str, Any]]) -> Optional[float]:
    amount = _money(change.context.get("campaign_budget.amount_micros"))
    if amount is not None:
        return amount
    budget_name = change.fields.get("campaign_budget") or change.before.get(
        "campaign_budget"
    )
    if budget_name:
        row = row_for(rows, "campaign_budget", str(budget_name))
        if row:
            return _money((row.get("campaign_budget") or {}).get("amount_micros"))
    return None


def estimate(changes: List[Change], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    daily_delta = 0.0
    known = False
    details: List[Dict[str, Any]] = []

    for change in changes:
        fields, before = change.fields, change.before
        if change.resource == "campaign_budget" and "amount_micros" in fields:
            new = _money(fields["amount_micros"])
            old = _money(before.get("amount_micros")) if change.kind == "update" else None
            if new is None:
                continue
            if change.kind == "create":
                details.append(
                    {
                        "what": "new budget",
                        "resource_name": change.resource_name,
                        "daily": new,
                        "note": "Spends nothing until an ENABLED campaign uses it.",
                    }
                )
                continue
            if old is not None:
                daily_delta += new - old
                known = True
            details.append(
                {
                    "what": "daily budget",
                    "resource_name": change.resource_name,
                    "from": old,
                    "to": new,
                    "daily_delta": round(new - old, 2) if old is not None else None,
                }
            )
        if change.resource == "campaign" and "status" in fields:
            budget = _campaign_budget(change, rows)
            was = before.get("status")
            now = fields["status"]
            delta = None
            if budget is not None:
                if now == "ENABLED" and was != "ENABLED":
                    delta = budget
                elif now != "ENABLED" and was == "ENABLED":
                    delta = -budget
                else:
                    delta = 0.0
                daily_delta += delta
                known = True
            details.append(
                {
                    "what": "campaign status",
                    "resource_name": change.resource_name,
                    "from": was,
                    "to": now,
                    "campaign_daily_budget": budget,
                    "daily_delta": delta,
                }
            )
        if change.resource == "campaign" and "campaign_budget" in fields:
            new = _money(
                ((row_for(rows, "campaign_budget", str(fields["campaign_budget"])) or {})
                 .get("campaign_budget") or {}).get("amount_micros")
            )
            old = _money(change.context.get("campaign_budget.amount_micros"))
            if new is not None and old is not None:
                daily_delta += new - old
                known = True
            details.append(
                {
                    "what": "campaign moved to another budget",
                    "resource_name": change.resource_name,
                    "from": old,
                    "to": new,
                    "daily_delta": round(new - old, 2)
                    if new is not None and old is not None else None,
                }
            )
        if change.resource == "campaign" and change.kind == "create":
            details.append(
                {
                    "what": "new campaign",
                    "status": fields.get("status"),
                    "note": "Created PAUSED: spends nothing until enabled.",
                }
            )
        for field in _BID_FIELDS:
            if field in fields:
                details.append(
                    {
                        "what": field,
                        "resource_name": change.resource_name,
                        "from": _money(before.get(field)),
                        "to": _money(fields[field]),
                        "note": "Per click (or per conversion for a CPA "
                                "target); no daily figure is possible.",
                    }
                )
        if "bid_modifier" in fields:
            details.append(
                {
                    "what": "bid modifier",
                    "resource_name": change.resource_name,
                    "from": before.get("bid_modifier", 1.0 if change.kind == "create" else None),
                    "to": fields["bid_modifier"],
                }
            )

    return {
        "currency": "the account's own currency, not converted",
        "daily_delta": round(daily_delta, 2) if known else None,
        "monthly_delta": round(daily_delta * DAYS_PER_MONTH, 2) if known else None,
        "basis": f"monthly = daily x {DAYS_PER_MONTH}" if known else
                 "no daily figure can be derived from these changes",
        "details": details,
    }


def rollback(changes: List[Change], tool: str) -> List[str]:
    """One line per change saying how to undo it."""
    steps: List[str] = []
    for change in changes:
        name = change.resource_name or f"(new {change.resource})"
        if change.kind == "create":
            steps.append(
                f"Remove or pause the created {change.resource} {name}; its "
                f"real id is in the apply result under 'actual'."
            )
        elif change.kind == "update":
            restore = {
                k: change.before.get(k) for k in change.fields
                if k in change.before
            }
            if restore:
                steps.append(
                    f"Set {name} back to {restore} (the values read before "
                    f"this plan) with {tool} or the matching tool."
                )
            else:
                steps.append(
                    f"Update {name} again with the previous values; they "
                    f"were not read before this plan, so read them first."
                )
        elif change.kind == "remove":
            steps.append(
                f"Not reversible: {name} is removed. Re-create it from the "
                f"'before' snapshot: {change.before or 'not read'}."
            )
        elif change.kind == "upload":
            steps.append(
                "Uploaded conversions cannot be withdrawn; a later upload "
                "with the same order_id or click id would be deduplicated."
            )
        else:
            steps.append(f"{change.kind} on {name}: see the tool's own notes.")
    return steps
