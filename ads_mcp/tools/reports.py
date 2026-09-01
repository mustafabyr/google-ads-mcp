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
    return {
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
    }


_METRIC_FIELDS = (
    "metrics.impressions, metrics.clicks, metrics.ctr, metrics.cost_micros, "
    "metrics.average_cpc, metrics.conversions, metrics.conversions_value"
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
