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
  * one ad per ad group everywhere, so nothing was ever being tested.

Read-only. It changes nothing and it asks for no confirmation; it is
meant to be safe to run on any account at any time.
"""

from typing import Any, Dict, List, Optional
import datetime

import proto
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

audit_mcp = FastMCP("audit")

_MICROS = 1_000_000

# Impression share comes back as these two sentinels rather than a number
# when Google will not disclose the exact figure.
_IS_FLOOR = 0.0999
_IS_CEILING = 0.9001

CRITICAL = "critical"
WARNING = "warning"
NOTE = "note"

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


def _check_conversions(cid: str, f: _Findings) -> Dict[str, Any]:
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
               metrics.conversions, metrics.all_conversions
        FROM campaign
        WHERE segments.date DURING LAST_30_DAYS
          AND metrics.all_conversions > 0
        """,
    )

    totals: Dict[str, Dict[str, Any]] = {}
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

    return {
        "biddable_goals": sorted(
            f"{c}/{o}" for (c, o), b in goals.items() if b
        ),
        "conversions_counted_30d": round(counted, 1),
    }


def _check_campaigns(cid: str, f: _Findings) -> Dict[str, Any]:
    rows = _rows(
        cid,
        """
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type,
               campaign.bidding_strategy_type,
               campaign.geo_target_type_setting.positive_geo_target_type,
               campaign_budget.amount_micros,
               campaign_budget.id,
               metrics.cost_micros, metrics.clicks, metrics.conversions,
               metrics.search_budget_lost_impression_share,
               metrics.search_impression_share
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND segments.date DURING LAST_30_DAYS
        """,
    )

    interest = []
    starved = []
    thin_is = []
    budget_ids: Dict[str, List[str]] = {}
    live: List[Dict[str, Any]] = []

    for row in rows:
        c = row["campaign"]
        m = row.get("metrics", {})
        b = row.get("campaign_budget", {})
        name = c.get("name")
        cost = _from_micros(m.get("cost_micros"))
        live.append(
            {
                "id": c["id"],
                "name": name,
                "channel": _enum(c.get("advertising_channel_type")),
                "bidding": _enum(c.get("bidding_strategy_type")),
                "cost_30d": cost,
                "conversions_30d": round(float(m.get("conversions") or 0), 1),
            }
        )

        geo = _enum(
            (c.get("geo_target_type_setting") or {}).get("positive_geo_target_type")
        )
        if geo == "PRESENCE_OR_INTEREST":
            interest.append(name)

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

    return {"live_campaigns": live}


def _check_ads(cid: str, f: _Findings) -> None:
    rows = _rows(
        cid,
        """
        SELECT campaign.name, ad_group.id, ad_group.name, ad_group.status,
               ad_group_ad.ad.id, ad_group_ad.status,
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

        if _enum(ad.get("ad_strength")) in ("POOR", "PENDING"):
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
    robotic = {}
    for row in rows:
        e = row["change_event"]
        client = _enum(e.get("client_type")) or ""
        if "RECOMMENDATIONS" not in client and "AUTOMATED" not in client:
            continue
        key = (client, _enum(e.get("change_resource_type")))
        robotic[key] = robotic.get(key, 0) + 1

    if robotic:
        f.add(
            CRITICAL,
            "governance",
            "Google is deleting things in this account automatically.",
            "Turn off Recommendations auto-apply in the UI (it cannot be "
            "read or changed through the API). Auto-apply removing negative "
            "keywords hands your budget back to the traffic you excluded.",
            evidence=[
                {"client": k[0], "resource": k[1], "removals_30d": v}
                for k, v in sorted(robotic.items(), key=lambda kv: -kv[1])
            ],
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


def _audit_account(
    customer_id: str,
    min_wasted_cost: float = 50.0,
    skip_search_terms: bool = False,
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
    conversions = run("conversions", _check_conversions, cid, f) or {}
    campaigns = run("campaigns", _check_campaigns, cid, f) or {}
    run("ads", _check_ads, cid, f)
    run("negatives", _check_negatives, cid, f)
    run("auto_apply", _check_auto_apply, cid, f)
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
        min_wasted_cost: Only report zero-conversion search terms that
            spent at least this much in the last 30 days, in account
            currency. Lower it on small accounts.
        skip_search_terms: Skip the search-term sweep, which is the
            slowest check on a large account.

    Returns:
        The account header, the live campaigns, and the ranked findings.
    """
    return _audit_account(
        customer_id,
        min_wasted_cost=min_wasted_cost,
        skip_search_terms=skip_search_terms,
    )

@audit_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def run_portfolio_audit(
    manager_customer_id: str,
    min_wasted_cost: float = 50.0,
    skip_search_terms: bool = True,
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
        try:
            audit = _audit_account(
                cid,
                min_wasted_cost=min_wasted_cost,
                skip_search_terms=skip_search_terms,
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
