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

"""Portfolio (shared) bidding strategies: read them, and move campaigns.

Why this module exists
----------------------

A campaign's bidding lives in one of two places, and until this module
existed the server could only see and write one of them:

  * on the campaign itself (``campaign.target_spend``, and friends), which
    is what ``campaigns.update_bid_strategy`` writes; or
  * on a PORTFOLIO strategy -- a ``bidding_strategy`` resource shared by
    several campaigns, pointed at by ``campaign.bidding_strategy``.

A campaign on a portfolio strategy reads back with a
``bidding_strategy_type`` like any other, and its own
``campaign.target_spend.cpc_bid_ceiling_micros`` reads 0 -- because the
ceiling is on the strategy, not on the campaign. Nothing in the output
says "the setting you are looking at is somewhere else". So
``campaigns.update_bid_strategy`` was called on such a campaign, built a
perfectly good campaign-level operation, and Google refused it:

    bidding_error: BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED
    on operations[0].update.bidding_strategy

The field named in that error was one the operation never set. What
Google was saying is this: the portfolio strategy is ALIGNED to a shared
campaign budget (``bidding_strategy.aligned_campaign_budget_id``), and
Google keeps the two together -- every campaign on that strategy uses
that budget, and every campaign on that budget uses that strategy. A
campaign cannot leave one without leaving the other, so clearing
``campaign.bidding_strategy`` on its own is not a legal operation.

So there are two legal ways out, and they affect different numbers of
campaigns. This module does not pick one: it reads the account, names
both, and makes the caller choose (see
``detach_campaign_from_portfolio``).

What is NOT here
----------------

Nothing in this module creates or removes a portfolio strategy. Creating
one is a structural decision about an account that belongs in the
interface, where the advertiser can see what it will swallow; and a
strategy with campaigns on it cannot be removed anyway. ``attach`` and
``detach`` are a pair, so every write here has its undo.
"""

from typing import Any, Dict, List, Optional

import proto

from fastmcp import FastMCP

import ads_mcp.utils as utils
from ads_mcp.tools import _bidding

bidding_strategies_mcp = FastMCP("bidding_strategies")

_MICROS = 1_000_000

# Every scheme's settings, selected in one go: a field that belongs to a
# scheme the strategy does not use simply reads back as its default, and
# selecting them all keeps the query a constant -- which matters, because a
# plan's reads are compared with the reads of the apply run and a query
# that changes shape between the two invalidates the plan.
_STRATEGY_FIELDS = """
        SELECT bidding_strategy.id,
               bidding_strategy.name,
               bidding_strategy.status,
               bidding_strategy.type,
               bidding_strategy.currency_code,
               bidding_strategy.effective_currency_code,
               bidding_strategy.aligned_campaign_budget_id,
               bidding_strategy.campaign_count,
               bidding_strategy.non_removed_campaign_count,
               bidding_strategy.target_spend.target_spend_micros,
               bidding_strategy.target_spend.cpc_bid_ceiling_micros,
               bidding_strategy.target_impression_share.location,
               bidding_strategy.target_impression_share.location_fraction_micros,
               bidding_strategy.target_impression_share.cpc_bid_ceiling_micros,
               bidding_strategy.maximize_conversions.target_cpa_micros,
               bidding_strategy.maximize_conversions.cpc_bid_ceiling_micros,
               bidding_strategy.maximize_conversions.cpc_bid_floor_micros,
               bidding_strategy.maximize_conversion_value.target_roas,
               bidding_strategy.maximize_conversion_value.cpc_bid_ceiling_micros,
               bidding_strategy.maximize_conversion_value.cpc_bid_floor_micros,
               bidding_strategy.target_cpa.target_cpa_micros,
               bidding_strategy.target_cpa.cpc_bid_ceiling_micros,
               bidding_strategy.target_cpa.cpc_bid_floor_micros,
               bidding_strategy.target_roas.target_roas,
               bidding_strategy.target_roas.cpc_bid_ceiling_micros,
               bidding_strategy.target_roas.cpc_bid_floor_micros
        FROM bidding_strategy
"""

