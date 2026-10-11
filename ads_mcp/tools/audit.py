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

"""One audit, run the same way against every account.

Every other read tool here answers a question you already knew to ask.
This one asks the questions for you, and it asks the same ones of every
account, which is the only way a portfolio stays at one standard: a
mistake found in one account becomes a check that runs against all of
them.

Every check in here was earned. Each one exists because the thing it
looks for was actually found in a live account and cost money or reach
before anyone noticed:

  * an account recording 1791 conversions that a report called "not
    counted", because the report read the action's own flag instead of
    the conversion goal that actually decides;
  * four campaigns on PRESENCE_OR_INTEREST, where 90.8% of the geo spend
    went to people merely INTERESTED in the city;
  * negative keywords deleted five times in a month by an auto-apply
    subscription nobody had turned off;
  * a 108-term negative list attached to nothing;
  * two ads disapproved for a final URL that did not exist;
  * one ad per ad group everywhere, so nothing was ever being tested;
  * Maximize Clicks with no CPC ceiling, paying whatever the auction asked;
  * a Target Impression Share campaign aiming low, losing to rank and
    leaving budget unspent, all at once;
  * calls biddable on an account whose contract said forms only;
  * a single biddable goal that had recorded nothing for a month;
  * a call extension pointed at a conversion action that was never set;
  * two OUTBOUND_CLICK actions counting the same click twice;
  * one-word broad negatives in a shared list, and negatives that blocked
    the account's own keywords;
  * one ad group taking most of a campaign's money and converting nothing;
  * responsive search ads with a handful of headlines;
  * changes made in the interface that nobody had written down.

Read-only. It changes nothing and it asks for no confirmation; it is
meant to be safe to run on any account at any time.
"""

from typing import Any, Dict, List, Optional
import datetime

import proto
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.tools import _bidding

audit_mcp = FastMCP("audit")

_MICROS = 1_000_000

# Impression share comes back as these two sentinels rather than a number
# when Google will not disclose the exact figure.
_IS_FLOOR = 0.0999
_IS_CEILING = 0.9001

CRITICAL = "critical"
WARNING = "warning"
NOTE = "note"

# Thresholds. Every one of these has a row in docs/kaynaklar.md; the
# KONVANSIYON ones are practice, not Google's word, and the findings say so.
_TIS_LOW_TARGET = 0.50  # Target Impression Share below this is "low"
_RANK_LOST_HIGH = 0.30  # losing this much to rank is "high"
_BUDGET_LOST_LOW = 0.05  # losing this little to budget is "unused"
_AD_GROUP_SPEND_SHARE = 0.50  # one ad group taking more than this
_MIN_RSA_HEADLINES = 12  # fewer headlines than this is thin
_UNLOGGED_WINDOW_DAYS = 7  # interface changes are compared over this

# Conversion categories that mean "a phone call".
_CALL_CATEGORIES = {"PHONE_CALL_LEAD", "CONTACT"}
_LEAD_CONTRACTS = {"FORM_ONLY", "CALLS_ONLY", "FORMS_AND_CALLS"}

_ORDER = {CRITICAL: 0, WARNING: 1, NOTE: 2}


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _from_micros(micros: Any) -> float:
    return round(int(micros or 0) / _MICROS, 2)


