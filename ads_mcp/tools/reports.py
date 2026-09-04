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

"""Read-only reporting shortcuts over the generic `search` tool.

These are thin GAQL wrappers, not analysis. They RETURN DATA and draw no
conclusions: the model calling this server is what reasons about the numbers
and decides what to do, and it can then act through the write tools. Putting a
second model inside the server would cost more, add a dependency, and make the
output non-deterministic for no gain.

What they do add is the right query. Field names, the micros conversion, the
date-range syntax and the joins are easy to get subtly wrong, and a wrong
report is worse than no report because it looks like an answer.

Money is returned in the ACCOUNT'S OWN currency, already converted out of
micros. Nothing here writes, so nothing here needs confirmation.
"""

import datetime
from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

reports_mcp = FastMCP("reports")

_MICROS = 1_000_000

# GAQL's own date-range literals. Anything else must be an explicit
# YYYY-MM-DD,YYYY-MM-DD pair, which is validated separately.
_DATE_RANGES = {
    "TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_14_DAYS", "LAST_30_DAYS",
    "THIS_MONTH", "LAST_MONTH", "THIS_WEEK_SUN_TODAY", "THIS_WEEK_MON_TODAY",
    "LAST_WEEK_SUN_SAT", "LAST_WEEK_MON_SUN", "LAST_BUSINESS_WEEK",
}


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _enum(block: Dict[str, Any], field: str) -> Any:
    """Reads an enum field from a proto-plus dict.

    proto-plus renames fields that collide with Python builtins, so the
    `type` field of a message arrives in the dict as `type_`. Reading
    `"type"` therefore silently returns None -- which is worse than an
    error, because a filter written against it quietly matches nothing.
    """
    if block is None:
        return None
    if field in block:
        return block[field]
    return block.get(field + "_")


def _from_micros(micros: Any) -> float:
    return round(int(micros or 0) / _MICROS, 2)


def _date_clause(date_range: str) -> str:
    """Turns a date range into a GAQL WHERE fragment, safely."""
    value = str(date_range).strip().upper()
    if value in _DATE_RANGES:
        return f"segments.date DURING {value}"

    parts = [p.strip() for p in str(date_range).split(",")]
    if len(parts) == 2:
        for part in parts:
            try:
                datetime.date.fromisoformat(part)
            except ValueError:
                raise ValueError(
                    f"Not a usable date: {part!r}. Use YYYY-MM-DD."
                ) from None
        if parts[0] > parts[1]:
            raise ValueError(
                f"The range starts after it ends: {parts[0]} to {parts[1]}."
            )
        return f"segments.date BETWEEN '{parts[0]}' AND '{parts[1]}'"

    raise ValueError(
        f"date_range must be one of {sorted(_DATE_RANGES)}, or an explicit "
        f"'YYYY-MM-DD,YYYY-MM-DD' pair. Got {date_range!r}."
    )