# Campaigns are read whole and matched to their strategy in Python rather
# than filtered on campaign.bidding_strategy in GAQL: the resource-name
# filter is the kind of thing that works until it does not, and a campaign
# list is dozens of rows, not thousands.
_CAMPAIGN_FIELDS = """
        SELECT campaign.id,
               campaign.name,
               campaign.status,
               campaign.advertising_channel_type,
               campaign.bidding_strategy,
               campaign.bidding_strategy_type,
               campaign.bidding_strategy_system_status,
               campaign_budget.id,
               campaign_budget.name,
               campaign_budget.amount_micros,
               campaign_budget.explicitly_shared
        FROM campaign
        WHERE campaign.status != 'REMOVED'
"""


class SpendConfirmationRequired(Exception):
    """Raised when an action would let money be spent without confirmation."""


class PortfolioAlignmentRequired(Exception):
    """The caller has to say which of the two legal routes to take."""


def _clean_id(value: Any) -> str:
    return _bidding.clean_id(value)


def _rows(customer_id: str, query: str) -> List[Dict[str, Any]]:
    """Runs a GAQL query and returns plain dict rows with enum NAMES."""
    ga_service = utils.get_googleads_service("GoogleAdsService")
    out: List[Dict[str, Any]] = []
    for batch in ga_service.search_stream(
        customer_id=customer_id, query=query
    ):
        for row in batch.results:
            out.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return out


def _strategy_rows(customer_id: str) -> List[Dict[str, Any]]:
    return _rows(
        customer_id,
        _STRATEGY_FIELDS + "        WHERE bidding_strategy.status != 'REMOVED'",
    )


def _campaign_rows(customer_id: str) -> List[Dict[str, Any]]:
    return _rows(customer_id, _CAMPAIGN_FIELDS)


def _dig(section: Dict[str, Any], path: str) -> Any:
    """Reads a dotted path out of a nested dict, or None."""
    node: Any = section
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _settings_of(strategy: Dict[str, Any], strategy_type: str) -> Dict[str, Any]:
    """Only the scheme fields this strategy's own type carries.

    proto-plus returns every unset sub-field as 0 or "", so reading the
    whole message back gives fifteen empty fields for the one that is set.
    """
    out: Dict[str, Any] = {}
    for path in _bidding.PORTFOLIO_SETTING_PATHS.get(strategy_type, []):
        value = _dig(strategy, path)
        if path.endswith("_micros") and not path.endswith(
            "location_fraction_micros"
        ):
            out[path.rsplit(".", 1)[-1][: -len("_micros")]] = (
                _bidding.from_micros(value)
            )
        elif path.endswith("location_fraction_micros"):
            out["target_share_percent"] = round(
                int(value or 0) / 10_000, 1
            )
        elif path.endswith(".location"):
            out["location"] = _bidding.enum_name(value)
        else:
            out[path.rsplit(".", 1)[-1]] = value
    return out


def _campaign_line(row: Dict[str, Any]) -> Dict[str, Any]:
    campaign = row.get("campaign") or {}
    budget = row.get("campaign_budget") or {}
    system_status = _bidding.enum_name(
        campaign.get("bidding_strategy_system_status")
    )
    line = {
        "campaign_id": str(campaign.get("id") or ""),
        "name": campaign.get("name"),
        "status": _bidding.enum_name(campaign.get("status")),
        "channel": _bidding.enum_name(
            campaign.get("advertising_channel_type")
        ),
        "bid_strategy_type": _bidding.enum_name(
            campaign.get("bidding_strategy_type")
        ),
        "system_status": system_status,
        "budget_id": str(budget.get("id") or ""),
        "daily_budget": _bidding.from_micros(budget.get("amount_micros")),
        "budget_is_shared": bool(budget.get("explicitly_shared")),
    }
    note = _bidding.describe_system_status(system_status)
    if note:
        line["system_status_means"] = note
    return line