def _enum(value: Any) -> Optional[str]:
    """proto-plus gives enums back as objects in some paths, strings in others."""
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _rows(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    out = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            out.append(proto.Message.to_dict(row, use_integers_for_enums=False))
    return out


class _Findings:
    """Collects findings so each check can stay a few lines long."""

    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(
        self,
        severity: str,
        area: str,
        title: str,
        fix: str,
        evidence: Optional[Any] = None,
    ) -> None:
        item = {
            "severity": severity,
            "area": area,
            "finding": title,
            "fix": fix,
        }
        if evidence is not None:
            item["evidence"] = evidence
        self.items.append(item)

    def sorted(self) -> List[Dict[str, Any]]:
        return sorted(self.items, key=lambda f: (_ORDER[f["severity"]], f["area"]))


# --------------------------------------------------------------------------
# Individual checks. Each takes the findings collector and adds to it; each
# is responsible for its own queries so a check can be read on its own.
# --------------------------------------------------------------------------


def _check_account_settings(cid: str, f: _Findings) -> Dict[str, Any]:
    rows = _rows(
        cid,
        """
        SELECT customer.id, customer.descriptive_name, customer.currency_code,
               customer.time_zone, customer.auto_tagging_enabled,
               customer.test_account, customer.optimization_score,
               customer.conversion_tracking_setting.conversion_tracking_id,
               customer.conversion_tracking_setting.conversion_tracking_status,
               customer.call_reporting_setting.call_reporting_enabled,
               customer.call_reporting_setting.call_conversion_reporting_enabled
        FROM customer
        """,
    )
    if not rows:
        raise ValueError(f"No account {cid}.")
    c = rows[0]["customer"]
    tracking = c.get("conversion_tracking_setting", {})
    calls = c.get("call_reporting_setting", {})

    if not c.get("auto_tagging_enabled"):
        f.add(
            CRITICAL,
            "measurement",
            "Auto-tagging is off.",
            "Turn on auto-tagging. Without a gclid, Analytics attributes "
            "this traffic to nothing and offline conversion imports cannot "
            "be matched back to a click.",
        )
    if not tracking.get("conversion_tracking_id"):
        f.add(
            CRITICAL,
            "measurement",
            "No conversion tracking id on the account.",
            "Nothing is measured. Set up conversion tracking before "
            "spending anything else.",
        )
    if not calls.get("call_reporting_enabled"):
        f.add(
            WARNING,
            "measurement",
            "Call reporting is off at account level.",
            "Call extensions will show the raw number instead of a Google "
            "forwarding number, so calls are counted nowhere.",
        )
    elif not calls.get("call_conversion_reporting_enabled"):
        f.add(
            WARNING,
            "measurement",
            "Calls are forwarded but call conversion reporting is off.",
            "The calls appear in reports and stay invisible to bidding.",
        )

    return {
        "customer_id": c["id"],
        "name": c.get("descriptive_name"),
        "currency": c.get("currency_code"),
        "time_zone": c.get("time_zone"),
        "optimization_score": round(float(c["optimization_score"]) * 100, 1)
        if c.get("optimization_score") is not None
        else None,
    }


def _check_conversions(
    cid: str, f: _Findings, lead_contract: Optional[str] = None
) -> Dict[str, Any]:
    """What actually reaches bidding, decided by the goals -- not the flags.

    The account's conversion GOALS, keyed by (category, origin), decide
    what counts. An action's own include_in_conversions_metric can say
    false while its goal is biddable and the conversions count anyway.
    Reading the flag alone reports working measurement as broken, which
    sends you to fix something that is not wrong.
    """
    goals: Dict[tuple, bool] = {}
    for row in _rows(
        cid,
        """
        SELECT customer_conversion_goal.category,
               customer_conversion_goal.origin,
               customer_conversion_goal.biddable
        FROM customer_conversion_goal
        """,
    ):
        g = row["customer_conversion_goal"]
        goals[(_enum(g.get("category")), _enum(g.get("origin")))] = bool(
            g.get("biddable")
        )

    # Metrics cannot be selected FROM conversion_action -- the API rejects it
    # ("metric is incompatible with the resource in the FROM clause"). The
    # numbers come from campaign segmented by conversion action; the settings
    # come from the conversion_action resource with no metrics. Two queries,
    # joined here.
    settings: Dict[str, Dict[str, Any]] = {}
    for row in _rows(
        cid,
        """
        SELECT conversion_action.resource_name, conversion_action.name,
               conversion_action.category, conversion_action.origin,
               conversion_action.status,
               conversion_action.include_in_conversions_metric
        FROM conversion_action
        WHERE conversion_action.status != 'REMOVED'
        """,
    ):
        a = row["conversion_action"]
        settings[a["resource_name"]] = a

    rows = _rows(
        cid,
        """
        SELECT segments.conversion_action, segments.conversion_action_name,
               segments.conversion_action_category,
               metrics.conversions, metrics.all_conversions
        FROM campaign
        WHERE segments.date DURING LAST_30_DAYS
          AND metrics.all_conversions > 0
        """,
    )

    totals: Dict[str, Dict[str, Any]] = {}
    by_category: Dict[str, float] = {}
    for row in rows:
        seg = row.get("segments", {})
        m = row.get("metrics", {})
        key = seg.get("conversion_action")
        entry = totals.setdefault(
            key,
            {"name": seg.get("conversion_action_name"), "conversions": 0.0, "all": 0.0},
        )
        entry["conversions"] += float(m.get("conversions") or 0)
        entry["all"] += float(m.get("all_conversions") or 0)
        category = _enum(seg.get("conversion_action_category")) or "UNKNOWN"
        by_category[category] = by_category.get(category, 0.0) + float(
            m.get("all_conversions") or 0
        )

    counted = 0.0
    stranded: List[Dict[str, Any]] = []
    for resource, entry in totals.items():
        a = settings.get(resource, {})
        key = (_enum(a.get("category")), _enum(a.get("origin")))
        biddable = goals.get(key, bool(a.get("include_in_conversions_metric")))
        if biddable:
            counted += entry["conversions"]
        elif entry["all"] > 0:
            stranded.append(
                {"name": entry["name"] or a.get("name"), "all_conversions": round(entry["all"], 1)}
            )

    if counted == 0 and stranded:
        f.add(
            CRITICAL,
            "measurement",
            "The account records conversions but none of them reach bidding.",
            "Smart bidding is optimising towards nothing and every "
            "conversion column in every report reads zero. Make at least "
            "one goal biddable.",
            evidence=stranded[:5],
        )
    elif stranded:
        total_stranded = round(sum(s["all_conversions"] for s in stranded), 1)
        # Google-hosted local actions are excluded on purpose in most
        # accounts, so this is a question, not a verdict.
        f.add(
            NOTE,
            "measurement",
            f"{total_stranded} recorded conversions do not reach bidding.",
            "Confirm this is deliberate. Secondary actions belong outside "
            "bidding; a real lead action outside it is a leak.",
            evidence=stranded[:5],
        )

    # --- the goals that bid, and whether they have recorded anything ------
    biddable_categories = sorted({c for (c, _), b in goals.items() if b if c})
    silent = [
        c for c in biddable_categories if by_category.get(c, 0.0) <= 0
    ]
    if biddable_categories and len(silent) == len(biddable_categories):
        f.add(
            CRITICAL,
            "measurement",
            "Every goal the account bids toward recorded nothing in 30 days."
            if len(biddable_categories) > 1
            else "The only goal the account bids toward recorded nothing in "
            "30 days.",
            "Smart bidding has no signal at all: either the tag is not "
            "firing, the wrong category is biddable, or the action that "
            "does record is not a goal. Check the tag first, then the goals.",
            evidence={"biddable": biddable_categories, "recorded_30d": by_category},
        )
    elif silent:
        f.add(
            NOTE,
            "measurement",
            f"{len(silent)} biddable goal(s) recorded nothing in 30 days.",
            "A biddable category with no records adds nothing and hides a "
            "broken tag. Confirm each one still fires.",
            evidence={"silent": silent, "recorded_30d": by_category},
        )

    # --- the contract: forms only, but calls are biddable -----------------
    contract = str(lead_contract or "").upper() or None
    if contract and contract not in _LEAD_CONTRACTS:
        raise ValueError(
            f"lead_contract must be one of {sorted(_LEAD_CONTRACTS)}, not "
            f"{lead_contract!r}."
        )
    if contract == "FORM_ONLY":
        call_goals = sorted(
            f"{c}/{o}" for (c, o), b in goals.items() if b and c in _CALL_CATEGORIES
        )
        if call_goals:
            f.add(
                WARNING,
                "measurement",
                "The contract counts forms only, but call goals are biddable.",
                "Bidding is chasing phone calls the client does not pay for. "
                "Make CONTACT and PHONE_CALL_LEAD non-biddable at account "
                "level, or on the campaigns, with set_campaign_conversion_goals.",
                evidence=call_goals,
            )
    if contract == "CALLS_ONLY":
        form_goals = sorted(
            f"{c}/{o}" for (c, o), b in goals.items()
            if b and c in {"SUBMIT_LEAD_FORM", "SIGNUP", "REQUEST_QUOTE"}
        )
        if form_goals:
            f.add(
                WARNING,
                "measurement",
                "The contract counts calls only, but form goals are biddable.",
                "Bidding is chasing forms the client does not pay for.",
                evidence=form_goals,
            )

    # --- the same click counted twice ------------------------------------
    outbound = [
        a.get("name")
        for a in settings.values()
        if _enum(a.get("category")) == "OUTBOUND_CLICK"
        and _enum(a.get("status")) == "ENABLED"
    ]
    if len(outbound) > 1:
        f.add(
            WARNING,
            "measurement",
            f"{len(outbound)} enabled OUTBOUND_CLICK actions.",
            "The same outbound click (WhatsApp, a phone link) is recorded by "
            "each of them, so conversions and cost per conversion are "
            "wrong by that factor. Keep one; retire the rest in the "
            "interface (the API cannot change an action's status).",
            evidence=outbound,
        )

    return {
        "biddable_goals": sorted(
            f"{c}/{o}" for (c, o), b in goals.items() if b
        ),
        "conversions_counted_30d": round(counted, 1),
        "recorded_by_category_30d": {
            k: round(v, 1) for k, v in sorted(by_category.items())
        },
    }


def _check_campaigns(
    cid: str, f: _Findings, brand_campaign_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    brand = {_clean_id(b) for b in (brand_campaign_ids or [])}
    rows = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type,
               campaign.bidding_strategy_type,
               campaign.bidding_strategy,
               campaign.bidding_strategy_system_status,
               campaign.target_spend.cpc_bid_ceiling_micros,
               campaign.target_impression_share.location,
               campaign.target_impression_share.location_fraction_micros,
               campaign.network_settings.target_search_network,
               campaign.network_settings.target_content_network,
               campaign.geo_target_type_setting.positive_geo_target_type,
               campaign_budget.amount_micros,
               campaign_budget.id,
               metrics.cost_micros, metrics.clicks, metrics.conversions,
               metrics.search_budget_lost_impression_share,
               metrics.search_rank_lost_impression_share,
               metrics.search_impression_share
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND segments.date DURING LAST_30_DAYS
        """,
    )

    interest = []
    starved = []
    thin_is = []
    no_ceiling = []
    on_portfolio = []
    system_status = []
    tis_stuck = []
    partners = []
    display = []
    budget_ids: Dict[str, List[str]] = {}
    live: List[Dict[str, Any]] = []

    for row in rows:
        c = row["campaign"]
        m = row.get("metrics", {})
        b = row.get("campaign_budget", {})
        name = c.get("name")
        cost = _from_micros(m.get("cost_micros"))
        bidding = _enum(c.get("bidding_strategy_type"))
        channel = _enum(c.get("advertising_channel_type"))
        live.append(
            {
                "id": c["id"],
                "name": name,
                "channel": channel,
                "bidding": bidding,
                "cost_30d": cost,
                "conversions_30d": round(float(m.get("conversions") or 0), 1),
            }
        )

        geo = _enum(
            (c.get("geo_target_type_setting") or {}).get("positive_geo_target_type")
        )
        if geo == "PRESENCE_OR_INTEREST" and str(c["id"]) not in brand:
            interest.append(name)

        # A campaign can sit on a PORTFOLIO (shared) bidding strategy,
        # and then NONE of its bidding settings are on the campaign:
        # campaign.target_spend.cpc_bid_ceiling_micros reads 0 because
        # the ceiling is on the strategy. Reading that 0 as "no ceiling"
        # is how this audit once told an operator to run
        # update_bid_strategy on such a campaign, which Google refuses
        # with BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED. Those
        # campaigns belong to _check_bid_strategies, not here.
        portfolio = str(c.get("bidding_strategy") or "")
        if portfolio:
            on_portfolio.append(
                {
                    "campaign": name,
                    "bidding": bidding,
                    "bidding_strategy_id": portfolio.rsplit("/", 1)[-1],
                }
            )

        # Maximize Clicks is TARGET_SPEND underneath. With no ceiling it
        # pays whatever the auction asks for a click.
        if bidding == "TARGET_SPEND" and not portfolio:
            ceiling = int((c.get("target_spend") or {}).get("cpc_bid_ceiling_micros") or 0)
            if not ceiling:
                no_ceiling.append(name)

        # Google fills this in for free and almost nobody reads it.
        meaning = _bidding.describe_system_status(
            _enum(c.get("bidding_strategy_system_status"))
        )
        if meaning:
            system_status.append(
                {
                    "campaign": name,
                    "status": _enum(c.get("bidding_strategy_system_status")),
                    "means": meaning,
                }
            )

        rank_lost = float(m.get("search_rank_lost_impression_share") or 0)
        budget_lost = float(m.get("search_budget_lost_impression_share") or 0)
        if bidding == "TARGET_IMPRESSION_SHARE":
            tis = c.get("target_impression_share") or {}
            fraction = int(tis.get("location_fraction_micros") or 0) / 1_000_000
            if (
                fraction < _TIS_LOW_TARGET
                and rank_lost >= _RANK_LOST_HIGH
                and budget_lost < _BUDGET_LOST_LOW
            ):
                tis_stuck.append(
                    {
                        "campaign": name,
                        "target_share": round(fraction * 100, 1),
                        "rank_lost_is": round(rank_lost * 100, 1),
                        "budget_lost_is": round(budget_lost * 100, 1),
                    }
                )

        network = c.get("network_settings") or {}
        if channel == "SEARCH":
            if network.get("target_search_network"):
                partners.append(name)
            if network.get("target_content_network"):
                display.append(name)

        lost = m.get("search_budget_lost_impression_share")
        if lost is not None and float(lost) >= 0.10:
            starved.append({"campaign": name, "budget_lost_is": round(float(lost) * 100, 1)})

        share = m.get("search_impression_share")
        if share is not None and float(share) < 0.30 and cost > 0:
            thin_is.append({"campaign": name, "impression_share": round(float(share) * 100, 1)})

        bid = b.get("id")
        if bid:
            budget_ids.setdefault(str(bid), []).append(name)

    if interest:
        f.add(
            WARNING,
            "targeting",
            f"{len(interest)} live campaign(s) target location by "
            "PRESENCE_OR_INTEREST.",
            "Switch to PRESENCE. Interest targeting serves to people who "
            "merely searched about the area from anywhere in the world, "
            "and it is on by default so it is almost never a decision "
            "anyone made. set_campaign_geo_target_type changes it.",
            evidence=interest,
        )
    if starved:
        f.add(
            WARNING,
            "budget",
            f"{len(starved)} campaign(s) lose 10%+ of impressions to budget.",
            "Demand exists that the budget is refusing. Raise the budget or "
            "narrow the targeting; leaving it is paying for a queue you "
            "never serve.",
            evidence=starved,
        )
    shared = {b: names for b, names in budget_ids.items() if len(names) > 1}
    if shared:
        f.add(
            NOTE,
            "budget",
            "Campaigns share a budget.",
            "A shared budget means the campaigns compete with each other "
            "and neither can be controlled on its own.",
            evidence=shared,
        )
    if thin_is:
        f.add(
            NOTE,
            "reach",
            f"{len(thin_is)} campaign(s) hold under 30% impression share.",
            "Most of the auction is going to someone else. Check rank "
            "(bid and quality) before budget.",
            evidence=thin_is[:8],
        )
    if no_ceiling:
        f.add(
            WARNING,
            "bidding",
            f"{len(no_ceiling)} Maximize Clicks campaign(s) have no CPC ceiling.",
            "Without target_spend.cpc_bid_ceiling_micros the strategy pays "
            "whatever holding a click costs. Set a ceiling with "
            "update_bid_strategy(MAXIMIZE_CLICKS, cpc_bid_ceiling=...).",
            evidence=no_ceiling,
        )
    if on_portfolio:
        f.add(
            NOTE,
            "bidding",
            f"{len(on_portfolio)} live campaign(s) take their bidding from "
            f"a portfolio (shared) strategy.",
            "Their ceilings and targets are NOT on the campaign, so the "
            "campaign-level numbers in this report read as zero and "
            "update_bid_strategy cannot write them. Read "
            "list_bidding_strategies and change them with "
            "update_portfolio_bid_ceiling.",
            evidence=on_portfolio[:8],
        )
    if system_status:
        misconfigured = [
            x for x in system_status if x["status"].startswith("MISCONFIGURED")
        ]
        f.add(
            CRITICAL if misconfigured else WARNING,
            "bidding",
            f"Google reports a bidding problem on {len(system_status)} live "
            f"campaign(s) in campaign.bidding_strategy_system_status.",
            "This is Google's own verdict on the strategy, not an "
            "inference. MISCONFIGURED_* means the strategy cannot do its "
            "job at all (a conversion-based strategy with no usable "
            "conversion type is optimising towards nothing); LIMITED_BY_* "
            "names what is holding it back, and LIMITED_BY_CPC_BID_CEILING "
            "means the ceiling rather than the budget is the limit.",
            evidence=system_status[:8],
        )
    if tis_stuck:
        f.add(
            WARNING,
            "bidding",
            f"{len(tis_stuck)} Target Impression Share campaign(s) aim low, "
            "lose to rank and leave budget unused.",
            f"The target is under {int(_TIS_LOW_TARGET * 100)}%, rank-lost "
            f"share is over {int(_RANK_LOST_HIGH * 100)}% and budget-lost "
            f"share is under {int(_BUDGET_LOST_LOW * 100)}%: the strategy has "
            "stopped competing while money sits unspent. Raise the target "
            "or the CPC ceiling (thresholds are practice, not Google's).",
            evidence=tis_stuck,
        )
    if display:
        f.add(
            WARNING,
            "targeting",
            f"{len(display)} search campaign(s) also serve on the Display "
            "network.",
            "Display placements spend a search budget on people who were "
            "not searching. Turn it off with update_campaign(content_network"
            "=False).",
            evidence=display,
        )
    if partners:
        f.add(
            NOTE,
            "targeting",
            f"{len(partners)} search campaign(s) include search partners.",
            "Partner traffic converts worse on most accounts and cannot be "
            "bid separately. Compare with get_performance_by_segment(network)"
            " and turn it off if it does not pay.",
            evidence=partners,
        )

    return {"live_campaigns": live}


def _check_bid_strategies(cid: str, f: _Findings) -> None:
    """Portfolio (shared) bidding strategies: the settings nobody can see.

    Every check here has the same live account behind it. A Maximize
    Conversions portfolio strategy, aligned to a shared budget, carried
    two campaigns; its own ceiling was 0 and Google had been reporting
    MISCONFIGURED_CONVERSION_TYPES on both for months. Nothing in a
    campaign report, a structure report or this audit said so, because
    every one of them read the campaign and the settings were not on the
    campaign.
    """
    rows = _rows(
        cid,
        """
        SELECT bidding_strategy.id, bidding_strategy.name,
               bidding_strategy.type, bidding_strategy.status,
               bidding_strategy.aligned_campaign_budget_id,
               bidding_strategy.campaign_count,
               bidding_strategy.non_removed_campaign_count,
               bidding_strategy.target_spend.cpc_bid_ceiling_micros,
               bidding_strategy.target_impression_share.cpc_bid_ceiling_micros,
               bidding_strategy.maximize_conversions.cpc_bid_ceiling_micros,
               bidding_strategy.maximize_conversion_value.cpc_bid_ceiling_micros,
               bidding_strategy.target_cpa.cpc_bid_ceiling_micros,
               bidding_strategy.target_roas.cpc_bid_ceiling_micros
        FROM bidding_strategy
        WHERE bidding_strategy.status != 'REMOVED'
        """,
    )
    if not rows:
        return

    no_ceiling = []
    aligned = []
    single = []
    empty = []
    for row in rows:
        strategy = row.get("bidding_strategy") or {}
        kind = _bidding.read_type(strategy)
        name = strategy.get("name")
        used = int(strategy.get("non_removed_campaign_count") or 0)
        path = _bidding.PORTFOLIO_CEILING_PATHS.get(kind)
        ceiling = 0
        if path:
            block, leaf = path.split(".", 1)
            ceiling = int((strategy.get(block) or {}).get(leaf) or 0)
        entry = {
            "strategy": name,
            "bidding_strategy_id": str(strategy.get("id") or ""),
            "type": kind,
            "campaigns": used,
        }
        if path and not ceiling and used:
            no_ceiling.append(entry)
        if int(strategy.get("aligned_campaign_budget_id") or 0):
            aligned.append(
                dict(
                    entry,
                    aligned_campaign_budget_id=str(
                        strategy["aligned_campaign_budget_id"]
                    ),
                )
            )
        if used == 1:
            single.append(entry)
        if not used:
            empty.append(entry)

    if no_ceiling:
        f.add(
            WARNING,
            "bidding",
            f"{len(no_ceiling)} portfolio bidding strategy(ies) carry no "
            f"CPC ceiling.",
            "A ceiling of 0 on a strategy means no ceiling: it pays "
            "whatever a click costs in the auction, for every campaign on "
            "it. Set one with update_portfolio_bid_ceiling -- the "
            "campaign-level tool cannot reach it.",
            evidence=no_ceiling,
        )
    if aligned:
        f.add(
            NOTE,
            "bidding",
            f"{len(aligned)} portfolio strategy(ies) are aligned to a "
            f"shared campaign budget.",
            "Google keeps the two together: every campaign on the strategy "
            "uses that budget and every campaign on that budget uses the "
            "strategy, so neither can be changed campaign by campaign. "
            "Moving one campaign out needs "
            "detach_campaign_from_portfolio with either its own budget or "
            "an explicit unalign.",
            evidence=aligned,
        )
    if single:
        f.add(
            NOTE,
            "bidding",
            f"{len(single)} portfolio strategy(ies) carry a single "
            f"campaign.",
            "A shared strategy with one campaign on it gains nothing from "
            "being shared and costs the campaign its own settings: the "
            "ceiling cannot be changed campaign by campaign and every "
            "campaign-level bidding tool refuses. Consider moving it onto "
            "its own strategy. (Practice, not a Google rule.)",
            evidence=single,
        )
    if empty:
        f.add(
            NOTE,
            "bidding",
            f"{len(empty)} portfolio strategy(ies) have no live campaign "
            f"on them.",
            "They change nothing while they sit there, but they are the "
            "ones an operator picks by mistake. Remove them in the "
            "interface if they are finished with.",
            evidence=empty,
        )


def _check_ads(cid: str, f: _Findings) -> None:
    rows = _rows(
        cid,
        """
        SELECT campaign.name, ad_group.id, ad_group.name, ad_group.status,
               ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status,
               ad_group_ad.ad.responsive_search_ad.headlines,
               ad_group_ad.ad_strength,
               ad_group_ad.policy_summary.approval_status,
               ad_group_ad.policy_summary.policy_topic_entries
        FROM ad_group_ad
        WHERE ad_group_ad.status != 'REMOVED'
          AND ad_group.status = 'ENABLED'
          AND campaign.status = 'ENABLED'
        """,
    )

    disapproved = []
    limited = []
    poor = []
    thin = []
    per_group: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        ad = row["ad_group_ad"]
        group = row["ad_group"]
        gid = str(group["id"])
        slot = per_group.setdefault(
            gid,
            {"campaign": row["campaign"]["name"], "ad_group": group.get("name"), "enabled_ads": 0},
        )
        if _enum(ad.get("status")) == "ENABLED":
            slot["enabled_ads"] += 1

        inner = ad.get("ad") or {}
        if (
            _enum(inner.get("type_") or inner.get("type")) == "RESPONSIVE_SEARCH_AD"
            and _enum(ad.get("status")) == "ENABLED"
        ):
            headlines = (inner.get("responsive_search_ad") or {}).get("headlines") or []
            if len(headlines) < _MIN_RSA_HEADLINES:
                thin.append(
                    {
                        "where": f"{row['campaign']['name']} / {group.get('name')}",
                        "ad_id": inner.get("id"),
                        "headlines": len(headlines),
                    }
                )

        summary = ad.get("policy_summary") or {}
        approval = _enum(summary.get("approval_status"))
        topics = [
            _enum(t.get("topic")) or t.get("topic")
            for t in (summary.get("policy_topic_entries") or [])
        ]
        where = f"{row['campaign']['name']} / {group.get('name')}"
        if approval == "DISAPPROVED":
            disapproved.append({"where": where, "ad_id": ad["ad"]["id"], "topics": topics})
        elif approval in ("APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY"):
            limited.append({"where": where, "status": approval, "topics": topics})

        # PENDING is not a verdict -- it is what every ad reads while Google
        # reviews it, which is exactly what an ad you just edited or created
        # looks like. Counting it as POOR made a restructure report itself as
        # 17 weak ads the moment the work was done.
        if _enum(ad.get("ad_strength")) == "POOR":
            poor.append(where)

    if disapproved:
        f.add(
            CRITICAL,
            "policy",
            f"{len(disapproved)} ad(s) are DISAPPROVED and not serving.",
            "A disapproved ad spends nothing and holds a slot. Read the "
            "policy topic, fix the cause, and resubmit.",
            evidence=disapproved,
        )
    if limited:
        f.add(
            WARNING,
            "policy",
            f"{len(limited)} ad(s) serve with restrictions.",
            "They run, but not everywhere or not for every query. Worth "
            "clearing before scaling spend behind them.",
            evidence=limited[:8],
        )

    single = [g for g in per_group.values() if g["enabled_ads"] == 1]
    empty = [g for g in per_group.values() if g["enabled_ads"] == 0]
    if empty:
        f.add(
            CRITICAL,
            "structure",
            f"{len(empty)} live ad group(s) have no enabled ad.",
            "The ad group cannot serve. Either add an ad or pause the group.",
            evidence=empty[:8],
        )
    if single:
        f.add(
            WARNING,
            "testing",
            f"{len(single)} ad group(s) run a single ad.",
            "With one ad there is no test and nothing to learn. Two "
            "responsive search ads per group is the floor.",
            evidence=[g["ad_group"] for g in single][:10],
        )
    if poor:
        f.add(
            NOTE,
            "creative",
            f"{len(poor)} ad(s) rate POOR on ad strength.",
            "Ad strength measures completeness, not performance -- but "
            "POOR usually means missing headlines or repeated text, and "
            "that does cost reach.",
            evidence=poor[:8],
        )
    if thin:
        f.add(
            NOTE,
            "creative",
            f"{len(thin)} responsive search ad(s) have fewer than "
            f"{_MIN_RSA_HEADLINES} headlines.",
            "Google assembles the ad from the headlines it is given; with "
            "few of them it has little to combine and fewer auctions to "
            "enter. Fill them with update_ad_text (practice, not a Google "
            "rule).",
            evidence=thin[:10],
        )


def _check_negatives(cid: str, f: _Findings) -> None:
    lists = _rows(
        cid,
        """
        SELECT shared_set.id, shared_set.name, shared_set.type,
               shared_set.member_count, shared_set.reference_count,
               shared_set.status
        FROM shared_set
        WHERE shared_set.status = 'ENABLED'
        """,
    )
    orphaned = [
        {"name": r["shared_set"].get("name"), "terms": r["shared_set"].get("member_count")}
        for r in lists
        if _enum(r["shared_set"].get("type")) == "NEGATIVE_KEYWORDS"
        and int(r["shared_set"].get("reference_count") or 0) == 0
    ]
    if orphaned:
        f.add(
            WARNING,
            "negatives",
            f"{len(orphaned)} negative keyword list(s) are attached to no campaign.",
            "The list exists and blocks nothing. Attach it or delete it -- "
            "an orphaned list is worse than none, because it looks like "
            "protection.",
            evidence=orphaned,
        )

    attached = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name
        FROM campaign_shared_set
        WHERE campaign.status = 'ENABLED'
        """,
    )
    have = {r["campaign"]["id"] for r in attached}
    search_campaigns = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND campaign.advertising_channel_type = 'SEARCH'
        """,
    )
    missing = [
        r["campaign"]["name"]
        for r in search_campaigns
        if r["campaign"]["id"] not in have
    ]
    if missing:
        f.add(
            WARNING,
            "negatives",
            f"{len(missing)} search campaign(s) have no shared negative list.",
            "Every campaign gets the account's master list, or the same "
            "junk gets paid for once per campaign.",
            evidence=missing,
        )

    # --- one-word BROAD negatives in shared lists --------------------------
    shared_terms = _rows(
        cid,
        """
        SELECT shared_set.id, shared_set.name,
               shared_criterion.keyword.text,
               shared_criterion.keyword.match_type
        FROM shared_criterion
        WHERE shared_set.status = 'ENABLED'
          AND shared_set.type = 'NEGATIVE_KEYWORDS'
        """,
    )
    one_word_broad = []
    terms_by_set: Dict[str, List[tuple]] = {}
    for r in shared_terms:
        kw = (r.get("shared_criterion") or {}).get("keyword") or {}
        text = str(kw.get("text") or "").strip()
        match = _enum(kw.get("match_type")) or ""
        if not text:
            continue
        terms_by_set.setdefault(str(r["shared_set"]["id"]), []).append((text, match))
        if match == "BROAD" and len(text.split()) == 1:
            one_word_broad.append({"list": r["shared_set"].get("name"), "term": text})
    if one_word_broad:
        f.add(
            NOTE,
            "negatives",
            f"{len(one_word_broad)} one-word BROAD negative(s) in shared lists.",
            "A one-word broad negative blocks every query containing that "
            "word, in any phrase. Right for 'free' or 'jobs'; wrong for a "
            "word the account's own keywords use. Read them.",
            evidence=one_word_broad[:15],
        )

    # --- negatives that block the account's own keywords -------------------
    attached_sets = _rows(
        cid,
        """
        SELECT campaign.id, shared_set.id, shared_set.type
        FROM campaign_shared_set
        WHERE campaign_shared_set.status != 'REMOVED'
          AND shared_set.type = 'NEGATIVE_KEYWORDS'
        """,
    )
    negatives: Dict[str, List[tuple]] = {}
    for r in attached_sets:
        campaign_id = str(r["campaign"]["id"])
        for term in terms_by_set.get(str(r["shared_set"]["id"]), []):
            negatives.setdefault(campaign_id, []).append(term + ("shared list",))
    for r in _rows(
        cid,
        """
        SELECT campaign.id, campaign_criterion.keyword.text,
               campaign_criterion.keyword.match_type,
               campaign_criterion.negative
        FROM campaign_criterion
        WHERE campaign_criterion.type = 'KEYWORD'
          AND campaign_criterion.negative = true
          AND campaign.status = 'ENABLED'
        """,
    ):
        kw = (r.get("campaign_criterion") or {}).get("keyword") or {}
        if kw.get("text"):
            negatives.setdefault(str(r["campaign"]["id"]), []).append(
                (str(kw["text"]), _enum(kw.get("match_type")) or "", "campaign")
            )
    group_negatives: Dict[str, List[tuple]] = {}
    for r in _rows(
        cid,
        """
        SELECT campaign.id, ad_group.id, ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               ad_group_criterion.negative
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.negative = true
          AND campaign.status = 'ENABLED'
        """,
    ):
        kw = (r.get("ad_group_criterion") or {}).get("keyword") or {}
        if kw.get("text"):
            group_negatives.setdefault(str(r["ad_group"]["id"]), []).append(
                (str(kw["text"]), _enum(kw.get("match_type")) or "", "ad group")
            )
    positives = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name, ad_group.id,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.negative = false
          AND ad_group_criterion.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND campaign.status = 'ENABLED'
        """,
    )
    clashes = []
    for r in positives:
        kw = (r.get("ad_group_criterion") or {}).get("keyword") or {}
        text = str(kw.get("text") or "").strip()
        if not text:
            continue
        campaign_id = str(r["campaign"]["id"])
        candidates = negatives.get(campaign_id, []) + group_negatives.get(
            str(r["ad_group"]["id"]), []
        )
        for neg_text, neg_match, where in candidates:
            if _negative_blocks(neg_text, neg_match, text):
                clashes.append(
                    {
                        "campaign": r["campaign"].get("name"),
                        "keyword": text,
                        "negative": neg_text,
                        "negative_match": neg_match,
                        "negative_level": where,
                    }
                )
    if clashes:
        f.add(
            WARNING,
            "negatives",
            f"{len(clashes)} keyword(s) are blocked by the account's own negatives.",
            "The keyword is bought and then the negative throws the query "
            "away, so the money buys nothing. Remove the negative "
            "(shared_sets_remove_terms / remove_negative_keywords) or the "
            "keyword.",
            evidence=clashes[:15],
        )