def _rows(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    out = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            out.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return out


def _metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    """Flattens a metrics block into plain numbers in account currency."""
    m = row.get("metrics", {})
    clicks = int(m.get("clicks") or 0)
    cost = _from_micros(m.get("cost_micros"))
    conversions = round(float(m.get("conversions") or 0), 2)
    value = round(float(m.get("conversions_value") or 0), 2)
    all_conversions = round(float(m.get("all_conversions") or 0), 2)
    out = {
        "impressions": int(m.get("impressions") or 0),
        "clicks": clicks,
        "ctr": round(float(m.get("ctr") or 0) * 100, 2),
        "cost": cost,
        "avg_cpc": _from_micros(m.get("average_cpc")),
        "conversions": conversions,
        "cost_per_conversion": round(cost / conversions, 2)
        if conversions else None,
        "conversion_value": value,
        "roas": round(value / cost, 2) if cost else None,
        "all_conversions": all_conversions,
        "cost_per_all_conversion": round(cost / all_conversions, 2)
        if all_conversions else None,
    }
    # `conversions` is only what the account chose to count; `all_conversions`
    # is everything that was recorded. When they differ, bidding is optimising
    # towards the smaller number, and a report that showed only the first one
    # would read as "this converts nothing" on an account that converts fine.
    # The flag is deliberately terse: the full explanation belongs once at the
    # top of the report, not repeated on every row.
    if all_conversions and not conversions:
        out["counted_for_bidding"] = False
    elif all_conversions and conversions and all_conversions > conversions * 1.5:
        out["counted_for_bidding"] = "partial"
    return out


_MEASUREMENT_NOTE = (
    "Some rows have counted_for_bidding false or partial: those conversions "
    "were recorded but do not reach the Conversions metric that bidding "
    "optimises against. get_conversion_performance says which actions, and "
    "update_conversion_action changes it."
)


def _measurement_note(rows: List[Dict[str, Any]]) -> Optional[str]:
    """Returns the one-off explanation when any row carries the flag."""
    if any(r.get("counted_for_bidding") is not None for r in rows):
        return _MEASUREMENT_NOTE
    return None


_METRIC_FIELDS = (
    "metrics.impressions, metrics.clicks, metrics.ctr, metrics.cost_micros, "
    "metrics.average_cpc, metrics.conversions, metrics.conversions_value, "
    "metrics.all_conversions, metrics.all_conversions_value"
)


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_campaigns(
    customer_id: str,
    include_removed: bool = False,
) -> List[Dict[str, Any]]:
    """Lists the account's campaigns with their settings.

    No metrics here, so no date range and no cost: this is the "what exists"
    call. Use get_campaign_performance for the numbers.

    Args:
        customer_id: The Google Ads account id.
        include_removed: True to include REMOVED campaigns.

    Returns:
        One row per campaign: id, name, status, channel, bidding, budget.
    """
    customer_id = _clean_id(customer_id)
    where = "" if include_removed else "WHERE campaign.status != 'REMOVED'"
    rows = _rows(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type,
               campaign.bidding_strategy_type,
               campaign.start_date_time, campaign.end_date_time,
               campaign_budget.id, campaign_budget.amount_micros
        FROM campaign
        {where}
        ORDER BY campaign.name
        """,
    )
    out = []
    for row in rows:
        c, b = row["campaign"], row.get("campaign_budget", {})
        out.append(
            {
                "campaign_id": c["id"],
                "name": c["name"],
                "status": c["status"],
                "channel": c.get("advertising_channel_type"),
                "bidding_strategy": c.get("bidding_strategy_type"),
                "daily_budget": _from_micros(b.get("amount_micros")),
                "budget_id": b.get("id"),
                "start": c.get("start_date_time"),
                "end": c.get("end_date_time") or None,
            }
        )
    return out


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_campaign_performance(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Gets campaign metrics for a period.

    Costs come back in the ACCOUNT'S OWN currency, already out of micros. CTR
    is a percentage. cost_per_conversion and roas are null when there is
    nothing to divide by, rather than zero, so an untracked campaign is not
    mistaken for a free one.

    Args:
        customer_id: The Google Ads account id.
        date_range: LAST_30_DAYS, LAST_7_DAYS, THIS_MONTH, LAST_MONTH,
            YESTERDAY, TODAY and the other GAQL literals, or an explicit
            "YYYY-MM-DD,YYYY-MM-DD" pair.
        campaign_id: Limit to one campaign. Omit for all of them.

    Returns:
        One row per campaign, ordered by cost, highest first.
    """
    customer_id = _clean_id(customer_id)
    clauses = [_date_clause(date_range), "campaign.status != 'REMOVED'"]
    if campaign_id:
        clauses.append(f"campaign.id = {_clean_id(campaign_id)}")

    rows = _rows(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status, {_METRIC_FIELDS}
        FROM campaign
        WHERE {' AND '.join(clauses)}
        ORDER BY metrics.cost_micros DESC
        """,
    )
    return [
        {
            "campaign_id": r["campaign"]["id"],
            "name": r["campaign"]["name"],
            "status": r["campaign"]["status"],
            **_metrics(r),
        }
        for r in rows
    ]


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_campaign_structure(
    customer_id: str,
    campaign_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Gets the campaign -> ad group -> ads/keywords hierarchy.

    Use this to orient before changing anything: it returns the ids the write
    tools need, so a plan can be built without guessing at structure.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: Limit to one campaign. Omit for all of them.

    Returns:
        One entry per campaign, each with its ad groups and their ad and
        keyword counts.
    """
    customer_id = _clean_id(customer_id)
    where = ["ad_group.status != 'REMOVED'", "campaign.status != 'REMOVED'"]
    if campaign_id:
        where.append(f"campaign.id = {_clean_id(campaign_id)}")

    groups = _rows(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               ad_group.id, ad_group.name, ad_group.status,
               ad_group.cpc_bid_micros
        FROM ad_group
        WHERE {' AND '.join(where)}
        ORDER BY campaign.name, ad_group.name
        """,
    )

    ad_counts: Dict[str, int] = {}
    for row in _rows(
        customer_id,
        """
        SELECT ad_group.id, ad_group_ad.ad.id
        FROM ad_group_ad WHERE ad_group_ad.status != 'REMOVED'
        """,
    ):
        key = str(row["ad_group"]["id"])
        ad_counts[key] = ad_counts.get(key, 0) + 1

    kw_counts: Dict[str, int] = {}
    for row in _rows(
        customer_id,
        """
        SELECT ad_group.id, ad_group_criterion.criterion_id
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = KEYWORD
          AND ad_group_criterion.status != 'REMOVED'
          AND ad_group_criterion.negative = false
        """,
    ):
        key = str(row["ad_group"]["id"])
        kw_counts[key] = kw_counts.get(key, 0) + 1

    by_campaign: Dict[str, Dict[str, Any]] = {}
    for row in groups:
        c, g = row["campaign"], row["ad_group"]
        entry = by_campaign.setdefault(
            str(c["id"]),
            {
                "campaign_id": c["id"],
                "name": c["name"],
                "status": c["status"],
                "ad_groups": [],
            },
        )
        gid = str(g["id"])
        entry["ad_groups"].append(
            {
                "ad_group_id": g["id"],
                "name": g["name"],
                "status": g["status"],
                "cpc_bid": _from_micros(g.get("cpc_bid_micros")),
                "ads": ad_counts.get(gid, 0),
                "keywords": kw_counts.get(gid, 0),
            }
        )
    return list(by_campaign.values())


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_search_terms(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_id: Optional[str] = None,
    min_impressions: int = 1,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Gets the real searches that triggered the ads.

    This is what people actually typed, as opposed to the keywords bid on.
    It is the raw material for negative keywords: anything here that should
    never have matched is a candidate for add_negative_keywords.

    Args:
        customer_id: The Google Ads account id.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD".
        campaign_id: Limit to one campaign. Omit for all of them.
        min_impressions: Skip terms below this, to cut the long tail.
        limit: Maximum rows, highest cost first.

    Returns:
        One row per search term, with the keyword that matched it.
    """
    customer_id = _clean_id(customer_id)
    clauses = [_date_clause(date_range)]
    if campaign_id:
        clauses.append(f"campaign.id = {_clean_id(campaign_id)}")
    if min_impressions > 1:
        clauses.append(f"metrics.impressions >= {int(min_impressions)}")

    rows = _rows(
        customer_id,
        f"""
        SELECT search_term_view.search_term,
               search_term_view.status,
               campaign.id, campaign.name,
               ad_group.id, ad_group.name,
               segments.keyword.info.text,
               segments.keyword.info.match_type,
               {_METRIC_FIELDS}
        FROM search_term_view
        WHERE {' AND '.join(clauses)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(limit)}
        """,
    )
    out = []
    for r in rows:
        kw = r.get("segments", {}).get("keyword", {}).get("info", {})
        out.append(
            {
                "search_term": r["search_term_view"]["search_term"],
                "added_or_excluded": r["search_term_view"].get("status"),
                "campaign_id": r["campaign"]["id"],
                "ad_group_id": r["ad_group"]["id"],
                "matched_keyword": kw.get("text"),
                "match_type": kw.get("match_type"),
                **_metrics(r),
            }
        )
    return out


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_wasted_spend(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    min_cost: float = 0.0,
    campaign_id: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Finds spend that produced no conversions, as data to judge.

    Deliberately named `get_`, not `analyze_`: it returns the rows and draws
    no conclusion. Zero conversions is not automatically waste -- a term can
    be new, seasonal, or assisting a conversion credited elsewhere -- so the
    decision belongs to whoever can see that context, not to this tool.

    Two lists come back. Search terms are the usual fix, via
    add_negative_keywords. Keywords are the slower one: pause or lower the
    bid rather than remove, so the history survives.

    Args:
        customer_id: The Google Ads account id.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD".
        min_cost: Ignore anything that cost less than this, in the account's
            own currency.
        campaign_id: Limit to one campaign. Omit for all of them.
        limit: Maximum rows per list.

    Returns:
        `search_terms` and `keywords`, each with cost but no conversions,
        plus the totals.
    """
    customer_id = _clean_id(customer_id)
    min_micros = int(round(max(min_cost, 0.0) * _MICROS))

    base = [_date_clause(date_range), "metrics.conversions = 0"]
    if min_micros > 0:
        base.append(f"metrics.cost_micros >= {min_micros}")
    else:
        base.append("metrics.cost_micros > 0")
    if campaign_id:
        base.append(f"campaign.id = {_clean_id(campaign_id)}")
    where = " AND ".join(base)

    terms = []
    for r in _rows(
        customer_id,
        f"""
        SELECT search_term_view.search_term, campaign.id, ad_group.id,
               segments.keyword.info.text, {_METRIC_FIELDS}
        FROM search_term_view
        WHERE {where}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(limit)}
        """,
    ):
        kw = r.get("segments", {}).get("keyword", {}).get("info", {})
        m = _metrics(r)
        terms.append(
            {
                "search_term": r["search_term_view"]["search_term"],
                "matched_keyword": kw.get("text"),
                "campaign_id": r["campaign"]["id"],
                "ad_group_id": r["ad_group"]["id"],
                "cost": m["cost"],
                "clicks": m["clicks"],
                "impressions": m["impressions"],
            }
        )

    keywords = []
    for r in _rows(
        customer_id,
        f"""
        SELECT ad_group_criterion.criterion_id,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               campaign.id, ad_group.id, {_METRIC_FIELDS}
        FROM keyword_view
        WHERE {where} AND ad_group_criterion.status != 'REMOVED'
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(limit)}
        """,
    ):
        crit = r["ad_group_criterion"]
        m = _metrics(r)
        keywords.append(
            {
                "criterion_id": crit["criterion_id"],
                "keyword": crit["keyword"]["text"],
                "match_type": crit["keyword"]["match_type"],
                "campaign_id": r["campaign"]["id"],
                "ad_group_id": r["ad_group"]["id"],
                "cost": m["cost"],
                "clicks": m["clicks"],
            }
        )

    return {
        "date_range": date_range,
        "search_terms": terms,
        "keywords": keywords,
        "total_search_term_cost": round(sum(t["cost"] for t in terms), 2),
        "total_keyword_cost": round(sum(k["cost"] for k in keywords), 2),
        "note": (
            "Zero conversions over one window is evidence, not a verdict. "
            "Check whether conversion tracking is actually installed before "
            "reading this as waste: an account with no working tag reports "
            "every term as unconverting."
        ),
    }

# --------------------------------------------------------------------------
# Conversions, impression share, keywords, ads
#
# These four answer the questions the older tools above cannot: WHAT converted,
# how much of the auction we are missing and why, which keyword deserves the
# money, and which ad is actually allowed to serve.
# --------------------------------------------------------------------------


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_conversion_performance(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Breaks conversions down by conversion action, and says which ones count.

    Google keeps two totals. `conversions` is only the actions whose
    include_in_conversions_metric is on -- the number every Smart Bidding
    strategy optimises against. `all_conversions` is everything recorded. An
    account can record hundreds of real leads and still report zero
    conversions, and then Maximize Conversions is bidding towards an event
    that never happens.

    This is the first call to make on an inherited account, before reading any
    other performance number.

    Args:
        customer_id: The Google Ads account id.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD".
        campaign_id: Limit to one campaign. Omit for the whole account.

    Returns:
        `actions`, one row per conversion action with both totals and whether
        it counts; `totals`; and `excluded_from_bidding`, the actions that
        recorded something the bidding strategy cannot see.
    """
    customer_id = _clean_id(customer_id)
    clauses = [_date_clause(date_range), "metrics.all_conversions > 0"]
    if campaign_id:
        clauses.append(f"campaign.id = {_clean_id(campaign_id)}")

    rows = _rows(
        customer_id,
        f"""
        SELECT segments.conversion_action,
               segments.conversion_action_name,
               segments.conversion_action_category,
               metrics.conversions, metrics.all_conversions,
               metrics.conversions_value, metrics.all_conversions_value
        FROM campaign
        WHERE {' AND '.join(clauses)}
        """,
    )

    # The settings live on the conversion_action resource, not on the segment.
    settings: Dict[str, Dict[str, Any]] = {}
    for row in _rows(
        customer_id,
        """
        SELECT conversion_action.resource_name, conversion_action.name,
               conversion_action.type, conversion_action.status,
               conversion_action.include_in_conversions_metric,
               conversion_action.primary_for_goal
        FROM conversion_action
        WHERE conversion_action.status != 'REMOVED'
        """,
    ):
        action = row["conversion_action"]
        settings[action["resource_name"]] = action

    merged: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        seg = row.get("segments", {})
        m = row.get("metrics", {})
        key = seg.get("conversion_action")
        entry = merged.setdefault(
            key,
            {
                "conversion_action": key,
                "name": seg.get("conversion_action_name"),
                "category": seg.get("conversion_action_category"),
                "conversions": 0.0,
                "all_conversions": 0.0,
                "conversion_value": 0.0,
            },
        )
        entry["conversions"] += float(m.get("conversions") or 0)
        entry["all_conversions"] += float(m.get("all_conversions") or 0)
        entry["conversion_value"] += float(
            m.get("all_conversions_value") or 0
        )

    actions = []
    for entry in merged.values():
        cfg = settings.get(entry["conversion_action"], {})
        counts = bool(cfg.get("include_in_conversions_metric"))
        actions.append(
            {
                "name": entry["name"],
                "category": entry["category"],
                "type": _enum(cfg, "type"),
                "conversion_action_id": (entry["conversion_action"] or "")
                .rsplit("/", 1)[-1] or None,
                "all_conversions": round(entry["all_conversions"], 2),
                "conversions": round(entry["conversions"], 2),
                "value": round(entry["conversion_value"], 2),
                "counts_towards_bidding": counts,
                "primary_for_goal": bool(cfg.get("primary_for_goal")),
            }
        )
    actions.sort(key=lambda a: a["all_conversions"], reverse=True)

    excluded = [a for a in actions if not a["counts_towards_bidding"]]
    total_all = round(sum(a["all_conversions"] for a in actions), 2)
    total_counted = round(sum(a["conversions"] for a in actions), 2)

    result = {
        "date_range": date_range,
        "actions": actions,
        "totals": {
            "all_conversions": total_all,
            "conversions_counted_for_bidding": total_counted,
            "recorded_but_not_counted": round(total_all - total_counted, 2),
        },
        "excluded_from_bidding": [
            {"name": a["name"], "all_conversions": a["all_conversions"]}
            for a in excluded
        ],
    }
    if total_all and not total_counted:
        result["verdict"] = (
            "Every recorded conversion is excluded from the Conversions "
            "metric. Any conversion-based bidding strategy in this account is "
            "optimising towards an event that never fires. Fix this before "
            "changing budgets or bids."
        )
    elif excluded:
        result["verdict"] = (
            "Some recorded conversions do not reach bidding. Decide "
            "deliberately which belong in the Conversions metric; "
            "update_conversion_action changes it."
        )
    else:
        result["verdict"] = (
            "Everything recorded also counts towards bidding."
        )
    return result


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_impression_share(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    level: str = "campaign",
    campaign_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Gets how much of the available auction the ads actually appeared in.

    This is the one report that separates "we cannot afford more" from "we are
    not good enough to win more":

      * budget_lost -- the auction was entered and lost to our own daily cap.
        Raising the budget buys these impressions directly.
      * rank_lost -- lost on Ad Rank: bid, quality score or extensions.
        Raising the budget buys nothing here.

    Shares are percentages. They are only reported for Search campaigns and
    are withheld by Google when the sample is too small, in which case the
    field is null rather than zero.

    Args:
        customer_id: The Google Ads account id.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD".
        level: "campaign" or "ad_group".
        campaign_id: Limit to one campaign. Omit for all of them.

    Returns:
        One row per campaign or ad group, ordered by cost, plus which lever
        the numbers point at.
    """
    customer_id = _clean_id(customer_id)
    level = str(level).strip().lower()
    if level not in {"campaign", "ad_group"}:
        raise ValueError("level must be 'campaign' or 'ad_group'.")

    clauses = [_date_clause(date_range), "metrics.impressions > 0"]
    if campaign_id:
        clauses.append(f"campaign.id = {_clean_id(campaign_id)}")

    name_fields = "campaign.id, campaign.name"
    share_fields = [
        "metrics.search_impression_share",
        "metrics.search_rank_lost_impression_share",
        "metrics.search_top_impression_share",
        "metrics.search_absolute_top_impression_share",
    ]
    if level == "ad_group":
        name_fields += ", ad_group.id, ad_group.name"
    else:
        # A budget belongs to a campaign, so budget-lost share is only
        # reported there: selecting it FROM ad_group is refused outright
        # ("could not support requested resources: 'AD_GROUP'").
        share_fields.insert(1, "metrics.search_budget_lost_impression_share")

    rows = _rows(
        customer_id,
        f"""
        SELECT {name_fields},
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               {', '.join(share_fields)}
        FROM {level}
        WHERE {' AND '.join(clauses)}
        ORDER BY metrics.cost_micros DESC
        """,
    )

    def pct(value: Any) -> Optional[float]:
        # Google returns a fraction, and returns nothing at all when the
        # sample is too small. None and 0 mean different things here.
        if value is None:
            return None
        return round(float(value) * 100, 2)

    def capped(*values: Any) -> Optional[str]:
        """Google reports 0.0999 for "< 10%" and 0.9001 for "> 90%"."""
        labels = set()
        for value in values:
            if value is None:
                continue
            if abs(float(value) - 0.0999) < 1e-6:
                labels.add("a share shown as < 10%")
            if abs(float(value) - 0.9001) < 1e-6:
                labels.add("a share shown as > 90%")
        return "; ".join(sorted(labels)) or None

    out = []
    for r in rows:
        m = r.get("metrics", {})
        budget_lost = pct(m.get("search_budget_lost_impression_share"))
        rank_lost = pct(m.get("search_rank_lost_impression_share"))

        if level == "ad_group":
            lever = (
                "rank only -- budget-lost share is a campaign-level figure; "
                "run this at level='campaign' for the budget half"
                if rank_lost is not None
                else "not reported for this row"
            )
        elif budget_lost is None and rank_lost is None:
            lever = "not reported for this row"
        elif (budget_lost or 0) >= (rank_lost or 0):
            lever = "budget: raise the daily budget to buy these impressions"
        else:
            lever = "rank: bid, quality score or extensions -- budget will not help"

        entry = {
            "campaign_id": r["campaign"]["id"],
            "campaign": r["campaign"]["name"],
            "impressions": int(m.get("impressions") or 0),
            "clicks": int(m.get("clicks") or 0),
            "cost": _from_micros(m.get("cost_micros")),
            "impression_share": pct(m.get("search_impression_share")),
            "budget_lost": budget_lost,
            "rank_lost": rank_lost,
            "top_share": pct(m.get("search_top_impression_share")),
            "absolute_top_share": pct(
                m.get("search_absolute_top_impression_share")
            ),
            "limited_by": lever,
        }
        sentinel = capped(
            m.get("search_impression_share"),
            m.get("search_top_impression_share"),
            m.get("search_absolute_top_impression_share"),
        )
        if sentinel:
            entry["exact_value_withheld"] = (
                f"Google reports {sentinel} rather than the exact figure."
            )
        if level == "ad_group":
            entry["ad_group_id"] = r["ad_group"]["id"]
            entry["ad_group"] = r["ad_group"]["name"]
        out.append(entry)
    return out


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_keyword_performance(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_id: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    min_impressions: int = 1,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Gets per-keyword metrics with the quality score components.

    The overall quality score is a 1-10 summary; the three components say
    which part is weak, and each has a different fix:

      * expected_ctr below average -- the keyword does not match the intent,
        or the ad does not answer the query.
      * ad_relevance below average -- the ad copy does not use the query's own
        words. Rewrite the ad, not the keyword.
      * landing_page below average -- the page does not deliver what the ad
        promised. No bid change fixes this.

    Quality score is only populated for keywords with enough traffic; it comes
    back as null otherwise, which is not the same as a low score.

    Args:
        customer_id: The Google Ads account id.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD".
        campaign_id: Limit to one campaign.
        ad_group_id: Limit to one ad group.
        min_impressions: Skip keywords below this.
        limit: Maximum rows, highest cost first.

    Returns:
        One row per keyword with its metrics, bid and quality components.
    """
    customer_id = _clean_id(customer_id)
    clauses = [
        _date_clause(date_range),
        "ad_group_criterion.status != 'REMOVED'",
        # keyword_view carries NEGATIVE keywords too, and they have no
        # metrics by definition. Left in, a list of exclusions appears as a
        # long tail of keywords with zero impressions -- which reads as dead
        # weight to cut, when it is the protection working.
        "ad_group_criterion.negative = false",
    ]
    if campaign_id:
        clauses.append(f"campaign.id = {_clean_id(campaign_id)}")
    if ad_group_id:
        clauses.append(f"ad_group.id = {_clean_id(ad_group_id)}")
    if min_impressions > 1:
        clauses.append(f"metrics.impressions >= {int(min_impressions)}")

    rows = _rows(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, ad_group.id, ad_group.name,
               ad_group_criterion.criterion_id,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               ad_group_criterion.status,
               ad_group_criterion.effective_cpc_bid_micros,
               ad_group_criterion.quality_info.quality_score,
               ad_group_criterion.quality_info.creative_quality_score,
               ad_group_criterion.quality_info.post_click_quality_score,
               ad_group_criterion.quality_info.search_predicted_ctr,
               {_METRIC_FIELDS}
        FROM keyword_view
        WHERE {' AND '.join(clauses)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(limit)}
        """,
    )

    out = []
    for r in rows:
        crit = r["ad_group_criterion"]
        quality = crit.get("quality_info", {})
        out.append(
            {
                "keyword": crit["keyword"]["text"],
                "match_type": crit["keyword"]["match_type"],
                "criterion_id": crit["criterion_id"],
                "status": crit.get("status"),
                "campaign_id": r["campaign"]["id"],
                "campaign": r["campaign"]["name"],
                "ad_group_id": r["ad_group"]["id"],
                "ad_group": r["ad_group"]["name"],
                "effective_cpc_bid": _from_micros(
                    crit.get("effective_cpc_bid_micros")
                ),
                # 0 is what the API sends for "not enough traffic to score
                # this yet". Reporting it as a quality score of zero would
                # invite pausing a keyword that has simply not run.
                "quality_score": quality.get("quality_score") or None,
                "expected_ctr": quality.get("search_predicted_ctr"),
                "ad_relevance": quality.get("creative_quality_score"),
                "landing_page": quality.get("post_click_quality_score"),
                **_metrics(r),
            }
        )
    return out


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_ad_performance(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_id: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    include_paused: bool = False,
    limit: int = 200,
) -> Dict[str, Any]:
    """Gets per-ad metrics with ad strength and whether the ad may serve at all.

    An ad with no impressions is ambiguous until you know its approval status:
    a DISAPPROVED ad and a merely unlucky one look identical in a metrics-only
    report. This returns both, and lists the disapproved ads separately so
    they cannot be missed.

    Args:
        customer_id: The Google Ads account id.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD".
        campaign_id: Limit to one campaign.
        ad_group_id: Limit to one ad group.
        include_paused: True to include PAUSED ads.
        limit: Maximum rows, highest cost first.

    Returns:
        `ads` with metrics and status, and `needs_attention` -- the ads that
        are disapproved, limited, or still under review.
    """
    customer_id = _clean_id(customer_id)
    clauses = [_date_clause(date_range)]
    if include_paused:
        clauses.append("ad_group_ad.status != 'REMOVED'")
    else:
        clauses.append("ad_group_ad.status = 'ENABLED'")
    if campaign_id:
        clauses.append(f"campaign.id = {_clean_id(campaign_id)}")
    if ad_group_id:
        clauses.append(f"ad_group.id = {_clean_id(ad_group_id)}")

    rows = _rows(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, ad_group.id, ad_group.name,
               ad_group_ad.ad.id, ad_group_ad.ad.type,
               ad_group_ad.ad.final_urls,
               ad_group_ad.status, ad_group_ad.ad_strength,
               ad_group_ad.policy_summary.approval_status,
               ad_group_ad.policy_summary.review_status,
               {_METRIC_FIELDS}
        FROM ad_group_ad
        WHERE {' AND '.join(clauses)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(limit)}
        """,
    )

    ads = []
    for r in rows:
        aga = r["ad_group_ad"]
        policy = aga.get("policy_summary", {})
        ads.append(
            {
                "ad_id": aga["ad"]["id"],
                "ad_type": _enum(aga["ad"], "type"),
                "final_urls": aga["ad"].get("final_urls") or [],
                "status": aga.get("status"),
                "ad_strength": aga.get("ad_strength"),
                "approval_status": policy.get("approval_status"),
                "review_status": policy.get("review_status"),
                "campaign_id": r["campaign"]["id"],
                "campaign": r["campaign"]["name"],
                "ad_group_id": r["ad_group"]["id"],
                "ad_group": r["ad_group"]["name"],
                **_metrics(r),
            }
        )

    blocked = {"DISAPPROVED", "AREA_OF_INTEREST_ONLY", "APPROVED_LIMITED"}
    needs_attention = [
        {
            "ad_id": a["ad_id"],
            "ad_group": a["ad_group"],
            "approval_status": a["approval_status"],
            "review_status": a["review_status"],
            "final_urls": a["final_urls"],
            "ad_strength": a["ad_strength"],
        }
        for a in ads
        if a["approval_status"] in blocked
        or a["review_status"] == "REVIEW_IN_PROGRESS"
        or a["ad_strength"] in {"POOR", "PENDING"}
    ]

    result = {
        "date_range": date_range,
        "ads": ads,
        "needs_attention": needs_attention,
        "note": (
            "A disapproved ad spends nothing and looks exactly like an ad "
            "that simply did not win, so read approval_status before reading "
            "impressions."
        ),
    }
    measurement = _measurement_note(ads)
    if measurement:
        result["measurement_note"] = measurement
    return result


# --------------------------------------------------------------------------
# Segments, change history, Google's own recommendations
# --------------------------------------------------------------------------

# Each entry: the GAQL resource to read from, the fields that identify the
# slice, and the keys they map to in the output.
_SEGMENTS = {
    "device": ("campaign", ["segments.device"], ["device"]),
    "day_of_week": ("campaign", ["segments.day_of_week"], ["day_of_week"]),
    "hour": ("campaign", ["segments.hour"], ["hour"]),
    "day_hour": (
        "campaign",
        ["segments.day_of_week", "segments.hour"],
        ["day_of_week", "hour"],
    ),
    "geo": (
        "geographic_view",
        [
            "geographic_view.location_type",
            "segments.geo_target_city",
            "segments.geo_target_most_specific_location",
        ],
        ["location_type", "city", "most_specific_location"],
    ),
    "network": ("campaign", ["segments.ad_network_type"], ["network"]),
    "date": ("campaign", ["segments.date"], ["date"]),
}


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_performance_by_segment(
    customer_id: str,
    segment: str = "device",
    date_range: str = "LAST_30_DAYS",
    campaign_id: Optional[str] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """Splits performance by device, time, network, geography or date.

    The write tools can already set device bid modifiers and an ad schedule.
    This is what tells you WHICH modifier to set: without it those settings
    are guesses.

    Segments:
      * device -- MOBILE / DESKTOP / TABLET. Feeds set_device_bid_modifiers.
      * day_of_week, hour, day_hour -- feeds set_campaign_ad_schedule. Hours
        are the account's own timezone, 0-23.
      * geo -- where the person was, split by location_type:
        LOCATION_OF_PRESENCE is people physically there,
        AREA_OF_INTEREST is people merely searching about the place. A local
        business paying for AREA_OF_INTEREST traffic is usually a targeting
        mistake, fixed by PRESENCE in set_campaign_proximity.
      * network -- SEARCH vs SEARCH_PARTNERS vs CONTENT. Display spend inside
        a Search campaign shows up here.
      * date -- the daily series, for spotting when something changed.

    Args:
        customer_id: The Google Ads account id.
        segment: One of device, day_of_week, hour, day_hour, geo, network,
            date.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD".
        campaign_id: Limit to one campaign. Omit for the whole account.
        limit: Maximum rows.

    Returns:
        `rows`, one per slice with full metrics, ordered by cost.
    """
    customer_id = _clean_id(customer_id)
    key = str(segment).strip().lower()
    if key not in _SEGMENTS:
        raise ValueError(
            f"segment must be one of {sorted(_SEGMENTS)}. Got {segment!r}."
        )
    resource, seg_fields, seg_names = _SEGMENTS[key]

    clauses = [_date_clause(date_range), "metrics.impressions > 0"]
    if campaign_id:
        clauses.append(f"campaign.id = {_clean_id(campaign_id)}")

    rows = _rows(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name,
               {', '.join(seg_fields)},
               {_METRIC_FIELDS}
        FROM {resource}
        WHERE {' AND '.join(clauses)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(limit)}
        """,
    )

    out = []
    for r in rows:
        entry = {
            "campaign_id": r["campaign"]["id"],
            "campaign": r["campaign"]["name"],
        }
        for field, name in zip(seg_fields, seg_names):
            head, tail = field.split(".", 1)
            block = r.get(head, {})
            for part in tail.split(".")[:-1]:
                block = block.get(part, {})
            entry[name] = block.get(tail.split(".")[-1])
        entry.update(_metrics(r))
        out.append(entry)

    result: Dict[str, Any] = {
        "segment": key,
        "date_range": date_range,
        "rows": out,
    }
    note = _measurement_note(out)
    if note:
        result["measurement_note"] = note
    if key == "geo":
        interest_cost = sum(
            e["cost"] for e in out if e.get("location_type") == "AREA_OF_INTEREST"
        )
        total_cost = sum(e["cost"] for e in out)
        if total_cost:
            result["area_of_interest_share_of_cost"] = round(
                interest_cost / total_cost * 100, 1
            )
            result["note"] = (
                "AREA_OF_INTEREST is people searching ABOUT the location from "
                "somewhere else. For a business people have to walk into, that "
                "share should be small; positive_geo_target_type = PRESENCE "
                "removes it."
            )
    return result


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_change_history(
    customer_id: str,
    date_range: str = "LAST_14_DAYS",
    campaign_id: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Gets who changed what in the account, and when.

    The first question to ask when performance moves without an obvious cause.
    It also exposes changes nobody on the team made: Google's auto-apply
    subscription edits live accounts under the user_email
    "Recommendations Auto-Apply", and removing a negative keyword that way
    can reopen spend on traffic somebody deliberately shut off.

    Google keeps roughly 30 days of change history and caps the query at
    10,000 rows; the range must be given as change_event dates, which this
    handles.

    Args:
        customer_id: The Google Ads account id.
        date_range: A GAQL literal or "YYYY-MM-DD,YYYY-MM-DD". Beyond about
            30 days the API returns nothing.
        campaign_id: Limit to one campaign.
        limit: Maximum rows, at most 10000. Newest first.

    Returns:
        `changes` newest first, and `by_author` -- a count per user, which is
        where an auto-apply subscription becomes visible.
    """
    customer_id = _clean_id(customer_id)
    # change_event filters on its own timestamp field, not segments.date.
    clause = _date_clause(date_range).replace(
        "segments.date", "change_event.change_date_time"
    )
    clauses = [clause]
    if campaign_id:
        clauses.append(
            f"change_event.campaign = "
            f"'customers/{customer_id}/campaigns/{_clean_id(campaign_id)}'"
        )

    rows = _rows(
        customer_id,
        f"""
        SELECT change_event.change_date_time,
               change_event.change_resource_type,
               change_event.resource_change_operation,
               change_event.user_email,
               change_event.client_type,
               change_event.changed_fields,
               change_event.campaign,
               change_event.ad_group
        FROM change_event
        WHERE {' AND '.join(clauses)}
        ORDER BY change_event.change_date_time DESC
        LIMIT {min(int(limit), 10000)}
        """,
    )

    # One mutate of twenty sitelinks writes twenty identical change_event
    # rows. Listing them separately buries the changes that differ, so
    # identical rows are collapsed into one with a count.
    changes: List[Dict[str, Any]] = []
    by_author: Dict[str, int] = {}
    seen: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        e = r["change_event"]
        author = e.get("user_email") or "unknown"
        by_author[author] = by_author.get(author, 0) + 1
        entry = {
            "when": e.get("change_date_time"),
            "who": author,
            "via": e.get("client_type"),
            "operation": e.get("resource_change_operation"),
            "resource_type": e.get("change_resource_type"),
            "fields": e.get("changed_fields"),
            "campaign_id": (e.get("campaign") or "").rsplit("/", 1)[-1]
            or None,
            "ad_group_id": (e.get("ad_group") or "").rsplit("/", 1)[-1]
            or None,
        }
        key = tuple(entry.values())
        if key in seen:
            seen[key]["count"] = seen[key].get("count", 1) + 1
            continue
        seen[key] = entry
        changes.append(entry)

    automated = {
        author: count
        for author, count in by_author.items()
        if "auto-apply" in author.lower() or "google" in author.lower()
    }
    result: Dict[str, Any] = {
        "date_range": date_range,
        "changes": changes,
        "by_author": by_author,
    }
    if automated:
        result["automated_changes"] = automated
        result["warning"] = (
            "Some changes were made by Google automation rather than a "
            "person. Check what it touched before attributing a performance "
            "change to the team's own work."
        )
    return result


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_recommendations(
    customer_id: str,
    include_dismissed: bool = False,
) -> Dict[str, Any]:
    """Lists Google's own recommendations, and the account optimisation score.

    Read this; do not apply it blindly. Several recommendation types exist to
    grow Google's inventory rather than the advertiser's result --
    SEARCH_PARTNERS_OPT_IN and DISPLAY_EXPANSION_OPT_IN both widen where the
    ads run, which for a tightly-targeted local account is usually spend
    leaving the target audience. Others, such as
    RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH or adding a missing extension,
    are worth acting on.

    Optimisation score is Google's own metric and is NOT a performance
    measure: an account can score 100% while converting nothing.

    Args:
        customer_id: The Google Ads account id.
        include_dismissed: True to also list ones already dismissed.

    Returns:
        `recommendations` grouped by type, and `optimization_score`.
    """
    customer_id = _clean_id(customer_id)
    clauses = [] if include_dismissed else ["recommendation.dismissed = false"]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = _rows(
        customer_id,
        f"""
        SELECT recommendation.type, recommendation.resource_name,
               recommendation.campaign, recommendation.dismissed,
               campaign.name
        FROM recommendation
        {where}
        """,
    )

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        rec = r["recommendation"]
        grouped.setdefault(_enum(rec, "type"), []).append(
            {
                "campaign_id": (rec.get("campaign") or "").rsplit("/", 1)[-1]
                or None,
                "campaign": r.get("campaign", {}).get("name"),
                "dismissed": rec.get("dismissed"),
                "resource_name": rec["resource_name"],
            }
        )

    score_rows = _rows(
        customer_id,
        """
        SELECT customer.id, customer.descriptive_name,
               customer.optimization_score
        FROM customer
        """,
    )
    score = None
    if score_rows:
        raw = score_rows[0]["customer"].get("optimization_score")
        score = round(float(raw) * 100, 1) if raw is not None else None

    inventory_widening = [
        t for t in grouped
        if t in {
            "SEARCH_PARTNERS_OPT_IN",
            "DISPLAY_EXPANSION_OPT_IN",
            "OPTIMIZE_AD_ROTATION",
            "MAXIMIZE_CLICKS_OPT_IN",
            "TARGET_ROAS_OPT_IN",
            "MOVE_UNUSED_BUDGET",
        }
    ]

    return {
        "optimization_score": score,
        "counts": {t: len(v) for t, v in grouped.items()},
        "recommendations": grouped,
        "widens_where_ads_run": inventory_widening,
        "note": (
            "Recommendations under widens_where_ads_run add inventory rather "
            "than precision. On a tightly targeted account they usually cost "
            "money. Decide each one on its own merits and record the "
            "decision; optimisation score measures compliance with these "
            "suggestions, not results."
        ),
    }