def _strategy_of(row: Dict[str, Any]) -> str:
    return str((row.get("campaign") or {}).get("bidding_strategy") or "")


def _budget_amount(
    campaign_rows: List[Dict[str, Any]], budget_id: str
) -> Optional[float]:
    for row in campaign_rows:
        budget = row.get("campaign_budget") or {}
        if str(budget.get("id") or "") == str(budget_id):
            return _bidding.from_micros(budget.get("amount_micros"))
    return None


def _describe_strategy(
    row: Dict[str, Any], campaign_rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    strategy = row.get("bidding_strategy") or {}
    strategy_id = str(strategy.get("id") or "")
    strategy_type = _bidding.read_type(strategy)
    resource_name = strategy.get("resource_name") or ""
    mine = [
        _campaign_line(c)
        for c in campaign_rows
        if _strategy_of(c) == resource_name
    ]
    settings = _settings_of(strategy, strategy_type)
    aligned = int(strategy.get("aligned_campaign_budget_id") or 0)
    ceiling_path = _bidding.PORTFOLIO_CEILING_PATHS.get(strategy_type)
    ceiling = (
        _bidding.from_micros(_dig(strategy, ceiling_path))
        if ceiling_path
        else None
    )

    notes: List[str] = []
    if ceiling_path is None:
        notes.append(
            f"{strategy_type} carries no CPC ceiling, so "
            f"update_portfolio_bid_ceiling does not apply to it."
        )
    elif not ceiling:
        notes.append(
            "No CPC ceiling on this strategy: it pays whatever a click "
            "costs in the auction. Set one with "
            "update_portfolio_bid_ceiling."
        )
    if aligned:
        amount = _budget_amount(campaign_rows, str(aligned))
        notes.append(
            f"Aligned to shared campaign budget {aligned}"
            + (f" ({amount}/day in the account's currency)" if amount else "")
            + ". Google keeps the two together: every campaign on this "
            "strategy uses that budget and every campaign on that budget "
            "uses this strategy. A campaign cannot leave one without "
            "leaving the other -- clearing campaign.bidding_strategy on "
            "its own is refused with "
            "BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED. Use "
            "detach_campaign_from_portfolio, which handles both halves."
        )
    bad = [c for c in mine if c.get("system_status_means")]
    for line in bad:
        notes.append(
            f"'{line['name']}' ({line['status']}): "
            f"{line['system_status']} -- {line['system_status_means']}."
        )
    if strategy.get("campaign_count") is not None and len(mine) != int(
        strategy.get("campaign_count") or 0
    ):
        notes.append(
            f"Google reports campaign_count="
            f"{strategy.get('campaign_count')} but "
            f"{len(mine)} non-removed campaign(s) point at this strategy; "
            f"the difference is removed campaigns."
        )

    return {
        "bidding_strategy_id": strategy_id,
        "resource_name": resource_name,
        "name": strategy.get("name"),
        "type": strategy_type,
        "status": _bidding.enum_name(strategy.get("status")),
        "currency": strategy.get("effective_currency_code")
        or strategy.get("currency_code"),
        "cpc_bid_ceiling": ceiling,
        "settings": settings,
        "aligned_campaign_budget_id": str(aligned) if aligned else None,
        "campaign_count": strategy.get("campaign_count"),
        "non_removed_campaign_count": strategy.get(
            "non_removed_campaign_count"
        ),
        "campaigns": mine,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


@bidding_strategies_mcp.tool
def list_bidding_strategies(
    customer_id: str,
    include_campaign_level: bool = True,
) -> Dict[str, Any]:
    """Lists the account's portfolio (shared) bidding strategies.

    Read this BEFORE changing any campaign's bidding. A campaign that
    points at a portfolio strategy keeps its bidding settings on the
    strategy, not on itself, and campaigns.update_bid_strategy cannot
    write them -- the request is refused with
    BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED when the strategy is
    aligned to a shared budget. The list says which campaigns are in that
    position and which tool reaches their settings.

    It also reports ``bidding_strategy_system_status``, which Google fills
    in for free and almost nobody reads: MISCONFIGURED_CONVERSION_TYPES
    means a conversion-based strategy is optimising towards nothing, and
    LIMITED_BY_CPC_BID_CEILING means the ceiling, not the budget, is what
    is holding the campaign back.

    Args:
        customer_id: The Google Ads account id.
        include_campaign_level: Also list the campaigns that carry their
            own strategy, with any system status worth looking at.

    Returns:
        One block per portfolio strategy: its type, its ceiling and
        target, the aligned shared budget if there is one, the campaigns
        on it, and notes naming anything that needs a decision.
    """
    customer_id = _clean_id(customer_id)
    strategy_rows = _strategy_rows(customer_id)
    campaign_rows = _campaign_rows(customer_id)

    portfolios = [
        _describe_strategy(row, campaign_rows) for row in strategy_rows
    ]
    on_portfolio = {
        c["campaign_id"]
        for p in portfolios
        for c in p["campaigns"]
    }

    result: Dict[str, Any] = {
        "customer_id": customer_id,
        "portfolio_strategies": portfolios,
        "portfolio_count": len(portfolios),
    }

    if include_campaign_level:
        own = [
            _campaign_line(row)
            for row in campaign_rows
            if not _strategy_of(row)
        ]
        result["campaign_level_campaigns"] = own
        result["campaign_level_problems"] = [
            c for c in own if c.get("system_status_means")
        ]

    if not portfolios:
        result["summary"] = (
            "No portfolio bidding strategy in this account: every "
            "campaign's bidding is on the campaign, so "
            "campaigns.update_bid_strategy reaches all of it."
        )
    else:
        result["summary"] = (
            f"{len(portfolios)} portfolio strategy(ies) carry the bidding "
            f"of {len(on_portfolio)} campaign(s). Those campaigns' "
            f"settings are NOT on the campaign and "
            f"campaigns.update_bid_strategy will not write them."
        )
    return result


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def _one_strategy(customer_id: str, strategy_id: str) -> Dict[str, Any]:
    rows = _rows(
        customer_id,
        _STRATEGY_FIELDS
        + f"        WHERE bidding_strategy.id = {strategy_id}",
    )
    if not rows:
        raise ValueError(
            f"No bidding strategy {strategy_id} in account {customer_id}. "
            f"list_bidding_strategies shows the ones that exist."
        )
    return rows[0]


def _one_campaign(customer_id: str, campaign_id: str) -> Dict[str, Any]:
    rows = _rows(
        customer_id,
        _CAMPAIGN_FIELDS + f"          AND campaign.id = {campaign_id}",
    )
    if not rows:
        raise ValueError(
            f"No campaign {campaign_id} in account {customer_id} "
            f"(or it is REMOVED)."
        )
    return rows[0]


def _serving(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in lines if c["status"] == "ENABLED"]


@bidding_strategies_mcp.tool
def update_portfolio_bid_ceiling(
    customer_id: str,
    bidding_strategy_id: str,
    cpc_bid_ceiling: float,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Sets the CPC bid ceiling on a portfolio (shared) bidding strategy.

    This is the only way to reach the ceiling of a campaign whose bidding
    lives on a portfolio strategy; campaigns.update_bid_strategy writes
    campaign-level fields and would be refused.

    The ceiling applies to EVERY campaign on the strategy, so the result
    names them all. Lowering it is written straight away. Raising it goes
    through a plan and is checked against the account's
    max_cpc_ceiling_try.

    A ceiling of 0 on a strategy means "no ceiling", which is why this
    tool will not set one: pass the number you want, in the account's own
    currency, never a converted one.

    Args:
        customer_id: The Google Ads account id.
        bidding_strategy_id: The portfolio strategy to change, from
            list_bidding_strategies.
        cpc_bid_ceiling: Maximum CPC, in the account's own currency.
        confirm_spend: Required when the ceiling goes UP and at least one
            campaign on the strategy is ENABLED.

    Returns:
        The old and new ceiling, the campaigns affected, and the strategy
        read back.
    """
    customer_id = _clean_id(customer_id)
    bidding_strategy_id = _clean_id(bidding_strategy_id)

    row = _one_strategy(customer_id, bidding_strategy_id)
    campaign_rows = _campaign_rows(customer_id)
    described = _describe_strategy(row, campaign_rows)
    strategy_type = described["type"]
    path = _bidding.PORTFOLIO_CEILING_PATHS.get(strategy_type)
    if path is None:
        raise ValueError(
            f"Strategy {bidding_strategy_id} is {strategy_type}, which "
            f"carries no cpc_bid_ceiling. Strategies that do: "
            f"{sorted(_bidding.PORTFOLIO_CEILING_PATHS)}."
        )

    old = described["cpc_bid_ceiling"] or 0.0
    serving = _serving(described["campaigns"])
    if cpc_bid_ceiling > old and serving and not confirm_spend:
        daily = sum(c["daily_budget"] for c in serving)
        raise SpendConfirmationRequired(
            f"Raising the CPC ceiling on '{described['name']}' from "
            f"{old or 'none'} to {cpc_bid_ceiling} changes how "
            f"{len(serving)} ENABLED campaign(s) bid right now: "
            f"{', '.join(c['name'] or c['campaign_id'] for c in serving)}. "
            f"Those campaigns' budgets total {round(daily, 2)}/day in the "
            f"account's currency. A higher ceiling does not raise the "
            f"budget, but it does raise what a single click may cost. "
            f"Show this to the user and call again with "
            f"confirm_spend=True only after they agree."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("BiddingStrategyService")
    operation = client.get_type("BiddingStrategyOperation")
    strategy = operation.update
    strategy.resource_name = service.bidding_strategy_path(
        customer_id, bidding_strategy_id
    )
    block, leaf = path.split(".", 1)
    setattr(
        getattr(strategy, block), leaf, _bidding.to_micros(cpc_bid_ceiling)
    )
    _bidding.set_mask(client, operation, [path])
    service.mutate_bidding_strategies(
        customer_id=customer_id, operations=[operation]
    )

    verified = _rows(
        customer_id,
        _STRATEGY_FIELDS
        + f"        WHERE bidding_strategy.id = {bidding_strategy_id}",
    )
    return {
        "bidding_strategy_id": bidding_strategy_id,
        "name": described["name"],
        "type": strategy_type,
        "field": path,
        "old_cpc_bid_ceiling": old or None,
        "new_cpc_bid_ceiling": cpc_bid_ceiling,
        "campaigns_affected": described["campaigns"],
        "enabled_campaigns_affected": len(serving),
        "verified": [
            _describe_strategy(r, campaign_rows) for r in verified
        ],
    }


@bidding_strategies_mcp.tool
def detach_campaign_from_portfolio(
    customer_id: str,
    campaign_id: str,
    bidding_strategy: str,
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    impression_share_location: Optional[str] = None,
    impression_share_percent: Optional[float] = None,
    cpc_bid_ceiling: Optional[float] = None,
    new_budget_id: Optional[str] = None,
    unalign_shared_budget: bool = False,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Takes a campaign off a portfolio strategy and gives it its own.

    Google will not let a campaign leave a portfolio strategy in a bare
    update: clearing campaign.bidding_strategy by itself is refused with
    BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED when the strategy is
    aligned to a shared campaign budget, because every campaign on that
    strategy has to use that budget and the other way round. The field
    the error names is one the operation never touched, which is what
    makes it so hard to read.

    So the strategy is cleared and the campaign's own one is set in ONE
    operation, with both ``bidding_strategy`` and the new scheme's leaf
    fields in the update mask.

    When the strategy IS aligned to a shared budget, that is not enough on
    its own and there are two legal routes. This tool does not choose:

      new_budget_id=<id>
          Move the campaign to another budget in the same operation. The
          portfolio strategy and its shared budget stay as they are for
          the other campaigns on them. Create the budget first with
          campaigns.create_campaign_budget.

      unalign_shared_budget=True
          Clear aligned_campaign_budget_id on the STRATEGY first, then
          move the campaign. One extra write, and it changes the strategy
          for every campaign on it -- which is why it needs saying out
          loud. The campaign keeps the shared budget.

    Called with neither, it refuses and names what each route would touch.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to take off the portfolio strategy.
        bidding_strategy: The campaign's own strategy from here on:
            MANUAL_CPC, MAXIMIZE_CLICKS, MAXIMIZE_CONVERSIONS, TARGET_CPA,
            TARGET_ROAS or TARGET_IMPRESSION_SHARE.
        target_cpa: Required for TARGET_CPA, in the account's currency.
        target_roas: Required for TARGET_ROAS, as a ratio (4.0 = 400%).
        impression_share_location: Required for TARGET_IMPRESSION_SHARE.
        impression_share_percent: Required for TARGET_IMPRESSION_SHARE,
            as a percentage 1-100.
        cpc_bid_ceiling: Maximum CPC, in the account's currency. Required
            for TARGET_IMPRESSION_SHARE, optional for MAXIMIZE_CLICKS,
            rejected by the conversion-based strategies.
        new_budget_id: Move the campaign to this budget in the same
            operation. One of the two routes above.
        unalign_shared_budget: Unalign the strategy from its shared budget
            first. The other route. Affects every campaign on the
            strategy.
        confirm_spend: Required when the campaign is ENABLED.

    Returns:
        What was on the campaign before, what is on it now, which other
        campaigns were affected, and the campaign read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    strategy_name = str(bidding_strategy).upper()

    campaign_rows = _campaign_rows(customer_id)
    row = _one_campaign(customer_id, campaign_id)
    me = _campaign_line(row)
    current = _strategy_of(row)
    if not current:
        raise ValueError(
            f"Campaign {campaign_id} ('{me['name']}') already carries its "
            f"own bidding strategy ({me['bid_strategy_type']}); there is "
            f"no portfolio strategy to detach from. Use "
            f"campaigns.update_bid_strategy."
        )

    portfolio_id = current.rsplit("/", 1)[-1]
    portfolio = _describe_strategy(
        _one_strategy(customer_id, portfolio_id), campaign_rows
    )
    aligned = portfolio["aligned_campaign_budget_id"]
    siblings = [
        c for c in portfolio["campaigns"] if c["campaign_id"] != campaign_id
    ]

    if me["status"] == "ENABLED" and not confirm_spend:
        raise SpendConfirmationRequired(
            f"'{me['name']}' is ENABLED and spending "
            f"{me['daily_budget']}/day, about "
            f"{round(me['daily_budget'] * 30.4, 2)}/month in the account's "
            f"currency. Taking it off portfolio strategy "
            f"'{portfolio['name']}' ({portfolio['type']}) and putting it on "
            f"{strategy_name} changes how that money is spent immediately. "
            f"Show this to the user and call again with confirm_spend=True "
            f"only after they agree."
        )

    if aligned and not (new_budget_id or unalign_shared_budget):
        names = ", ".join(
            f"{c['name']} ({c['status']})" for c in siblings
        ) or "none"
        raise PortfolioAlignmentRequired(
            f"Portfolio strategy '{portfolio['name']}' ({portfolio_id}) is "
            f"aligned to shared campaign budget {aligned}, so campaign "
            f"{campaign_id} cannot leave the strategy while it stays on "
            f"that budget -- Google refuses it with "
            f"BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED. Pick a route "
            f"and call again:\n"
            f"  new_budget_id=<id>: move this campaign to its own budget. "
            f"Touches only '{me['name']}'. The shared budget "
            f"({aligned}) and the strategy stay as they are for: {names}. "
            f"Create the budget first with "
            f"campaigns.create_campaign_budget.\n"
            f"  unalign_shared_budget=True: unalign the STRATEGY from the "
            f"shared budget. '{me['name']}' keeps budget {aligned}, and "
            f"the change applies to the strategy that still carries: "
            f"{names}."
        )

    client = utils.get_googleads_client()
    unaligned = False
    if aligned and unalign_shared_budget:
        strategy_service = utils.get_googleads_service(
            "BiddingStrategyService"
        )
        unalign = client.get_type("BiddingStrategyOperation")
        unalign.update.resource_name = (
            strategy_service.bidding_strategy_path(customer_id, portfolio_id)
        )
        unalign.update.aligned_campaign_budget_id = 0
        _bidding.set_mask(
            client, unalign, ["aligned_campaign_budget_id"]
        )
        strategy_service.mutate_bidding_strategies(
            customer_id=customer_id, operations=[unalign]
        )
        unaligned = True

    service = utils.get_googleads_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = service.campaign_path(customer_id, campaign_id)
    # The clear and the replacement go in the SAME operation: Google
    # validates the result, not the steps, and a campaign with neither a
    # portfolio strategy nor its own is not a legal state.
    campaign.bidding_strategy = ""
    paths = ["bidding_strategy"] + _bidding.apply_campaign_scheme(
        client,
        campaign,
        strategy_name,
        target_cpa=target_cpa,
        target_roas=target_roas,
        impression_share_location=impression_share_location,
        impression_share_percent=impression_share_percent,
        cpc_bid_ceiling=cpc_bid_ceiling,
    )
    moved_to = None
    if new_budget_id:
        budget_service = utils.get_googleads_service("CampaignBudgetService")
        new_budget_id = _clean_id(new_budget_id)
        campaign.campaign_budget = budget_service.campaign_budget_path(
            customer_id, new_budget_id
        )
        paths.append("campaign_budget")
        moved_to = new_budget_id
    _bidding.set_mask(client, operation, paths)
    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    verified = _rows(
        customer_id,
        _CAMPAIGN_FIELDS + f"          AND campaign.id = {campaign_id}",
    )
    return {
        "campaign_id": campaign_id,
        "campaign_name": me["name"],
        "campaign_status": me["status"],
        "left_portfolio": {
            "bidding_strategy_id": portfolio_id,
            "name": portfolio["name"],
            "type": portfolio["type"],
            "was_aligned_to_budget": aligned,
        },
        "new_bid_strategy": strategy_name,
        "update_mask": paths,
        "shared_budget_unaligned": unaligned,
        "moved_to_budget_id": moved_to,
        "other_campaigns_on_that_strategy": siblings,
        "also_affected": (
            [c["name"] for c in siblings] if unaligned else []
        ),
        "verified": [_campaign_line(r) for r in verified],
    }


@bidding_strategies_mcp.tool
def attach_campaign_to_portfolio(
    customer_id: str,
    campaign_id: str,
    bidding_strategy_id: str,
    move_to_aligned_budget: bool = False,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Puts a campaign onto an existing portfolio (shared) strategy.

    The undo of detach_campaign_from_portfolio, and the only way to
    rebuild what a detach took apart.

    From the moment it is attached the campaign's bidding settings live on
    the STRATEGY: its own ceiling and target stop applying, and changing
    them means update_portfolio_bid_ceiling, not
    campaigns.update_bid_strategy. The strategy is shared, so the campaign
    now also learns from -- and competes with -- every other campaign on
    it.

    If the strategy is aligned to a shared campaign budget, the campaign
    has to use that budget too; Google refuses the attach otherwise.
    Moving a campaign onto another budget changes what it may spend every
    day, so it needs move_to_aligned_budget=True and the result reports
    the change in daily budget.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to attach.
        bidding_strategy_id: The portfolio strategy, from
            list_bidding_strategies.
        move_to_aligned_budget: Move the campaign onto the strategy's
            aligned shared budget in the same operation. Required when the
            strategy has one and the campaign is not on it already.
        confirm_spend: Required when the campaign is ENABLED.

    Returns:
        What the campaign had, what it has now, and the campaign read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    bidding_strategy_id = _clean_id(bidding_strategy_id)

    campaign_rows = _campaign_rows(customer_id)
    row = _one_campaign(customer_id, campaign_id)
    me = _campaign_line(row)
    portfolio = _describe_strategy(
        _one_strategy(customer_id, bidding_strategy_id), campaign_rows
    )
    aligned = portfolio["aligned_campaign_budget_id"]

    if _strategy_of(row) == portfolio["resource_name"]:
        return {
            "campaign_id": campaign_id,
            "bidding_strategy_id": bidding_strategy_id,
            "changed": False,
            "note": (
                f"'{me['name']}' is already on portfolio strategy "
                f"'{portfolio['name']}'. Nothing written."
            ),
            "verified": [me],
        }

    if me["status"] == "ENABLED" and not confirm_spend:
        raise SpendConfirmationRequired(
            f"'{me['name']}' is ENABLED and spending "
            f"{me['daily_budget']}/day. Putting it on portfolio strategy "
            f"'{portfolio['name']}' ({portfolio['type']}, ceiling "
            f"{portfolio['cpc_bid_ceiling'] or 'none'}) hands its bidding "
            f"to that strategy immediately and makes its own ceiling stop "
            f"applying. Show this to the user and call again with "
            f"confirm_spend=True only after they agree."
        )

    budget_change = None
    if aligned and me["budget_id"] != str(aligned):
        if not move_to_aligned_budget:
            raise PortfolioAlignmentRequired(
                f"Portfolio strategy '{portfolio['name']}' is aligned to "
                f"shared campaign budget {aligned}, so a campaign on it "
                f"must use that budget. '{me['name']}' is on budget "
                f"{me['budget_id']} at {me['daily_budget']}/day. Call again "
                f"with move_to_aligned_budget=True to move it onto budget "
                f"{aligned} "
                f"({_budget_amount(campaign_rows, str(aligned))}/day, "
                f"shared with "
                f"{len(portfolio['campaigns'])} campaign(s)) in the same "
                f"operation."
            )
        budget_change = {
            "from_budget_id": me["budget_id"],
            "from_daily": me["daily_budget"],
            "to_budget_id": str(aligned),
            "to_daily": _budget_amount(campaign_rows, str(aligned)),
            "note": (
                "The target budget is shared: its amount is the ceiling "
                "for every campaign on it together, not for this one "
                "alone."
            ),
        }

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignService")
    strategy_service = utils.get_googleads_service("BiddingStrategyService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = service.campaign_path(customer_id, campaign_id)
    campaign.bidding_strategy = strategy_service.bidding_strategy_path(
        customer_id, bidding_strategy_id
    )
    paths = ["bidding_strategy"]
    if budget_change:
        budget_service = utils.get_googleads_service("CampaignBudgetService")
        campaign.campaign_budget = budget_service.campaign_budget_path(
            customer_id, str(aligned)
        )
        paths.append("campaign_budget")
    _bidding.set_mask(client, operation, paths)
    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    verified = _rows(
        customer_id,
        _CAMPAIGN_FIELDS + f"          AND campaign.id = {campaign_id}",
    )
    return {
        "campaign_id": campaign_id,
        "campaign_name": me["name"],
        "old_bid_strategy": me["bid_strategy_type"],
        "bidding_strategy_id": bidding_strategy_id,
        "portfolio_name": portfolio["name"],
        "portfolio_type": portfolio["type"],
        "portfolio_cpc_bid_ceiling": portfolio["cpc_bid_ceiling"],
        "update_mask": paths,
        "budget_change": budget_change,
        "changed": True,
        "note": (
            "This campaign's bidding is now on the strategy. Change its "
            "ceiling with update_portfolio_bid_ceiling; "
            "campaigns.update_bid_strategy no longer reaches it."
        ),
        "verified": [_campaign_line(r) for r in verified],
    }