def _negative_blocks(negative: str, match: str, keyword: str) -> bool:
    """Whether a negative keyword would stop a query equal to ``keyword``.

    The rules are Google's, on the keyword's own text as the query:
    BROAD blocks when every word of the negative appears; PHRASE when the
    negative's words appear in order; EXACT when the texts are the same.
    """
    neg = negative.casefold().split()
    kw = keyword.casefold().split()
    if not neg or not kw:
        return False
    if match == "EXACT":
        return neg == kw
    if match == "PHRASE":
        return any(kw[i : i + len(neg)] == neg for i in range(len(kw) - len(neg) + 1))
    return all(word in kw for word in neg)


def _check_ad_group_spend(cid: str, f: _Findings, min_cost: float) -> None:
    """One ad group eating most of a campaign's money and converting nothing."""
    rows = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name, ad_group.id, ad_group.name,
               metrics.cost_micros, metrics.conversions
        FROM ad_group
        WHERE segments.date DURING LAST_30_DAYS
          AND campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
        """,
    )
    by_campaign: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        m = r.get("metrics", {})
        slot = by_campaign.setdefault(
            str(r["campaign"]["id"]),
            {"name": r["campaign"].get("name"), "cost": 0.0, "groups": []},
        )
        cost = _from_micros(m.get("cost_micros"))
        slot["cost"] += cost
        slot["groups"].append(
            {
                "ad_group": r["ad_group"].get("name"),
                "cost": cost,
                "conversions": float(m.get("conversions") or 0),
            }
        )
    hogs = []
    for campaign in by_campaign.values():
        if campaign["cost"] <= 0:
            continue
        for group in campaign["groups"]:
            share = group["cost"] / campaign["cost"]
            if (
                share > _AD_GROUP_SPEND_SHARE
                and group["conversions"] == 0
                and group["cost"] >= min_cost
            ):
                hogs.append(
                    {
                        "campaign": campaign["name"],
                        "ad_group": group["ad_group"],
                        "cost_30d": round(group["cost"], 2),
                        "share_of_campaign": round(share * 100, 1),
                        "ad_groups_in_campaign": len(campaign["groups"]),
                    }
                )
    if hogs:
        f.add(
            WARNING,
            "structure",
            f"{len(hogs)} ad group(s) take over {int(_AD_GROUP_SPEND_SHARE * 100)}% "
            "of their campaign's spend with no conversion.",
            "Most of the campaign's money goes to a group that has not "
            "produced a primary conversion in 30 days. Read its search "
            "terms; then lower its bids, narrow its keywords or pause it "
            "(practice, not a Google rule).",
            evidence=hogs[:10],
        )


def _check_call_extensions(cid: str, f: _Findings) -> None:
    """A call button whose calls are counted nowhere."""
    rows = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name, asset.id,
               asset.call_asset.call_conversion_reporting_state,
               asset.call_asset.call_conversion_action,
               campaign_asset.field_type
        FROM campaign_asset
        WHERE campaign_asset.field_type = 'CALL'
          AND campaign_asset.status != 'REMOVED'
        """,
    )
    broken = []
    for r in rows:
        call = (r.get("asset") or {}).get("call_asset") or {}
        state = _enum(call.get("call_conversion_reporting_state"))
        if state == "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION" and not call.get(
            "call_conversion_action"
        ):
            broken.append(
                {
                    "campaign": r["campaign"].get("name"),
                    "asset_id": (r.get("asset") or {}).get("id"),
                }
            )
    if broken:
        f.add(
            WARNING,
            "measurement",
            f"{len(broken)} call extension(s) count calls into no conversion action.",
            "The reporting state says 'use my own action' and none is set: "
            "the button shows, people call, nothing is counted. Repoint "
            "with update_call_extension(use_account_default=True).",
            evidence=broken,
        )


