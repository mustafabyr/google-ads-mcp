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

"""Account-portfolio tools, for whoever is responsible for several accounts.

Everything else in this server answers a question about ONE account. These
answer "which of my accounts needs me today" and "are we about to overspend
the month", which is the work that actually starts an agency's day.

Both are read-only.
"""

from typing import Any, Dict, List, Optional
import calendar
import datetime

import proto
from fastmcp import FastMCP
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

agency_mcp = FastMCP("agency")

_MICROS = 1_000_000


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _from_micros(micros: Any) -> float:
    return round(int(micros or 0) / _MICROS, 2)


def _rows(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    out = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            out.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return out


@agency_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def account_portfolio_overview(
    manager_customer_id: Optional[str] = None,
    date_range: str = "LAST_7_DAYS",
    customer_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """One line per account: spend, conversions and whether measurement works.

    Built for the start of the day. The health check it runs on each account
    is the one that matters most and is easiest to miss: an account that
    RECORDS conversions but counts none of them towards the Conversions
    metric is bidding towards nothing, and every other report in that account
    reads as zero.

    Accounts are read one at a time and an account that refuses access is
    reported rather than failing the whole call -- a deactivated or
    unlinked account in the list is normal, not an error.

    Args:
        manager_customer_id: A manager account whose clients to list. Omit to
            use every account the credentials can reach.
        date_range: A GAQL literal such as LAST_7_DAYS or THIS_MONTH.
        customer_ids: An explicit list of accounts, instead of discovering
            them.

    Each account row also carries `call_extension_clicks` (clicks on the
    call button in the date range), `phone_calls` (calls Google timed
    through a forwarding number) and `spending_last_7_days`, whatever the
    date range, because a client whose account has gone quiet is the first
    thing to notice.

    Returns:
        `accounts` sorted by spend, `needs_attention`, and `unreachable`.
    """
    if customer_ids:
        targets = [_clean_id(c) for c in customer_ids]
    elif manager_customer_id:
        manager = _clean_id(manager_customer_id)
        targets = [
            str(row["customer_client"]["id"])
            for row in _rows(
                manager,
                """
                SELECT customer_client.id, customer_client.manager,
                       customer_client.status
                FROM customer_client
                WHERE customer_client.status = 'ENABLED'
                  AND customer_client.manager = false
                """,
            )
        ]
    else:
        service = utils.get_googleads_service("CustomerService")
        targets = [
            name.rsplit("/", 1)[-1]
            for name in service.list_accessible_customers().resource_names
        ]

    accounts: List[Dict[str, Any]] = []
    unreachable: List[Dict[str, str]] = []

    for customer_id in targets:
        try:
            info = _rows(
                customer_id,
                """
                SELECT customer.id, customer.descriptive_name,
                       customer.currency_code, customer.manager,
                       customer.test_account, customer.auto_tagging_enabled,
                       customer.optimization_score
                FROM customer
                """,
            )
            if not info:
                continue
            c = info[0]["customer"]
            if c.get("manager"):
                continue

            totals = _rows(
                customer_id,
                f"""
                SELECT metrics.cost_micros, metrics.clicks,
                       metrics.impressions, metrics.conversions,
                       metrics.all_conversions
                FROM customer
                WHERE segments.date DURING {date_range}
                """,
            )
            m = totals[0].get("metrics", {}) if totals else {}
            cost = _from_micros(m.get("cost_micros"))
            conversions = round(float(m.get("conversions") or 0), 2)
            all_conversions = round(float(m.get("all_conversions") or 0), 2)

            # Clicks on the call button, i.e. the call extension. The
            # segment has to be SELECTed to be filtered on; the customer
            # resource does not carry metrics.phone_calls, so that one is
            # summed from campaigns.
            call_rows = _rows(
                customer_id,
                f"""
                SELECT metrics.clicks, segments.click_type
                FROM customer
                WHERE segments.click_type = 'CALLS'
                  AND segments.date DURING {date_range}
                """,
            )
            call_clicks = sum(
                int(r.get("metrics", {}).get("clicks") or 0) for r in call_rows
            )
            phone_calls = sum(
                int(r.get("metrics", {}).get("phone_calls") or 0)
                for r in _rows(
                    customer_id,
                    f"""
                    SELECT campaign.id, metrics.phone_calls
                    FROM campaign
                    WHERE segments.date DURING {date_range}
                    """,
                )
            )

            # "Is it spending at all" is the first thing to know about an
            # account, whatever date range the caller asked for.
            week = _rows(
                customer_id,
                """
                SELECT metrics.cost_micros
                FROM customer
                WHERE segments.date DURING LAST_7_DAYS
                """,
            )
            cost_7d = _from_micros(
                week[0].get("metrics", {}).get("cost_micros") if week else 0
            )

            flags = []
            if not cost_7d:
                flags.append("no spend in the last 7 days")
            if all_conversions and not conversions:
                flags.append(
                    "records conversions but none count towards bidding"
                )
            if cost and not all_conversions:
                flags.append("spending with nothing recorded at all")
            if not c.get("auto_tagging_enabled"):
                flags.append("auto-tagging off, so Analytics cannot attribute")

            accounts.append(
                {
                    "customer_id": c["id"],
                    "name": c.get("descriptive_name"),
                    "currency": c.get("currency_code"),
                    "test_account": bool(c.get("test_account")),
                    "cost": cost,
                    "clicks": int(m.get("clicks") or 0),
                    "call_extension_clicks": call_clicks,
                    "phone_calls": phone_calls,
                    "cost_last_7_days": cost_7d,
                    "spending_last_7_days": bool(cost_7d),
                    "conversions_counted": conversions,
                    "all_conversions": all_conversions,
                    "cost_per_conversion": round(cost / conversions, 2)
                    if conversions else None,
                    "optimization_score": round(
                        float(c["optimization_score"]) * 100, 1
                    ) if c.get("optimization_score") is not None else None,
                    "flags": flags,
                }
            )
        except GoogleAdsException as exc:
            unreachable.append(
                {
                    "customer_id": customer_id,
                    "reason": "; ".join(
                        error.message for error in exc.failure.errors
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one bad account must not
            unreachable.append(  # stop the portfolio view
                {"customer_id": customer_id, "reason": str(exc)}
            )

    accounts.sort(key=lambda a: a["cost"], reverse=True)
    return {
        "date_range": date_range,
        "accounts": accounts,
        "needs_attention": [
            {"customer_id": a["customer_id"], "name": a["name"],
             "cost": a["cost"], "flags": a["flags"]}
            for a in accounts if a["flags"]
        ],
        "unreachable": unreachable,
        "totals": {
            "accounts": len(accounts),
            "cost": round(sum(a["cost"] for a in accounts), 2),
        },
    }


@agency_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_budget_pacing(
    customer_id: str,
    monthly_plan: Optional[float] = None,
    campaign_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Compares month-to-date spend against where it should be by now.

    A daily budget is not a monthly cap: Google may spend up to twice the
    daily amount on a good day, balancing over the month, so an account can
    sit above plan mid-month and still land on target -- or not. This puts
    the actual figure next to the straight-line pace so the difference is
    visible before the month ends rather than after.

    Args:
        customer_id: The Google Ads account id.
        monthly_plan: What the month is supposed to cost, in the account's
            own currency. Omit to use the sum of the daily budgets times the
            days in the month, which is Google's own implied ceiling.
        campaign_id: Limit to one campaign.

    Returns:
        Spend so far, the straight-line pace, the projection, and the
        per-campaign breakdown.
    """
    customer_id = _clean_id(customer_id)
    today = datetime.date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed = today.day

    where = ["campaign.status = 'ENABLED'"]
    if campaign_id:
        where.append(f"campaign.id = {_clean_id(campaign_id)}")

    budgets = _rows(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign_budget.amount_micros
        FROM campaign
        WHERE {' AND '.join(where)}
        """,
    )

    spend_where = ["segments.date DURING THIS_MONTH"]
    if campaign_id:
        spend_where.append(f"campaign.id = {_clean_id(campaign_id)}")
    spent_by_campaign: Dict[str, float] = {}
    for row in _rows(
        customer_id,
        f"""
        SELECT campaign.id, metrics.cost_micros
        FROM campaign
        WHERE {' AND '.join(spend_where)}
        """,
    ):
        key = str(row["campaign"]["id"])
        spent_by_campaign[key] = spent_by_campaign.get(key, 0.0) + _from_micros(
            row.get("metrics", {}).get("cost_micros")
        )

    campaigns = []
    daily_total = 0.0
    for row in budgets:
        c = row["campaign"]
        daily = _from_micros(
            row.get("campaign_budget", {}).get("amount_micros")
        )
        daily_total += daily
        spent = round(spent_by_campaign.get(str(c["id"]), 0.0), 2)
        campaigns.append(
            {
                "campaign_id": c["id"],
                "name": c["name"],
                "daily_budget": daily,
                "spent_this_month": spent,
                "implied_monthly_ceiling": round(daily * days_in_month, 2),
            }
        )
    campaigns.sort(key=lambda c: c["spent_this_month"], reverse=True)

    spent = round(sum(c["spent_this_month"] for c in campaigns), 2)
    plan = (
        float(monthly_plan)
        if monthly_plan is not None
        else round(daily_total * days_in_month, 2)
    )
    expected_by_now = round(plan * days_elapsed / days_in_month, 2)
    projection = round(
        spent / days_elapsed * days_in_month, 2
    ) if days_elapsed else 0.0

    if plan:
        variance_pct = round((spent - expected_by_now) / plan * 100, 1)
    else:
        variance_pct = None

    if variance_pct is None:
        verdict = "No plan to compare against."
    elif variance_pct > 10:
        verdict = (
            f"Ahead of plan. At this rate the month lands at {projection}, "
            f"against a plan of {plan}."
        )
    elif variance_pct < -10:
        verdict = (
            f"Behind plan. At this rate the month lands at {projection}, "
            f"against a plan of {plan}. Underspend is only good news if the "
            f"campaigns are not losing impression share to budget -- check "
            f"get_impression_share before adding budget elsewhere."
        )
    else:
        verdict = f"On pace. Projected {projection} against a plan of {plan}."

    return {
        "today": today.isoformat(),
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "monthly_plan": plan,
        "plan_source": "given" if monthly_plan is not None
        else "sum of daily budgets x days in month",
        "spent_this_month": spent,
        "expected_by_now": expected_by_now,
        "projected_month_end": projection,
        "variance_vs_plan_pct": variance_pct,
        "verdict": verdict,
        "campaigns": campaigns,
    }


@agency_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_account_settings(
    customer_id: str,
) -> Dict[str, Any]:
    """Gets the account-level settings that silently break measurement.

    Four things sit above every campaign and are invisible from campaign
    reports: auto-tagging (off means Analytics cannot attribute anything),
    call reporting (off means every call extension records nothing), the
    conversion attribution window, and the account timezone and currency that
    every other number in this server is expressed in.

    Args:
        customer_id: The Google Ads account id.

    Returns:
        The settings, plus a `problems` list naming what is misconfigured.
    """
    customer_id = _clean_id(customer_id)
    rows = _rows(
        customer_id,
        """
        SELECT customer.id, customer.descriptive_name, customer.currency_code,
               customer.time_zone, customer.auto_tagging_enabled,
               customer.has_partners_badge, customer.manager,
               customer.test_account, customer.optimization_score,
               customer.conversion_tracking_setting.conversion_tracking_id,
               customer.conversion_tracking_setting.conversion_tracking_status,
               customer.conversion_tracking_setting.cross_account_conversion_tracking_id,
               customer.call_reporting_setting.call_reporting_enabled,
               customer.call_reporting_setting.call_conversion_reporting_enabled,
               customer.call_reporting_setting.call_conversion_action
        FROM customer
        """,
    )
    if not rows:
        raise ValueError(f"No account {customer_id}.")
    c = rows[0]["customer"]
    tracking = c.get("conversion_tracking_setting", {})
    calls = c.get("call_reporting_setting", {})

    problems = []
    if not c.get("auto_tagging_enabled"):
        problems.append(
            "Auto-tagging is OFF. Google Analytics cannot attribute any of "
            "this traffic to Ads, and offline conversion imports keyed on "
            "GCLID will not work."
        )
    if not calls.get("call_reporting_enabled"):
        problems.append(
            "Call reporting is OFF at account level. Call extensions show a "
            "raw number instead of a Google forwarding number, so calls are "
            "counted nowhere."
        )
    elif not calls.get("call_conversion_reporting_enabled"):
        problems.append(
            "Calls are forwarded but call CONVERSION reporting is off, so "
            "the calls are visible in reports and invisible to bidding."
        )
    if not tracking.get("conversion_tracking_id"):
        problems.append(
            "No conversion tracking id on the account: nothing is measured."
        )

    return {
        "customer_id": c["id"],
        "name": c.get("descriptive_name"),
        "currency": c.get("currency_code"),
        "time_zone": c.get("time_zone"),
        "test_account": bool(c.get("test_account")),
        "auto_tagging": bool(c.get("auto_tagging_enabled")),
        "conversion_tracking_id": tracking.get("conversion_tracking_id"),
        "conversion_tracking_status": tracking.get(
            "conversion_tracking_status"
        ),
        "cross_account_conversion_tracking_id": tracking.get(
            "cross_account_conversion_tracking_id"
        ),
        "call_reporting_enabled": bool(calls.get("call_reporting_enabled")),
        "call_conversion_reporting_enabled": bool(
            calls.get("call_conversion_reporting_enabled")
        ),
        "account_call_conversion_action": calls.get("call_conversion_action")
        or None,
        "optimization_score": round(float(c["optimization_score"]) * 100, 1)
        if c.get("optimization_score") is not None else None,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# Labels
#
# Labels are how a portfolio stays legible six months later: which campaigns
# belong to which test, which were rebuilt, which a client asked to pause.
# GAQL can filter on them, so a label turns "the ones we rebuilt in
# September" into a query instead of a memory.
# --------------------------------------------------------------------------


@agency_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_labels(
    customer_id: str,
) -> Dict[str, Any]:
    """Lists the account's labels and what each one is applied to.

    Args:
        customer_id: The Google Ads account id.

    Returns:
        Labels with the campaigns carrying them.
    """
    customer_id = _clean_id(customer_id)
    labels = {
        row["label"]["resource_name"]: {
            "label_id": row["label"]["id"],
            "name": row["label"]["name"],
            "status": row["label"].get("status"),
            "campaigns": [],
        }
        for row in _rows(
            customer_id,
            """
            SELECT label.id, label.name, label.status, label.resource_name
            FROM label WHERE label.status = 'ENABLED'
            """,
        )
    }

    for row in _rows(
        customer_id,
        """
        SELECT campaign_label.label, campaign.id, campaign.name
        FROM campaign_label
        """,
    ):
        entry = labels.get(row["campaign_label"]["label"])
        if entry is not None:
            entry["campaigns"].append(
                {
                    "campaign_id": row["campaign"]["id"],
                    "name": row["campaign"]["name"],
                }
            )

    return {"labels": list(labels.values()), "count": len(labels)}


@agency_mcp.tool
def create_label(
    customer_id: str,
    name: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a label. Labels change nothing about serving or spend.

    Args:
        customer_id: The Google Ads account id.
        name: The label text, e.g. "2026-09 yeniden kurulan".
        description: Optional note on what it marks.

    Returns:
        The new label's id.
    """
    customer_id = _clean_id(customer_id)
    name = str(name).strip()
    if not name:
        raise ValueError("Give the label a name.")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("LabelService")
    operation = client.get_type("LabelOperation")
    label = operation.create
    label.name = name
    # A Label carries no description of its own; the field lives on the
    # TextLabel sub-message alongside the swatch colour.
    label.text_label.background_color = "#B3D7FF"
    if description:
        label.text_label.description = description

    response = service.mutate_labels(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    return {
        "label_id": resource_name.rsplit("/", 1)[-1],
        "name": name,
        "resource_name": resource_name,
    }


@agency_mcp.tool
def apply_label_to_campaigns(
    customer_id: str,
    label_id: str,
    campaign_ids: List[str],
) -> Dict[str, Any]:
    """Applies a label to campaigns. Nothing about serving or spend changes.

    Args:
        customer_id: The Google Ads account id.
        label_id: The label to apply, from list_labels.
        campaign_ids: The campaigns to mark.

    Returns:
        How many were labelled, and which already carried it.
    """
    customer_id = _clean_id(customer_id)
    label_id = _clean_id(label_id)
    if not campaign_ids:
        raise ValueError("Give at least one campaign id.")

    label_path = utils.get_googleads_service("LabelService").label_path(
        customer_id, label_id
    )
    already = {
        str(row["campaign"]["id"])
        for row in _rows(
            customer_id,
            f"""
            SELECT campaign.id FROM campaign_label
            WHERE campaign_label.label = '{label_path}'
            """,
        )
    }

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignLabelService")
    campaign_service = utils.get_googleads_service("CampaignService")

    operations = []
    applied = []
    skipped = []
    for campaign_id in campaign_ids:
        campaign_id = _clean_id(campaign_id)
        if campaign_id in already:
            skipped.append(campaign_id)
            continue
        operation = client.get_type("CampaignLabelOperation")
        link = operation.create
        link.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        link.label = label_path
        operations.append(operation)
        applied.append(campaign_id)

    if operations:
        service.mutate_campaign_labels(
            customer_id=customer_id, operations=operations
        )

    return {
        "label_id": label_id,
        "applied_to": applied,
        "already_labelled": skipped,
    }