def _check_unlogged_changes(cid: str, f: _Findings) -> None:
    """Changes made in the interface that the decision log does not explain."""
    from ads_mcp.guardrails import audit_log

    today = datetime.date.today()
    since = today - datetime.timedelta(days=_UNLOGGED_WINDOW_DAYS)
    until = today + datetime.timedelta(days=1)
    rows = _rows(
        cid,
        f"""
        SELECT change_event.change_date_time,
               change_event.change_resource_type,
               change_event.resource_change_operation,
               change_event.client_type, change_event.user_email,
               change_event.campaign, change_event.changed_fields
        FROM change_event
        WHERE change_event.change_date_time >= '{since.isoformat()}'
          AND change_event.change_date_time <= '{until.isoformat()}'
          AND change_event.client_type = 'GOOGLE_ADS_WEB_CLIENT'
        ORDER BY change_event.change_date_time DESC
        LIMIT 500
        """,
    )
    if not rows:
        return

    logged = audit_log.read(
        cid,
        since=(since - datetime.timedelta(days=2)).isoformat(),
        until=until.isoformat(),
        limit=2000,
    )
    manual = [e for e in logged if e.get("source") == "manual"]

    def documented(when: datetime.datetime, resource_type: str) -> bool:
        for entry in manual:
            stamp = entry.get("time_utc") or ""
            try:
                logged_at = datetime.datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if logged_at.tzinfo is None:
                logged_at = logged_at.replace(tzinfo=datetime.timezone.utc)
            if abs((logged_at - when).total_seconds()) > 2 * 86400:
                continue
            wanted = (entry.get("arguments") or {}).get("resource_type")
            if not wanted or wanted == resource_type:
                return True
        return False

    undocumented: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        e = r["change_event"]
        stamp = str(e.get("change_date_time") or "")
        try:
            when = datetime.datetime.fromisoformat(stamp.replace(" ", "T"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        resource_type = _enum(e.get("change_resource_type")) or "UNKNOWN"
        if documented(when, resource_type):
            continue
        key = (
            stamp[:10],
            resource_type,
            _enum(e.get("resource_change_operation")),
            e.get("user_email"),
            (e.get("campaign") or "").rsplit("/", 1)[-1],
        )
        slot = undocumented.setdefault(
            key,
            {
                "day": key[0],
                "resource": key[1],
                "operation": key[2],
                "who": key[3],
                "campaign_id": key[4] or None,
                "fields": [],
                "count": 0,
            },
        )
        slot["count"] += 1
        # proto-plus renders a FieldMask as "a,b,c", not as a dict.
        changed = e.get("changed_fields") or ""
        paths = (
            changed.split(",") if isinstance(changed, str)
            else list(changed.get("paths") or [])
        )
        for path in [p for p in paths if p][:5]:
            if path not in slot["fields"]:
                slot["fields"].append(path)

    if undocumented:
        total = sum(v["count"] for v in undocumented.values())
        f.add(
            NOTE,
            "governance",
            f"{total} change(s) made in the interface in the last "
            f"{_UNLOGGED_WINDOW_DAYS} days have no entry in the decision log.",
            "Somebody changed the account by hand and nothing says why. Ask, "
            "then write it down with audit_log_manual_change so the next "
            "person does not undo it.",
            evidence=sorted(undocumented.values(), key=lambda v: v["day"], reverse=True)[:15],
        )


def _check_auto_apply(cid: str, f: _Findings) -> None:
    """Recommendations auto-apply deletes negative keywords behind your back.

    It cannot be read or turned off through the API, so the only evidence
    is the change log: removals whose client type is not a person.
    """
    today = datetime.date.today()
    since = (today - datetime.timedelta(days=29)).isoformat()
    until = (today + datetime.timedelta(days=1)).isoformat()
    rows = _rows(
        cid,
        f"""
        SELECT change_event.change_date_time, change_event.change_resource_type,
               change_event.resource_change_operation,
               change_event.client_type, change_event.user_email
        FROM change_event
        WHERE change_event.change_date_time >= '{since}'
          AND change_event.change_date_time <= '{until}'
          AND change_event.resource_change_operation = 'REMOVE'
        ORDER BY change_event.change_date_time DESC
        LIMIT 500
        """,
    )
    robotic: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        e = row["change_event"]
        client = _enum(e.get("client_type")) or ""
        if "RECOMMENDATIONS" not in client and "AUTOMATED" not in client:
            continue
        key = (client, _enum(e.get("change_resource_type")))
        slot = robotic.setdefault(key, {"removals": 0, "last": ""})
        slot["removals"] += 1
        when = str(e.get("change_date_time") or "")[:10]
        if when > slot["last"]:
            slot["last"] = when

    if not robotic:
        return

    # The window is 30 days, so a setting switched off three weeks ago still
    # shows its old damage here. Saying "Google IS deleting things" when the
    # user already turned it off is the tool being wrong, loudly. Date the
    # last automated change and let it decide how alarmed to be.
    latest = max(v["last"] for v in robotic.values())
    days_quiet = (today - datetime.date.fromisoformat(latest)).days if latest else 0
    evidence = [
        {"client": k[0], "resource": k[1], "removals": v["removals"], "last_change": v["last"]}
        for k, v in sorted(robotic.items(), key=lambda kv: -kv[1]["removals"])
    ]

    if days_quiet <= 7:
        f.add(
            CRITICAL,
            "governance",
            f"Google is changing this account automatically -- last on {latest}.",
            "Turn off Recommendations auto-apply in the UI (it cannot be "
            "read or changed through the API). Auto-apply removing negative "
            "keywords hands your budget back to the traffic you excluded.",
            evidence=evidence,
        )
    else:
        f.add(
            NOTE,
            "governance",
            f"Automated changes in the window, but none since {latest} "
            f"({days_quiet} days ago).",
            "Consistent with auto-apply having been turned off. Worth "
            "checking what it removed while it was on -- deleted negatives "
            "do not come back on their own.",
            evidence=evidence,
        )


def _check_keyword_overlap(cid: str, f: _Findings) -> None:
    rows = _rows(
        cid,
        """
        SELECT campaign.name, ad_group.name,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type
        FROM keyword_view
        WHERE ad_group_criterion.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND campaign.status = 'ENABLED'
          AND ad_group_criterion.negative = false
        """,
    )
    seen: Dict[tuple, set] = {}
    for row in rows:
        kw = (row.get("ad_group_criterion") or {}).get("keyword") or {}
        text = (kw.get("text") or "").strip().lower()
        if not text:
            continue
        key = (text, _enum(kw.get("match_type")))
        seen.setdefault(key, set()).add(row["campaign"]["name"])

    clashes = [
        {"keyword": k[0], "match": k[1], "campaigns": sorted(v)}
        for k, v in seen.items()
        if len(v) > 1
    ]
    if clashes:
        f.add(
            WARNING,
            "structure",
            f"{len(clashes)} keyword(s) appear in more than one live campaign.",
            "The campaigns bid against each other for the same query and "
            "the winner is decided by ad rank, not by you. Keep each "
            "keyword in one campaign and negative it out of the others.",
            evidence=clashes[:10],
        )


def _check_wasted_spend(cid: str, f: _Findings, min_cost: float) -> None:
    rows = _rows(
        cid,
        """
        SELECT search_term_view.search_term, campaign.name,
               metrics.cost_micros, metrics.clicks, metrics.conversions
        FROM search_term_view
        WHERE segments.date DURING LAST_30_DAYS
          AND campaign.status = 'ENABLED'
        """,
    )
    burners = []
    total = 0.0
    for row in rows:
        m = row.get("metrics", {})
        if float(m.get("conversions") or 0) > 0:
            continue
        cost = _from_micros(m.get("cost_micros"))
        if cost < min_cost:
            continue
        total += cost
        burners.append(
            {
                "term": row["search_term_view"]["search_term"],
                "campaign": row["campaign"]["name"],
                "cost": cost,
                "clicks": int(m.get("clicks") or 0),
            }
        )
    if burners:
        burners.sort(key=lambda b: -b["cost"])
        f.add(
            WARNING,
            "negatives",
            f"{len(burners)} search term(s) spent {round(total, 2)} in 30 days "
            "with no conversion.",
            "Read them before excluding: a term with no conversion and real "
            "intent needs a better landing page, not a negative. The rest "
            "belong on the shared negative list.",
            evidence=burners[:15],
        )


# --------------------------------------------------------------------------
# The audit itself
# --------------------------------------------------------------------------


def _check_inherited_account_extensions(cid: str, f: _Findings) -> None:
    """Account-level sitelinks/callouts that live campaigns silently inherit.

    Found live (Oct 2026): two new campaigns launched without sitelinks of
    their own would have shown account-level sitelinks carrying forbidden
    claims and 301-redirected URLs. The campaign's own extension list looked
    clean; only the account level showed them.
    """
    links = _rows(
        cid,
        """
        SELECT customer_asset.field_type, customer_asset.status, asset.id,
               asset.sitelink_asset.link_text, asset.callout_asset.callout_text,
               asset.final_urls
        FROM customer_asset
        WHERE customer_asset.field_type IN ('SITELINK', 'CALLOUT')
          AND customer_asset.status = 'ENABLED'
        """,
    )
    if not links:
        return
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for r in links:
        ftype = _enum((r.get("customer_asset") or {}).get("field_type"))
        asset = r.get("asset") or {}
        text = ((asset.get("sitelink_asset") or {}).get("link_text")
                or (asset.get("callout_asset") or {}).get("callout_text"))
        by_type.setdefault(ftype, []).append(
            {"asset_id": asset.get("id"), "text": text,
             "final_urls": asset.get("final_urls") or None}
        )
    campaigns = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name, campaign.status
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND campaign.advertising_channel_type = 'SEARCH'
        """,
    )
    own = _rows(
        cid,
        """
        SELECT campaign.id, campaign_asset.field_type, campaign_asset.status
        FROM campaign_asset
        WHERE campaign_asset.field_type IN ('SITELINK', 'CALLOUT')
          AND campaign_asset.status = 'ENABLED'
        """,
    )
    has_own = {
        (str((r.get("campaign") or {}).get("id")),
         _enum((r.get("campaign_asset") or {}).get("field_type")))
        for r in own
    }
    for ftype, assets in sorted(by_type.items()):
        inheriting = [
            c["campaign"].get("name") for c in campaigns
            if (str(c["campaign"].get("id")), ftype) not in has_own
        ]
        if not inheriting:
            continue
        f.add(
            WARNING,
            "extensions",
            f"{len(inheriting)} live search campaign(s) inherit "
            f"{len(assets)} account-level {ftype.lower()}(s).",
            "A campaign with no extension of this type of its own shows the "
            "account-level ones, and its own extension list looks empty. "
            "Read them with list_account_extensions: give the campaign its "
            "own, or unlink stale ones with remove_account_extension.",
            evidence={"campaigns": inheriting, "account_assets": assets},
        )


def _audit_account(
    customer_id: str,
    min_wasted_cost: float = 50.0,
    skip_search_terms: bool = False,
    brand_campaign_ids: Optional[List[str]] = None,
    lead_contract: Optional[str] = None,
) -> Dict[str, Any]:
    """The audit itself, callable from Python.

    run_portfolio_audit runs this directly rather than reaching through
    the tool wrapper, so the two entry points can never drift apart.

    Same checks, same order, every account -- so "we found this in one
    account" becomes "we checked every account". Findings come back
    ranked: critical means something is not working or not serving,
    warning means money or reach is leaking, note means look and decide.

    A check that fails to run is reported in `checks_failed` rather than
    failing the audit, because a partial audit is worth more than none.

    Args:
        customer_id: The Google Ads account id.
        min_wasted_cost: Only report zero-conversion search terms that
            spent at least this much in the last 30 days, in account
            currency. Lower it on small accounts.
        skip_search_terms: Skip the search-term sweep, which is the
            slowest check on a large account.

    Returns:
        The account header, the live campaigns, and the ranked findings.
    """
    cid = _clean_id(customer_id)
    f = _Findings()
    failed: List[Dict[str, str]] = []

    def run(name, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - a failed check must not end the audit
            failed.append({"check": name, "error": str(exc)[:200]})
            return None

    header = _check_account_settings(cid, f)  # fatal on purpose: no account, no audit
    conversions = run("conversions", _check_conversions, cid, f, lead_contract) or {}
    campaigns = run("campaigns", _check_campaigns, cid, f, brand_campaign_ids) or {}
    run("ads", _check_ads, cid, f)
    run("bid_strategies", _check_bid_strategies, cid, f)
    run("negatives", _check_negatives, cid, f)
    run("ad_group_spend", _check_ad_group_spend, cid, f, min_wasted_cost)
    run("call_extensions", _check_call_extensions, cid, f)
    run("account_extensions", _check_inherited_account_extensions, cid, f)
    run("auto_apply", _check_auto_apply, cid, f)
    run("unlogged_changes", _check_unlogged_changes, cid, f)
    run("keyword_overlap", _check_keyword_overlap, cid, f)
    if not skip_search_terms:
        run("wasted_spend", _check_wasted_spend, cid, f, min_wasted_cost)

    findings = f.sorted()
    counts = {
        level: sum(1 for x in findings if x["severity"] == level)
        for level in (CRITICAL, WARNING, NOTE)
    }

    if counts[CRITICAL]:
        verdict = (
            f"{counts[CRITICAL]} critical finding(s): something in this "
            "account is not working or not serving. Fix these before "
            "anything else."
        )
    elif counts[WARNING]:
        verdict = (
            f"Nothing is broken. {counts[WARNING]} warning(s) are leaking "
            "money or reach."
        )
    elif findings:
        verdict = "Nothing broken and nothing leaking; the notes are judgement calls."
    else:
        verdict = "Every check passed."

    result = {
        "account": header,
        "measurement": conversions,
        "campaigns": campaigns.get("live_campaigns", []),
        "counts": counts,
        "findings": findings,
        "verdict": verdict,
    }
    if failed:
        result["checks_failed"] = failed
    return result


@audit_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def run_account_audit(
    customer_id: str,
    min_wasted_cost: float = 50.0,
    skip_search_terms: bool = False,
    brand_campaign_ids: Optional[List[str]] = None,
    lead_contract: Optional[str] = None,
) -> Dict[str, Any]:
    """Runs the full agency checklist against one account.

    Same checks, same order, every account -- so "we found this in one
    account" becomes "we checked every account". Findings come back
    ranked: critical means something is not working or not serving,
    warning means money or reach is leaking, note means look and decide.

    A check that fails to run is reported in `checks_failed` rather than
    failing the audit, because a partial audit is worth more than none.

    Args:
        customer_id: The Google Ads account id.
        min_wasted_cost: Only report zero-conversion search terms, and
            ad groups, that spent at least this much in the last 30 days,
            in account currency. Lower it on small accounts.
        skip_search_terms: Skip the search-term sweep, which is the
            slowest check on a large account.
        brand_campaign_ids: Campaigns to leave out of the
            PRESENCE_OR_INTEREST check -- a brand campaign may legitimately
            want people interested in the area from elsewhere.
        lead_contract: What the client pays for: FORM_ONLY, CALLS_ONLY or
            FORMS_AND_CALLS. With FORM_ONLY, biddable call goals are
            reported; with CALLS_ONLY, biddable form goals are. Omit to
            skip that check.

    Returns:
        The account header, the live campaigns, and the ranked findings.
    """
    return _audit_account(
        customer_id,
        min_wasted_cost=min_wasted_cost,
        skip_search_terms=skip_search_terms,
        brand_campaign_ids=brand_campaign_ids,
        lead_contract=lead_contract,
    )

@audit_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def run_portfolio_audit(
    manager_customer_id: str,
    min_wasted_cost: float = 50.0,
    skip_search_terms: bool = True,
    lead_contracts: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Runs the account audit across every account under a manager.

    This is the answer to "we fixed that in one account -- is it wrong
    in the others too". It runs the same checklist everywhere and then
    inverts the result: `shared_findings` lists each finding by how many
    accounts have it, so a problem that shows up in three accounts reads
    as one job rather than three separate discoveries.

    Search terms are skipped by default here; they are the slow check and
    the per-account audit is the right place to read them.

    An account that cannot be read is listed in `unreachable` rather than
    failing the run -- a deactivated or unlinked account is normal.

    Args:
        manager_customer_id: The manager (MCC) account id.
        min_wasted_cost: Passed through to each account audit.
        skip_search_terms: Passed through to each account audit.
        lead_contracts: Optional map of customer id to FORM_ONLY,
            CALLS_ONLY or FORMS_AND_CALLS, for the contract check.

    Returns:
        Per-account counts and verdicts, plus findings grouped across
        accounts, worst first.
    """
    mcc = _clean_id(manager_customer_id)
    children = _rows(
        mcc,
        """
        SELECT customer_client.id, customer_client.descriptive_name,
               customer_client.manager, customer_client.status
        FROM customer_client
        WHERE customer_client.manager = false
          AND customer_client.status = 'ENABLED'
        """,
    )

    accounts: List[Dict[str, Any]] = []
    unreachable: List[Dict[str, str]] = []
    grouped: Dict[tuple, Dict[str, Any]] = {}

    for row in children:
        c = row["customer_client"]
        cid = str(c["id"])
        name = c.get("descriptive_name") or cid
        contract = None
        for key, value in (lead_contracts or {}).items():
            if _clean_id(key) == cid:
                contract = value
        try:
            audit = _audit_account(
                cid,
                min_wasted_cost=min_wasted_cost,
                skip_search_terms=skip_search_terms,
                lead_contract=contract,
            )
        except Exception as exc:  # noqa: BLE001
            unreachable.append({"customer_id": cid, "name": name, "error": str(exc)[:200]})
            continue

        entry = {
            "customer_id": cid,
            "name": name,
            "counts": audit["counts"],
            "verdict": audit["verdict"],
        }
        # A silently dropped check is worse than a failed one: it makes a
        # broken audit look clean. Carry it up.
        if audit.get("checks_failed"):
            entry["checks_failed"] = audit["checks_failed"]
        accounts.append(entry)
        for finding in audit["findings"]:
            key = (finding["severity"], finding["area"], finding["fix"])
            slot = grouped.setdefault(
                key,
                {
                    "severity": finding["severity"],
                    "area": finding["area"],
                    "fix": finding["fix"],
                    "accounts": [],
                },
            )
            slot["accounts"].append({"name": name, "finding": finding["finding"]})

    shared = sorted(
        grouped.values(),
        key=lambda g: (_ORDER[g["severity"]], -len(g["accounts"])),
    )
    for g in shared:
        g["account_count"] = len(g["accounts"])

    return {
        "manager_customer_id": mcc,
        "accounts_audited": len(accounts),
        "accounts": sorted(accounts, key=lambda a: -a["counts"][CRITICAL]),
        "shared_findings": shared,
        "unreachable": unreachable,
    }


# --------------------------------------------------------------------------
# The change log this server writes
#
# Google's change_event says what changed in the account. The audit log
# says what THIS server changed, why (the plan), and who asked. Reading it
# is how a briefing or a QA pass separates "we did this on purpose" from
# "somebody did this in the interface".
# --------------------------------------------------------------------------


@audit_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_change_log(
    customer_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Reads what this server wrote to an account, newest first.

    One entry per write attempt: applied writes, validations, refusals at
    the precondition, API failures, and decisions logged by hand with
    log_manual_change. Each carries the time in UTC and in Europe/Istanbul,
    the tool and its class, the plan id, the operations sent and who asked.

    Args:
        customer_id: The Google Ads account id.
        since: Start of the window, "YYYY-MM-DD" or an ISO timestamp.
            Defaults to 30 days ago.
        until: End of the window. Defaults to now.
        tool: Limit to one tool, e.g. "campaigns.update_campaign_budget".
        limit: Maximum entries, newest first.

    Returns:
        `entries`, the window, and counts by result and by tool.
    """
    from ads_mcp.guardrails import audit_log

    entries = [
        audit_log.summarize(e)
        for e in audit_log.read(
            _clean_id(customer_id), since=since, until=until, tool=tool,
            limit=limit,
        )
    ]
    by_result: Dict[str, int] = {}
    by_tool: Dict[str, int] = {}
    for entry in entries:
        by_result[entry["result"] or "unknown"] = (
            by_result.get(entry["result"] or "unknown", 0) + 1
        )
        by_tool[entry["tool"] or "unknown"] = (
            by_tool.get(entry["tool"] or "unknown", 0) + 1
        )
    return {
        "customer_id": _clean_id(customer_id),
        "since": since or "30 days ago",
        "until": until or "now",
        "log_directory": str(audit_log.audit_dir()),
        "entries": entries,
        "by_result": by_result,
        "by_tool": by_tool,
    }


@audit_mcp.tool
def log_manual_change(
    customer_id: str,
    description: str,
    resource_type: Optional[str] = None,
    campaign_id: Optional[str] = None,
    when: Optional[str] = None,
) -> Dict[str, Any]:
    """Writes down a change made in the Google Ads interface, and why.

    The audit checks compare Google's change history with this log: a
    change made in the interface with no entry here is reported as an
    undocumented change. Logging it closes that gap. Nothing in the account
    is touched.

    Args:
        customer_id: The Google Ads account id.
        description: What was changed and why, in one or two sentences.
        resource_type: What kind of thing, as Google names it in the change
            history: CAMPAIGN, AD_GROUP, AD_GROUP_CRITERION, CAMPAIGN_BUDGET,
            CAMPAIGN_CRITERION, AD_GROUP_AD, ASSET ...
        campaign_id: The campaign it concerns, if one.
        when: When it was done, "YYYY-MM-DD" or an ISO timestamp. Defaults
            to now.

    Returns:
        The entry as written, with both timestamps.
    """
    from ads_mcp.guardrails import audit_log, guard

    description = str(description or "").strip()
    if not description:
        raise ValueError("Say what was changed and why.")
    return audit_log.record_manual(
        _clean_id(customer_id),
        description,
        resource_type=str(resource_type).upper() if resource_type else None,
        campaign_id=_clean_id(campaign_id) if campaign_id else None,
        when=when,
        actor=guard.current_actor(),
    )
