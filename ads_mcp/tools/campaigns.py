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

"""Write tools for budgets, campaigns, geo targeting and ad scheduling.

SAFETY CONTRACT (enforced in code, not merely by convention):

  * Every campaign is created PAUSED. It cannot serve and cannot spend.
  * Enabling a campaign, or raising a budget, requires an explicit
    ``confirm_spend=True`` from the caller. Without it the call is refused
    and returns the numbers the user needs in order to decide.
  * Removing anything requires ``confirm_remove=True``.
  * Every write reads the object back and returns its real, post-write state.
"""

from typing import Any, Dict, List, Optional

import proto

from fastmcp import FastMCP
from google.protobuf import field_mask_pb2
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.tools import _bidding

campaigns_mcp = FastMCP("campaigns")

# Google Ads expresses money in "micros": 1 unit of currency = 1_000_000.
_MICROS = 1_000_000

_DAYS = {
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
    "FRIDAY", "SATURDAY", "SUNDAY",
}

# Whether a location target means "is in the area" or "is interested in it".
# Google's default is PRESENCE_OR_INTEREST, which reaches people anywhere in
# the world who merely searched about the area.
_POSITIVE_GEO_TYPES = {
    "PRESENCE",
    "PRESENCE_OR_INTEREST",
    "SEARCH_INTEREST",
}
_NEGATIVE_GEO_TYPES = {"PRESENCE", "PRESENCE_OR_INTEREST"}

# Where on the page Target Impression Share aims to appear.
_IMPRESSION_SHARE_LOCATIONS = {
    "ANYWHERE_ON_PAGE",
    "TOP_OF_PAGE",
    "ABSOLUTE_TOP_OF_PAGE",
}


class SpendConfirmationRequired(Exception):
    """Raised when an action would let money be spent without confirmation."""


def _clean_id(customer_id: str) -> str:
    """Normalizes '123-456-7890' or 123456789 to a bare digit string."""
    cleaned = "".join(ch for ch in str(customer_id) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable customer id: {customer_id!r}")
    return cleaned


def _to_micros(amount: float) -> int:
    """Converts an amount in account currency to micros.

    The caller always passes the human number (500 means 500 TRY / 500 USD in
    the account's own currency). Never pre-convert currencies.
    """
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return int(round(amount * _MICROS))


def _from_micros(micros: int) -> float:
    return round((micros or 0) / _MICROS, 2)


def _read_back(customer_id: str, query: str) -> List[Dict[str, Any]]:
    """Runs a GAQL query and returns plain dict rows, for post-write verification."""
    ga_service = utils.get_googleads_service("GoogleAdsService")
    rows = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            # Enum NAMES, not the raw integers proto-plus defaults to: this
            # output is read by a model, and "status": 3 tells it nothing.
            # Status gates elsewhere in this module compare against the name.
            rows.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return rows


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------


@campaigns_mcp.tool
def create_campaign_budget(
    customer_id: str,
    name: str,
    daily_amount: float,
    explicitly_shared: bool = False,
) -> Dict[str, Any]:
    """Creates a campaign budget.

    A budget on its own never spends; only a campaign that uses it and is
    ENABLED can spend. Creating a budget is therefore safe and needs no
    confirmation.

    Args:
        customer_id: The Google Ads account id. Dashes are fine.
        name: Budget name. Must be unique within the account.
        daily_amount: Average daily budget in the ACCOUNT'S OWN currency
            (e.g. 500 means 500 TRY in a TRY account). Do not convert to USD.
        explicitly_shared: True to let several campaigns share this budget.

    Returns:
        The created budget's id, resource name, and daily amount, read back
        from the API.
    """
    customer_id = _clean_id(customer_id)
    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignBudgetService")

    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.create
    budget.name = name
    budget.amount_micros = _to_micros(daily_amount)
    budget.delivery_method = (
        client.enums.BudgetDeliveryMethodEnum.STANDARD
    )
    budget.explicitly_shared = explicitly_shared

    response = service.mutate_campaign_budgets(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    budget_id = resource_name.rsplit("/", 1)[-1]

    verified = _read_back(
        customer_id,
        f"""
        SELECT campaign_budget.id, campaign_budget.name,
               campaign_budget.amount_micros, campaign_budget.explicitly_shared
        FROM campaign_budget
        WHERE campaign_budget.id = {budget_id}
        """,
    )
    return {
        "budget_id": budget_id,
        "resource_name": resource_name,
        "daily_amount": daily_amount,
        "implied_monthly_amount": round(daily_amount * 30.4, 2),
        "verified": verified,
    }


@campaigns_mcp.tool
def update_campaign_budget(
    customer_id: str,
    budget_id: str,
    daily_amount: float,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Changes a budget's daily amount.

    RAISING a budget increases what the account can spend, so it requires
    confirm_spend=True. Lowering a budget is allowed without confirmation.

    Args:
        customer_id: The Google Ads account id.
        budget_id: The budget to change.
        daily_amount: New daily amount, in the account's own currency.
        confirm_spend: Must be True when the new amount is higher than the
            current one. Only pass True after the user has agreed to that
            specific increase.

    Returns:
        Old and new amounts, read back from the API.
    """
    customer_id = _clean_id(customer_id)
    current = _read_back(
        customer_id,
        f"""
        SELECT campaign_budget.id, campaign_budget.name,
               campaign_budget.amount_micros
        FROM campaign_budget WHERE campaign_budget.id = {budget_id}
        """,
    )
    if not current:
        raise ValueError(f"No budget {budget_id} in account {customer_id}.")

    old_micros = int(current[0]["campaign_budget"]["amount_micros"])
    old_amount = _from_micros(old_micros)
    new_micros = _to_micros(daily_amount)

    if new_micros > old_micros and not confirm_spend:
        raise SpendConfirmationRequired(
            f"This raises the daily budget from {old_amount} to {daily_amount} "
            f"(+{round(daily_amount - old_amount, 2)}/day, about "
            f"{round((daily_amount - old_amount) * 30.4, 2)}/month) in the "
            f"account's currency. Show these numbers to the user and call "
            f"again with confirm_spend=True only after they agree."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    operation.update.resource_name = service.campaign_budget_path(
        customer_id, budget_id
    )
    operation.update.amount_micros = new_micros
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(paths=["amount_micros"]),
    )
    service.mutate_campaign_budgets(
        customer_id=customer_id, operations=[operation]
    )

    return {
        "budget_id": budget_id,
        "old_daily_amount": old_amount,
        "new_daily_amount": daily_amount,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_budget.id, campaign_budget.amount_micros
            FROM campaign_budget WHERE campaign_budget.id = {budget_id}
            """,
        ),
    }


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------


@campaigns_mcp.tool
def create_search_campaign(
    customer_id: str,
    name: str,
    budget_resource_name: str,
    bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search_network: bool = False,
    content_network: bool = False,
) -> Dict[str, Any]:
    """Creates a Search campaign. ALWAYS created PAUSED — it cannot spend.

    After this returns, the campaign exists but serves nothing. Add ad groups,
    ads and keywords, set geo targeting and a schedule, show the user the whole
    plan, and only then call set_campaign_status to enable it.

    Args:
        customer_id: The Google Ads account id.
        name: Campaign name. Must be unique within the account.
        budget_resource_name: From create_campaign_budget, e.g.
            "customers/1234567890/campaignBudgets/987654".
        bidding_strategy: One of MANUAL_CPC, MAXIMIZE_CLICKS,
            MAXIMIZE_CONVERSIONS, TARGET_CPA, TARGET_ROAS.
            Note: conversion-based strategies need working conversion tracking;
            check that before choosing one.
        target_cpa: Required for TARGET_CPA. In the account's own currency.
        target_roas: Required for TARGET_ROAS, as a ratio (4.0 means 400%).
        start_date: YYYY-MM-DD. Defaults to today if omitted.
        end_date: YYYY-MM-DD. Omit for no end date.
        search_network: Include Google search partners. Default False.
        content_network: Include the Display network. Default False —
            leaving this on is a classic way to waste a search budget.

    Returns:
        The created campaign's id, its PAUSED status, and the read-back state.
    """
    customer_id = _clean_id(customer_id)
    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.create
    campaign.name = name
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.SEARCH
    )
    # Non-negotiable: never create anything that can start spending.
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_resource_name
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = search_network
    campaign.network_settings.target_content_network = content_network
    # Required by API v25 on every campaign create. This server does not
    # create political advertising.
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )

    strategy = bidding_strategy.upper()
    if strategy == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False
    elif strategy == "MAXIMIZE_CLICKS":
        client.copy_from(
            campaign.target_spend, client.get_type("TargetSpend")
        )
    elif strategy == "MAXIMIZE_CONVERSIONS":
        client.copy_from(
            campaign.maximize_conversions,
            client.get_type("MaximizeConversions"),
        )
    elif strategy == "TARGET_CPA":
        if target_cpa is None:
            raise ValueError("TARGET_CPA requires target_cpa.")
        # Target CPA is Maximize Conversions with a target, not a strategy of
        # its own; the standalone TargetCpa is rejected on Search campaigns.
        campaign.maximize_conversions.target_cpa_micros = _to_micros(
            target_cpa
        )
    elif strategy == "TARGET_ROAS":
        if target_roas is None:
            raise ValueError("TARGET_ROAS requires target_roas.")
        campaign.maximize_conversion_value.target_roas = target_roas
    else:
        raise ValueError(f"Unknown bidding strategy: {bidding_strategy}")

    if start_date:
        campaign.start_date_time = f"{start_date} 00:00:00"
    if end_date:
        campaign.end_date_time = f"{end_date} 23:59:59"

    response = service.mutate_campaigns(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    campaign_id = resource_name.rsplit("/", 1)[-1]

    return {
        "campaign_id": campaign_id,
        "resource_name": resource_name,
        "status": "PAUSED",
        "note": (
            "Created PAUSED. It is not serving and is not spending. "
            "Enable it with set_campaign_status(confirm_spend=True) only "
            "after the user has approved the daily budget."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   campaign.start_date_time, campaign.end_date_time,
                   campaign_budget.amount_micros
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        ),
    }


def _change_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: str,
    confirm_spend: bool,
    confirm_remove: bool,
) -> Dict[str, Any]:
    """Shared by set_campaign_status, pause_campaign and enable_campaign."""
    customer_id = _clean_id(customer_id)
    status = status.upper()
    if status not in {"ENABLED", "PAUSED", "REMOVED"}:
        raise ValueError("status must be ENABLED, PAUSED or REMOVED.")

    current = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign_budget.amount_micros
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not current:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")

    daily = _from_micros(
        int(current[0]["campaign_budget"]["amount_micros"])
    )

    if status == "ENABLED" and not confirm_spend:
        raise SpendConfirmationRequired(
            f"Enabling '{current[0]['campaign']['name']}' starts spending "
            f"{daily}/day, about {round(daily * 30.4, 2)}/month in the "
            f"account's currency. Show these numbers to the user and call "
            f"again with confirm_spend=True only after they agree."
        )
    if status == "REMOVED" and not confirm_remove:
        raise ValueError(
            "Removing a campaign is not reversible. Name what will be lost, "
            "then call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    resource_name = service.campaign_path(customer_id, campaign_id)

    if status == "REMOVED":
        # Deletion is its own operation, not a status update.
        operation.remove = resource_name
    else:
        operation.update.resource_name = resource_name
        operation.update.status = getattr(
            client.enums.CampaignStatusEnum, status
        )
        client.copy_from(
            operation.update_mask, field_mask_pb2.FieldMask(paths=["status"])
        )

    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    return {
        "campaign_id": campaign_id,
        "previous_status": current[0]["campaign"].get("status"),
        "new_status": status,
        "daily_budget": daily,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        ),
    }


@campaigns_mcp.tool
def set_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: str,
    confirm_spend: bool = False,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Enables, pauses or removes a campaign. DEPRECATED: prefer the split tools.

    Use pause_campaign to stop spend and enable_campaign to start it. This
    tool does both, which means a client cannot allow pausing without also
    allowing enabling; it stays for one release and then goes.

    ENABLED starts real spending and requires confirm_spend=True.
    REMOVED is not reversible and requires confirm_remove=True.
    PAUSED always works without confirmation -- stopping spend is always safe.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        status: ENABLED, PAUSED or REMOVED.
        confirm_spend: Required for ENABLED. Pass True only after showing the
            user the daily and implied monthly budget and getting a yes.
        confirm_remove: Required for REMOVED.

    Returns:
        The campaign's state read back after the change.
    """
    return _change_campaign_status(
        customer_id, campaign_id, status, confirm_spend, confirm_remove
    )


@campaigns_mcp.tool
def pause_campaign(
    customer_id: str,
    campaign_id: str,
) -> Dict[str, Any]:
    """Pauses a campaign. Stopping spend is always safe, so nothing is asked.

    The campaign keeps its history, its ads and its keywords and can be
    enabled again with enable_campaign. This is the right first move when
    something looks wrong and the cause is not yet known.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to pause.

    Returns:
        The campaign's status before and after, read back from the API.
    """
    return _change_campaign_status(
        customer_id, campaign_id, "PAUSED", False, False
    )


@campaigns_mcp.tool
def enable_campaign(
    customer_id: str,
    campaign_id: str,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Enables a campaign, which starts real spending.

    This is the single gate that lets money move: everything under a
    campaign is created ENABLED and stays silent until the campaign itself
    is. The refusal carries the daily budget and the implied monthly figure
    so the user can be shown real numbers before agreeing.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to enable.
        confirm_spend: Required. Pass True only after showing the user the
            daily and implied monthly budget and getting a yes.

    Returns:
        The campaign's status before and after, read back from the API.
    """
    return _change_campaign_status(
        customer_id, campaign_id, "ENABLED", confirm_spend, False
    )


# --------------------------------------------------------------------------
# Call-focused campaigns
#
# Google Ads API v25 has no call ad: the Ad message carries no `call_ad`
# field and the client library has no CallAdInfo (docs/kaynaklar.md).
# What the API can build is the shape Google migrated call ads into -- a
# Search campaign whose responsive search ads carry a call asset -- and
# that is what this tool builds, in one step and in the right order.
# --------------------------------------------------------------------------


@campaigns_mcp.tool
def create_call_campaign(
    customer_id: str,
    name: str,
    phone_number: str,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    budget_resource_name: Optional[str] = None,
    daily_budget: Optional[float] = None,
    country_code: str = "TR",
    call_conversion_action_id: Optional[str] = None,
    ad_group_name: Optional[str] = None,
    bidding_strategy: str = "MAXIMIZE_CLICKS",
    cpc_bid: Optional[float] = None,
    keywords: Optional[List[Dict[str, Any]]] = None,
    path1: Optional[str] = None,
    path2: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds a campaign whose ads exist to make the phone ring.

    Call-only ads are gone from the Google Ads API (v25 has no call ad
    type), so a "call campaign" is built the way Google now builds one: a
    Search campaign, one ad group, a responsive search ad, and a CALL asset
    on the campaign so every ad shows the call button and every call is
    counted as a conversion. This tool does those steps in order, so the
    result is one campaign rather than four half-finished objects.

    The campaign is created PAUSED and spends nothing until enable_campaign.
    Keywords are optional here so the copy can be reviewed first; add_keywords
    fills the ad group afterwards.

    Args:
        customer_id: The Google Ads account id.
        name: Campaign name, unique in the account.
        phone_number: The business number as a local caller would dial it.
        final_url: Landing page for the ad, with https://. Copy it from an
            approved ad or confirm it resolves; never type it from memory.
        headlines: 3-15 headlines, at most 30 characters each. Put the
            call to action in them ("Hemen Arayın").
        descriptions: 2-4 descriptions, at most 90 characters each.
        budget_resource_name: An existing budget, e.g.
            "customers/123/campaignBudgets/456". Give this OR daily_budget.
        daily_budget: Creates a new budget with this daily amount in the
            account's own currency. Give this OR budget_resource_name.
        country_code: Two-letter country of the number, e.g. "TR".
        call_conversion_action_id: Count calls against this conversion
            action. Omit to use the account's default call action.
        ad_group_name: Defaults to the campaign name.
        bidding_strategy: MANUAL_CPC, MAXIMIZE_CLICKS (default) or
            MAXIMIZE_CONVERSIONS. Conversion-based bidding needs a call
            conversion action that actually records.
        cpc_bid: Max CPC for the ad group, in the account's currency. Used
            by MANUAL_CPC only.
        keywords: Optional, same shape as add_keywords:
            [{"text": "acil çilingir", "match_type": "PHRASE"}].
        path1: Optional display path, at most 15 characters.
        path2: Optional second display path.

    Returns:
        The ids of everything created, the campaign read back PAUSED, and
        the account's call reporting state -- which must be on for the
        calls to be measured.
    """
    # Lazy imports: adgroups and extensions import this module.
    from ads_mcp.tools import adgroups, extensions

    customer_id = _clean_id(customer_id)
    if bool(budget_resource_name) == bool(daily_budget):
        raise ValueError("Give exactly one of budget_resource_name or daily_budget.")
    strategy = bidding_strategy.upper()
    if strategy not in {"MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS"}:
        raise ValueError(
            "bidding_strategy must be MANUAL_CPC, MAXIMIZE_CLICKS or "
            "MAXIMIZE_CONVERSIONS."
        )
    if not final_url.startswith(("http://", "https://")):
        raise ValueError("final_url must start with http:// or https://")

    budget = None
    if daily_budget is not None:
        budget = create_campaign_budget(
            customer_id, f"{name} - bütçe", daily_budget
        )
        budget_resource_name = budget["resource_name"]

    campaign = create_search_campaign(
        customer_id,
        name,
        budget_resource_name,
        bidding_strategy=strategy,
        search_network=False,
        content_network=False,
    )
    campaign_id = campaign["campaign_id"]

    ad_group = adgroups.create_ad_group(
        customer_id, campaign_id, ad_group_name or name, cpc_bid=cpc_bid
    )
    ad = adgroups.create_responsive_search_ad(
        customer_id,
        ad_group["ad_group_id"],
        final_url,
        headlines,
        descriptions,
        path1=path1,
        path2=path2,
    )
    call = extensions.add_call_extension(
        customer_id,
        campaign_id,
        phone_number,
        country_code=country_code,
        call_conversion_action_id=call_conversion_action_id,
    )
    added_keywords = None
    if keywords:
        added_keywords = adgroups.add_keywords(
            customer_id, ad_group["ad_group_id"], keywords
        )

    return {
        "campaign_id": campaign_id,
        "budget_resource_name": budget_resource_name,
        "budget_created": budget["budget_id"] if budget else None,
        "ad_group_id": ad_group["ad_group_id"],
        "ad_id": ad["ad_id"],
        "call_asset_resource_name": call.get("asset_resource_name"),
        "keywords_added": added_keywords["added"] if added_keywords else 0,
        "status": "PAUSED",
        "call_reporting_setting": call.get("call_reporting_setting"),
        "warnings": call.get("warnings", []),
        "note": (
            "Created PAUSED. The API has no call-only ad type any more; this "
            "is a Search campaign with a call asset, which is what Google "
            "migrated call ads into. Set geo targeting and languages, add "
            "keywords if none were given, then enable_campaign."
        ),
        "verified": campaign.get("verified"),
    }


# --------------------------------------------------------------------------
# Geo targeting
# --------------------------------------------------------------------------


@campaigns_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_location_ids(
    query: str,
    country_code: str = "TR",
    locale: str = "tr",
) -> List[Dict[str, Any]]:
    """Looks up geo target constant ids for a place name.

    Call this before set_campaign_geo_targets — never guess a location id.

    Args:
        query: Place name, e.g. "Istanbul", "Ankara", "Turkey".
        country_code: Two-letter country code to search within.
        locale: Language for the returned names.

    Returns:
        Matching locations with their id, canonical name and type.
    """
    service = utils.get_googleads_service("GeoTargetConstantService")
    client = utils.get_googleads_client()
    request = client.get_type("SuggestGeoTargetConstantsRequest")
    request.locale = locale
    request.country_code = country_code
    request.location_names.names.append(query)

    response = service.suggest_geo_target_constants(request=request)
    return [
        {
            "id": s.geo_target_constant.id,
            "name": s.geo_target_constant.name,
            "canonical_name": s.geo_target_constant.canonical_name,
            "target_type": s.geo_target_constant.target_type,
            "resource_name": s.geo_target_constant.resource_name,
        }
        for s in response.geo_target_constant_suggestions
    ]


@campaigns_mcp.tool
def set_campaign_geo_targets(
    customer_id: str,
    campaign_id: str,
    location_ids: List[str],
    negative: bool = False,
) -> Dict[str, Any]:
    """Adds geo targets to a campaign.

    Get the ids from find_location_ids first. This ADDS criteria; it does not
    replace existing ones. Adding targeting never increases spend beyond the
    campaign budget, so no confirmation is needed.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to target.
        location_ids: Geo target constant ids, e.g. ["2792"] for Turkey,
            ["1012782"] for Istanbul.
        negative: True to EXCLUDE these locations instead of targeting them.

    Returns:
        The campaign's full geo targeting, read back after the change.
    """
    customer_id = _clean_id(customer_id)
    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_service = utils.get_googleads_service("CampaignService")

    operations = []
    for location_id in location_ids:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        criterion.location.geo_target_constant = (
            f"geoTargetConstants/{_clean_id(location_id)}"
        )
        criterion.negative = negative
        operations.append(operation)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    return {
        "campaign_id": campaign_id,
        "added": len(operations),
        "negative": negative,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.location.geo_target_constant,
                   campaign_criterion.negative
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = LOCATION
            """,
        ),
    }


# --------------------------------------------------------------------------
# Geo target type, languages and URL settings
# --------------------------------------------------------------------------


@campaigns_mcp.tool
def set_campaign_geo_target_type(
    customer_id: str,
    campaign_id: str,
    positive_geo_target_type: str = "PRESENCE",
    negative_geo_target_type: Optional[str] = None,
    confirm_widens_reach: bool = False,
) -> Dict[str, Any]:
    """Sets whether location targeting means presence or mere interest.

    Google's default is PRESENCE_OR_INTEREST, which serves the ads to people
    merely INTERESTED in the targeted area from anywhere else in the world.
    For a business people have to walk into, that is usually the largest
    single source of wasted spend, and it is invisible in a campaign report --
    only a geo segment shows it.

    Narrowing to PRESENCE can only reduce reach, so it needs no confirmation.
    Widening back to PRESENCE_OR_INTEREST or SEARCH_INTEREST opens traffic
    that was closed, which is the same class of change as removing a negative
    keyword, so it requires confirm_widens_reach=True.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        positive_geo_target_type: PRESENCE (people in the area),
            PRESENCE_OR_INTEREST (also people interested in it from
            elsewhere -- Google's default), or SEARCH_INTEREST.
        negative_geo_target_type: How EXCLUDED locations are matched:
            PRESENCE or PRESENCE_OR_INTEREST. Omit to keep.
        confirm_widens_reach: Required when the change opens traffic that
            PRESENCE was keeping out.

    Returns:
        Old and new setting, and the campaign read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    positive = positive_geo_target_type.upper()
    if positive not in _POSITIVE_GEO_TYPES:
        raise ValueError(
            f"positive_geo_target_type must be one of "
            f"{sorted(_POSITIVE_GEO_TYPES)}, not {positive_geo_target_type!r}."
        )
    negative = None
    if negative_geo_target_type is not None:
        negative = negative_geo_target_type.upper()
        if negative not in _NEGATIVE_GEO_TYPES:
            raise ValueError(
                f"negative_geo_target_type must be one of "
                f"{sorted(_NEGATIVE_GEO_TYPES)}, not "
                f"{negative_geo_target_type!r}."
            )

    current = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.geo_target_type_setting.positive_geo_target_type,
               campaign.geo_target_type_setting.negative_geo_target_type
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not current:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")

    setting = current[0]["campaign"].get("geo_target_type_setting", {})
    old_positive = str(setting.get("positive_geo_target_type", "UNSPECIFIED"))

    if (
        old_positive == "PRESENCE"
        and positive != "PRESENCE"
        and not confirm_widens_reach
    ):
        raise SpendConfirmationRequired(
            f"'{current[0]['campaign']['name']}' currently serves only to "
            f"people PRESENT in the targeted locations. Changing it to "
            f"{positive} also serves people merely interested in those "
            f"locations from anywhere else, which reopens traffic that is "
            f"closed today. Show this to the user and call again with "
            f"confirm_widens_reach=True only after they agree."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = service.campaign_path(customer_id, campaign_id)

    # The mask has to name a LEAF: masking geo_target_type_setting itself is
    # rejected with FIELD_HAS_SUBFIELDS.
    paths = ["geo_target_type_setting.positive_geo_target_type"]
    campaign.geo_target_type_setting.positive_geo_target_type = getattr(
        client.enums.PositiveGeoTargetTypeEnum, positive
    )
    if negative is not None:
        campaign.geo_target_type_setting.negative_geo_target_type = getattr(
            client.enums.NegativeGeoTargetTypeEnum, negative
        )
        paths.append("geo_target_type_setting.negative_geo_target_type")

    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )
    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    return {
        "campaign_id": campaign_id,
        "old_positive_geo_target_type": old_positive,
        "new_positive_geo_target_type": positive,
        "changed": paths,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name,
                   campaign.geo_target_type_setting.positive_geo_target_type,
                   campaign.geo_target_type_setting.negative_geo_target_type
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        ),
    }


@campaigns_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_language_ids(
    customer_id: str,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Looks up language constant ids by name or two-letter code.

    Call this before set_campaign_languages -- never guess a language id.
    Matching is done here rather than in GAQL because language_constant does
    not accept a LIKE filter on every field, and there are few enough
    languages to filter in Python.

    Args:
        customer_id: The Google Ads account id.
        query: Part of a language name ("Turkish") or its code ("tr").
            Omit to list every targetable language.

    Returns:
        Matching languages with their id, code and name.
    """
    customer_id = _clean_id(customer_id)
    rows = _read_back(
        customer_id,
        """
        SELECT language_constant.id, language_constant.code,
               language_constant.name, language_constant.targetable
        FROM language_constant
        """,
    )

    needle = (query or "").strip().lower()
    out = []
    for row in rows:
        language = row["language_constant"]
        if not language.get("targetable"):
            continue
        name = str(language.get("name", ""))
        code = str(language.get("code", ""))
        if needle and needle not in name.lower() and needle != code.lower():
            continue
        out.append(
            {
                "id": str(language.get("id")),
                "code": code,
                "name": name,
                "resource_name": f"languageConstants/{language.get('id')}",
            }
        )
    return out


@campaigns_mcp.tool
def set_campaign_languages(
    customer_id: str,
    campaign_id: str,
    language_ids: List[str],
    replace_existing: bool = True,
) -> Dict[str, Any]:
    """Sets which languages a campaign targets.

    A campaign created through the API carries NO language criterion, and no
    language criterion means ALL languages -- so a Turkish campaign quietly
    serves against English-language sessions until this is set. That is not
    visible in a campaign report either.

    Language targeting matches the user's Google interface language and
    recent browsing, NOT the language of the keyword, so this narrows who
    sees the ad without touching which queries can match it.

    `replace_existing` defaults to True, so this behaves like its name: a
    language is turned OFF by being absent, and an add-only version could
    not express that.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        language_ids: Language constant ids from find_language_ids,
            e.g. ["1037"] for Turkish.
        replace_existing: True (default) clears the campaign's current
            language criteria first. False adds to what is there.

    Returns:
        The campaign's full language targeting, read back after the change.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    if not language_ids:
        raise ValueError(
            "Give at least one language id. An empty list would leave the "
            "campaign with no language criterion, which means ALL languages "
            "-- pass the ids you want instead."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_service = utils.get_googleads_service("CampaignService")

    removed = []
    if replace_existing:
        for row in _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = LANGUAGE
              AND campaign_criterion.status != 'REMOVED'
            """,
        ):
            criterion = row["campaign_criterion"]
            operation = client.get_type("CampaignCriterionOperation")
            operation.remove = criterion["resource_name"]
            service.mutate_campaign_criteria(
                customer_id=customer_id, operations=[operation]
            )
            removed.append(criterion["criterion_id"])

    operations = []
    for language_id in language_ids:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        criterion.language.language_constant = (
            f"languageConstants/{_clean_id(language_id)}"
        )
        operations.append(operation)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    return {
        "campaign_id": campaign_id,
        "added": len(operations),
        "removed": removed,
        "note": (
            "The campaign now targets ONLY these languages. Any language not "
            "listed is off."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.language.language_constant
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = LANGUAGE
            """,
        ),
    }


def _validate_url_suffix(suffix: str) -> str:
    """Checks a final URL suffix is the bare query string Google expects."""
    cleaned = suffix.strip()
    if not cleaned:
        return ""
    if cleaned[0] in "?&":
        raise ValueError(
            "final_url_suffix is appended to the landing page URL by Google, "
            f"so it must NOT start with '?' or '&'. Pass {cleaned[1:]!r}."
        )
    if " " in cleaned:
        raise ValueError(
            "final_url_suffix cannot contain spaces; URL-encode them as %20."
        )
    for pair in cleaned.split("&"):
        if "=" not in pair:
            raise ValueError(
                f"final_url_suffix must be key=value pairs joined by '&'. "
                f"{pair!r} has no '='."
            )
    return cleaned


@campaigns_mcp.tool
def set_campaign_url_settings(
    customer_id: str,
    campaign_id: str,
    final_url_suffix: Optional[str] = None,
    tracking_url_template: Optional[str] = None,
) -> Dict[str, Any]:
    """Sets the campaign's final URL suffix and/or tracking template.

    The final URL suffix is where UTM parameters belong. Google appends it to
    the landing page URL after any redirect, so analytics and CRM see the
    campaign that produced the click without the ad's own final_url having to
    carry the parameters -- which matters because an RSA's final_url cannot
    be edited afterwards.

    A suffix is per campaign, so a per-campaign utm_campaign value can only
    be set here, not at account level.

    The tracking template is the redirect mechanism itself and a wrong one
    breaks EVERY landing page in the campaign, so it is left alone unless
    explicitly passed. Pass an empty string to clear either field.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        final_url_suffix: Bare query string with no leading '?', e.g.
            "utm_source=google&utm_medium=cpc&utm_campaign=konut-eylul".
            ValueTrack parameters such as {keyword} and {adgroupid} are
            allowed. Omit to keep; pass "" to clear.
        tracking_url_template: Full redirect template, e.g.
            "{lpurl}?utm_source=google". Omit to keep; pass "" to clear.

    Returns:
        What changed, and the campaign read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    if final_url_suffix is None and tracking_url_template is None:
        raise ValueError(
            "Give final_url_suffix or tracking_url_template (or both)."
        )

    current = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.final_url_suffix, campaign.tracking_url_template
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not current:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = service.campaign_path(customer_id, campaign_id)

    paths = []
    if final_url_suffix is not None:
        campaign.final_url_suffix = _validate_url_suffix(final_url_suffix)
        paths.append("final_url_suffix")
    if tracking_url_template is not None:
        campaign.tracking_url_template = tracking_url_template.strip()
        paths.append("tracking_url_template")

    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )
    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    return {
        "campaign_id": campaign_id,
        "changed": paths,
        "before": {
            "final_url_suffix": current[0]["campaign"].get(
                "final_url_suffix", ""
            ),
            "tracking_url_template": current[0]["campaign"].get(
                "tracking_url_template", ""
            ),
        },
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.final_url_suffix,
                   campaign.tracking_url_template
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        ),
    }


# --------------------------------------------------------------------------
# Ad scheduling
# --------------------------------------------------------------------------


@campaigns_mcp.tool
def set_campaign_ad_schedule(
    customer_id: str,
    campaign_id: str,
    schedule: List[Dict[str, Any]],
    replace_existing: bool = True,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Sets when a campaign is allowed to serve, in the ACCOUNT'S time zone.

    A campaign with no ad schedule runs all week. Once any schedule entry
    exists, the campaign serves ONLY inside the entries that exist — so the
    schedule you pass must list EVERY block the campaign should run in.

    `replace_existing` defaults to True, so this behaves like its name: the
    schedule you pass becomes the whole schedule. That matters because a day
    is turned OFF by being absent, and an add-only version of this tool
    cannot express that at all -- passing Monday-to-Saturday would silently
    leave an existing Sunday entry serving.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to schedule.
        schedule: One dict per time block, e.g.
            [{"day": "MONDAY", "start_hour": 9, "end_hour": 23},
             {"day": "TUESDAY", "start_hour": 9, "end_hour": 23,
              "bid_modifier": 0.5}]
            Hours are 0-24. Minutes may be 0, 15, 30 or 45 via the optional
            "start_minute"/"end_minute" keys; end_hour 24 means midnight.
            The optional "bid_modifier" multiplies the bid inside that block:
            0.5 bids half, 1.5 bids fifty percent more. Allowed range is
            0.1 to 10.0 -- there is no 0, because an hour is turned off by
            being absent, not by a modifier. Use a modifier instead of
            deleting a block when the hours still convert, only worse: a
            deleted block earns nothing and therefore never proves itself
            wrong, while a discounted one keeps producing the data the next
            decision needs.
        replace_existing: True (default) clears the campaign's current ad
            schedule first, so the result is exactly what you passed. False
            adds to what is already there, which can only widen the hours.
        confirm_spend: Required when the campaign is ENABLED and any block
            carries a bid_modifier above 1.0, which raises cost per click
            immediately.

    Returns:
        The campaign's full ad schedule, read back after the change.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_service = utils.get_googleads_service("CampaignService")

    raised = [
        float(e["bid_modifier"])
        for e in schedule
        if e.get("bid_modifier") is not None and float(e["bid_modifier"]) > 1.0
    ]
    if raised:
        current = _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign_budget.amount_micros
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        )
        if not current:
            raise ValueError(
                f"No campaign {campaign_id} in account {customer_id}."
            )
        if _campaign_is_serving(current[0]) and not confirm_spend:
            daily = _from_micros(
                int(current[0]["campaign_budget"]["amount_micros"])
            )
            raise SpendConfirmationRequired(
                f"'{current[0]['campaign']['name']}' is ENABLED and spending "
                f"{daily}/day in the account's currency. A bid modifier above "
                f"1.0 (highest here: {max(raised)}) raises cost per click "
                f"inside those hours straight away. Show this to the user and "
                f"call again with confirm_spend=True only after they agree."
            )

    removed = []
    if replace_existing:
        for row in _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = AD_SCHEDULE
              AND campaign_criterion.status != 'REMOVED'
            """,
        ):
            criterion = row["campaign_criterion"]
            operation = client.get_type("CampaignCriterionOperation")
            operation.remove = criterion["resource_name"]
            service.mutate_campaign_criteria(
                customer_id=customer_id, operations=[operation]
            )
            removed.append(criterion["criterion_id"])

    minute_enum = client.enums.MinuteOfHourEnum
    minute_names = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}

    operations = []
    for entry in schedule:
        day = str(entry["day"]).upper()
        if day not in _DAYS:
            raise ValueError(f"Unknown day: {entry['day']}")
        start_hour = int(entry.get("start_hour", 0))
        end_hour = int(entry.get("end_hour", 24))
        if not 0 <= start_hour <= 23 or not 1 <= end_hour <= 24:
            raise ValueError("start_hour must be 0-23 and end_hour 1-24.")
        if end_hour <= start_hour:
            raise ValueError(
                f"end_hour ({end_hour}) must be after start_hour "
                f"({start_hour}) for {day}."
            )
        for key in ("start_minute", "end_minute"):
            if int(entry.get(key, 0)) not in minute_names:
                raise ValueError(f"{key} must be 0, 15, 30 or 45.")

        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        criterion.ad_schedule.day_of_week = getattr(
            client.enums.DayOfWeekEnum, day
        )
        criterion.ad_schedule.start_hour = start_hour
        criterion.ad_schedule.end_hour = end_hour
        criterion.ad_schedule.start_minute = getattr(
            minute_enum, minute_names[int(entry.get("start_minute", 0))]
        )
        criterion.ad_schedule.end_minute = getattr(
            minute_enum, minute_names[int(entry.get("end_minute", 0))]
        )
        if entry.get("bid_modifier") is not None:
            modifier = float(entry["bid_modifier"])
            if not 0.1 <= modifier <= 10.0:
                raise ValueError(
                    f"bid_modifier must be between 0.1 and 10.0, not "
                    f"{modifier} (for {day}). To stop serving in an hour, "
                    f"leave that block out of the schedule instead."
                )
            criterion.bid_modifier = modifier
        operations.append(operation)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    return {
        "campaign_id": campaign_id,
        "added": len(operations),
        "removed": removed,
        "note": (
            "The campaign now serves ONLY during these blocks, in the "
            "account's time zone. Any day not listed is OFF."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.ad_schedule.day_of_week,
                   campaign_criterion.ad_schedule.start_hour,
                   campaign_criterion.ad_schedule.end_hour,
                   campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = AD_SCHEDULE
            """,
        ),
    }


# --------------------------------------------------------------------------
# Campaign settings and bidding
# --------------------------------------------------------------------------


def _campaign_is_serving(row: Dict[str, Any]) -> bool:
    """True when the campaign is ENABLED, i.e. able to spend right now."""
    return str(row["campaign"].get("status")) == "ENABLED"


@campaigns_mcp.tool
def update_campaign(
    customer_id: str,
    campaign_id: str,
    name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search_network: Optional[bool] = None,
    content_network: Optional[bool] = None,
    budget_resource_name: Optional[str] = None,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Changes a campaign's settings. Status is not changed here.

    Use set_campaign_status to enable, pause or remove a campaign; this tool
    only touches settings, so it can never start a campaign serving.

    Moving the campaign to a LARGER budget raises what it can spend and
    requires confirm_spend=True. Everything else here is capped by the budget
    that is already in place and needs no confirmation.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        name: New campaign name. Omit to keep.
        start_date: YYYY-MM-DD. Omit to keep. Cannot be changed once the
            campaign has started serving.
        end_date: YYYY-MM-DD. Omit to keep.
        search_network: Include Google search partners. Omit to keep.
        content_network: Include the Display network. Omit to keep. Turning
            this on for a search campaign is a classic way to waste budget.
        budget_resource_name: Move the campaign to a different budget, e.g.
            "customers/123/campaignBudgets/456". Omit to keep.
        confirm_spend: Required only when moving to a larger budget.

    Returns:
        What changed, and the campaign read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    if all(
        v is None
        for v in (
            name,
            start_date,
            end_date,
            search_network,
            content_network,
            budget_resource_name,
        )
    ):
        raise ValueError("Give at least one setting to change.")

    current = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.network_settings.target_search_network,
               campaign.network_settings.target_content_network,
               campaign.campaign_budget,
               campaign_budget.amount_micros
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not current:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")

    old_daily = _from_micros(
        int(current[0]["campaign_budget"]["amount_micros"])
    )

    if budget_resource_name:
        new_budget_id = budget_resource_name.rsplit("/", 1)[-1]
        new_budget = _read_back(
            customer_id,
            f"""
            SELECT campaign_budget.id, campaign_budget.amount_micros
            FROM campaign_budget WHERE campaign_budget.id = {new_budget_id}
            """,
        )
        if not new_budget:
            raise ValueError(
                f"No budget {new_budget_id} in account {customer_id}."
            )
        new_daily = _from_micros(
            int(new_budget[0]["campaign_budget"]["amount_micros"])
        )
        if new_daily > old_daily and not confirm_spend:
            raise SpendConfirmationRequired(
                f"Moving '{current[0]['campaign']['name']}' to this budget "
                f"raises its daily cap from {old_daily} to {new_daily} "
                f"(about {round((new_daily - old_daily) * 30.4, 2)} more per "
                f"month) in the account's currency. Show these numbers to the "
                f"user and call again with confirm_spend=True only after they "
                f"agree."
            )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = service.campaign_path(customer_id, campaign_id)

    paths = []
    if name is not None:
        campaign.name = name
        paths.append("name")
    if start_date is not None:
        campaign.start_date_time = f"{start_date} 00:00:00"
        paths.append("start_date_time")
    if end_date is not None:
        campaign.end_date_time = f"{end_date} 23:59:59"
        paths.append("end_date_time")
    if search_network is not None:
        campaign.network_settings.target_search_network = search_network
        paths.append("network_settings.target_search_network")
    if content_network is not None:
        campaign.network_settings.target_content_network = content_network
        paths.append("network_settings.target_content_network")
    if budget_resource_name is not None:
        campaign.campaign_budget = budget_resource_name
        paths.append("campaign_budget")

    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )
    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    return {
        "campaign_id": campaign_id,
        "changed": paths,
        "status_unchanged": current[0]["campaign"]["status"],
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.start_date_time, campaign.end_date_time,
                   campaign.network_settings.target_search_network,
                   campaign.network_settings.target_content_network,
                   campaign_budget.amount_micros
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        ),
    }


@campaigns_mcp.tool
def update_bid_strategy(
    customer_id: str,
    campaign_id: str,
    bidding_strategy: str,
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    impression_share_location: Optional[str] = None,
    impression_share_percent: Optional[float] = None,
    cpc_bid_ceiling: Optional[float] = None,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Changes a campaign's bidding strategy.

    On a PAUSED campaign this is free: it still cannot spend. On an ENABLED
    campaign it changes how money is spent right now, so it requires
    confirm_spend=True.

    Conversion-based strategies (MAXIMIZE_CONVERSIONS, TARGET_CPA,
    TARGET_ROAS) need working conversion tracking in the account. Without it
    the API rejects the change with CONVERSION_TRACKING_NOT_ENABLED.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        bidding_strategy: MANUAL_CPC, MAXIMIZE_CLICKS, MAXIMIZE_CONVERSIONS,
            TARGET_CPA, TARGET_ROAS or TARGET_IMPRESSION_SHARE.
        target_cpa: Required for TARGET_CPA, in the account's own currency.
        target_roas: Required for TARGET_ROAS, as a ratio (4.0 means 400%).
        impression_share_location: Required for TARGET_IMPRESSION_SHARE.
            ANYWHERE_ON_PAGE, TOP_OF_PAGE or ABSOLUTE_TOP_OF_PAGE.
        impression_share_percent: Required for TARGET_IMPRESSION_SHARE. The
            share of auctions to appear in, as a percentage 1-100.
        cpc_bid_ceiling: Maximum CPC, in the account's own currency.
            REQUIRED for TARGET_IMPRESSION_SHARE, which bids for position
            rather than for a conversion and will otherwise pay whatever the
            auction asks -- the usual reason an own-brand campaign costs
            several times what a brand click should. Optional for
            MAXIMIZE_CLICKS; rejected by the conversion-based strategies.
        confirm_spend: Required when the campaign is ENABLED.

    Returns:
        Old and new strategy, and the campaign read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    strategy = bidding_strategy.upper()

    current = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.bidding_strategy_type,
               campaign.bidding_strategy,
               campaign.bidding_strategy_system_status,
               campaign_budget.amount_micros
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not current:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")

    old_strategy = current[0]["campaign"]["bidding_strategy_type"]
    daily = _from_micros(int(current[0]["campaign_budget"]["amount_micros"]))

    # A campaign can point at a PORTFOLIO strategy, and then its bidding
    # settings are not on the campaign at all -- campaign.target_spend
    # reads 0 because the ceiling lives on the shared strategy. Writing
    # campaign-level fields anyway is refused by Google with
    # BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED pointed at
    # update.bidding_strategy, a field this operation never sets, which
    # is close to unreadable. Catch it here instead, while we know why.
    portfolio = str(current[0]["campaign"].get("bidding_strategy") or "")
    if portfolio:
        raise ValueError(
            f"Campaign {campaign_id} "
            f"('{current[0]['campaign'].get('name')}') does not carry its "
            f"own bidding strategy: it is on PORTFOLIO strategy "
            f"{portfolio.rsplit('/', 1)[-1]}, and that is where its "
            f"{old_strategy} settings live. This tool writes campaign-level "
            f"fields, which Google refuses for such a campaign "
            f"(BIDDING_STRATEGY_AND_BUDGET_MUST_BE_ALIGNED). To change the "
            f"shared ceiling, use "
            f"bidding_strategies.update_portfolio_bid_ceiling. To give this "
            f"campaign its own strategy instead, use "
            f"bidding_strategies.detach_campaign_from_portfolio, which "
            f"clears the portfolio and sets the new strategy in one "
            f"operation. bidding_strategies.list_bidding_strategies shows "
            f"which campaigns are in this position."
        )

    if _campaign_is_serving(current[0]) and not confirm_spend:
        raise SpendConfirmationRequired(
            f"'{current[0]['campaign']['name']}' is ENABLED and spending "
            f"{daily}/day, about {round(daily * 30.4, 2)}/month in the "
            f"account's currency. Changing its bidding from {old_strategy} to "
            f"{strategy} changes how that money is spent immediately. Show "
            f"this to the user and call again with confirm_spend=True only "
            f"after they agree."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = service.campaign_path(customer_id, campaign_id)

    paths = _bidding.apply_campaign_scheme(
        client,
        campaign,
        strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        impression_share_location=impression_share_location,
        impression_share_percent=impression_share_percent,
        cpc_bid_ceiling=cpc_bid_ceiling,
    )

    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )
    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    return {
        "campaign_id": campaign_id,
        "old_bid_strategy": old_strategy,
        "new_bid_strategy": strategy,
        "campaign_status": current[0]["campaign"]["status"],
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.bidding_strategy_type,
                   campaign.bidding_strategy,
                   campaign.bidding_strategy_system_status,
                   campaign.target_spend.cpc_bid_ceiling_micros,
                   campaign.target_impression_share.location,
                   campaign.target_impression_share.location_fraction_micros,
                   campaign.target_impression_share.cpc_bid_ceiling_micros,
                   campaign.maximize_conversions.target_cpa_micros
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        ),
    }


@campaigns_mcp.tool
def set_device_bid_modifiers(
    customer_id: str,
    campaign_id: str,
    modifiers: Dict[str, float],
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Sets per-device bid adjustments on a campaign.

    A modifier multiplies the bid on that device: 1.0 is no change, 1.2 bids
    20% more, 0.7 bids 30% less, 0 stops serving on that device entirely.
    Raising any modifier above its current value can raise cost per click, so
    it requires confirm_spend=True. Lowering is always allowed.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to adjust.
        modifiers: Device name to multiplier, e.g.
            {"MOBILE": 1.2, "DESKTOP": 0.8, "TABLET": 0}
            Valid devices: MOBILE, DESKTOP, TABLET, CONNECTED_TV.
        confirm_spend: Required when any modifier is being raised.

    Returns:
        The campaign's device modifiers, read back after the change.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    valid = {"MOBILE", "DESKTOP", "TABLET", "CONNECTED_TV"}
    cleaned = {}
    for device, value in modifiers.items():
        name = str(device).upper()
        if name not in valid:
            raise ValueError(f"Device must be one of {sorted(valid)}: {device}")
        value = float(value)
        if not 0 <= value <= 10:
            raise ValueError(
                f"Modifier for {name} must be between 0 and 10, got {value}."
            )
        cleaned[name] = value

    existing = _read_back(
        customer_id,
        f"""
        SELECT campaign_criterion.criterion_id,
               campaign_criterion.device.type,
               campaign_criterion.bid_modifier
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id}
          AND campaign_criterion.type = DEVICE
        """,
    )
    by_device = {}
    for row in existing:
        crit = row["campaign_criterion"]
        by_device[str(crit["device"]["type_"])] = crit

    raised = []
    for name, value in cleaned.items():
        old = by_device.get(name, {}).get("bid_modifier")
        old_value = 1.0 if old is None else float(old)
        if value > old_value:
            raised.append(f"{name} {old_value} -> {value}")

    if raised and not confirm_spend:
        raise SpendConfirmationRequired(
            "This raises bids on: "
            + "; ".join(raised)
            + ". Higher bids mean a higher cost per click, though the campaign "
            "budget still caps the daily total. Show this to the user and call "
            "again with confirm_spend=True only after they agree."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_service = utils.get_googleads_service("CampaignService")

    operations = []
    for name, value in cleaned.items():
        operation = client.get_type("CampaignCriterionOperation")
        if name in by_device:
            criterion = operation.update
            criterion.resource_name = service.campaign_criterion_path(
                customer_id, campaign_id, by_device[name]["criterion_id"]
            )
            criterion.bid_modifier = value
            client.copy_from(
                operation.update_mask,
                field_mask_pb2.FieldMask(paths=["bid_modifier"]),
            )
        else:
            criterion = operation.create
            criterion.campaign = campaign_service.campaign_path(
                customer_id, campaign_id
            )
            criterion.device.type_ = getattr(client.enums.DeviceEnum, name)
            criterion.bid_modifier = value
        operations.append(operation)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    return {
        "campaign_id": campaign_id,
        "applied": cleaned,
        "raised": raised,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.device.type,
                   campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = DEVICE
            """,
        ),
    }


@campaigns_mcp.tool
def set_campaign_proximity(
    customer_id: str,
    campaign_id: str,
    radius: float,
    street_address: Optional[str] = None,
    city_name: Optional[str] = None,
    postal_code: Optional[str] = None,
    country_code: str = "TR",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    radius_units: str = "KILOMETERS",
    bid_modifier: Optional[float] = None,
    presence_only: bool = True,
    replace_existing_locations: bool = False,
) -> Dict[str, Any]:
    """Targets a radius around a point -- the right shape for one shopfront.

    District and city targeting are drawn by administration, not by how far
    anyone will travel: a district can reach 15km inland while the street two
    minutes away sits in the next district and is missed entirely. For a
    single-location business a radius around the door matches real catchment.

    Layer several radii to bid by distance: call this once per ring with its
    own bid_modifier (0-3km at 1.2, 3-6km at 1.0, 6-10km at 0.75). Rings are
    separate criteria and Google applies the tightest matching one.

    `presence_only` also fixes the setting that quietly wastes local budgets:
    Google's default counts people merely INTERESTED in the area, so someone
    searching "maltepe berber" from another city still sees the ad. True
    restricts to people actually there, which is what a walk-in business
    wants.

    Give either an address (street_address + city_name, Google geocodes it)
    or latitude/longitude. Targeting never spends beyond the campaign budget,
    so this needs no confirmation.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to target.
        radius: How far from the point, in radius_units.
        street_address: e.g. "Rifki Tongsir Cd. No:113".
        city_name: e.g. "Maltepe".
        postal_code: e.g. "34841". Sharpens geocoding.
        country_code: Two-letter country, e.g. "TR".
        latitude: Use instead of an address, e.g. 40.9412.
        longitude: Use instead of an address, e.g. 29.1234.
        radius_units: KILOMETERS or MILES.
        bid_modifier: 1.2 bids 20% more inside this ring, 0.75 bids 25% less.
            Omit for no adjustment.
        presence_only: True targets people IN the area; False also targets
            people merely interested in it. True is right for a shopfront.
        replace_existing_locations: True removes the campaign's existing
            location and proximity criteria first. Use it when moving from
            district targeting to a radius, or the two stack and the wider
            one wins.

    Returns:
        The campaign's geo targeting, read back after the change.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    radius_units = radius_units.upper()
    if radius_units not in {"KILOMETERS", "MILES"}:
        raise ValueError("radius_units must be KILOMETERS or MILES.")
    if radius <= 0:
        raise ValueError("radius must be greater than zero.")

    has_point = latitude is not None and longitude is not None
    has_address = bool(street_address or city_name or postal_code)
    if not has_point and not has_address:
        raise ValueError(
            "Give either latitude and longitude, or an address "
            "(street_address / city_name / postal_code)."
        )
    if bid_modifier is not None and not 0.1 <= bid_modifier <= 10.0:
        raise ValueError("bid_modifier must be between 0.1 and 10.0.")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_service = utils.get_googleads_service("CampaignService")
    campaign_path = campaign_service.campaign_path(customer_id, campaign_id)

    removed = []
    if replace_existing_locations:
        existing = _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.resource_name,
                   campaign_criterion.type
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type IN (LOCATION, PROXIMITY)
            """,
        )
        removals = []
        for row in existing:
            criterion = row["campaign_criterion"]
            operation = client.get_type("CampaignCriterionOperation")
            operation.remove = criterion["resource_name"]
            removals.append(operation)
            removed.append(criterion["resource_name"])
        if removals:
            service.mutate_campaign_criteria(
                customer_id=customer_id, operations=removals
            )

    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = campaign_path
    criterion.proximity.radius = float(radius)
    criterion.proximity.radius_units = getattr(
        client.enums.ProximityRadiusUnitsEnum, radius_units
    )
    if has_point:
        criterion.proximity.geo_point.latitude_in_micro_degrees = int(
            round(latitude * 1_000_000)
        )
        criterion.proximity.geo_point.longitude_in_micro_degrees = int(
            round(longitude * 1_000_000)
        )
    else:
        if street_address:
            criterion.proximity.address.street_address = street_address
        if city_name:
            criterion.proximity.address.city_name = city_name
        if postal_code:
            criterion.proximity.address.postal_code = postal_code
        criterion.proximity.address.country_code = country_code.upper()
    if bid_modifier is not None:
        criterion.bid_modifier = float(bid_modifier)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=[operation]
    )

    # "Presence or interest" is Google's default and the single most common
    # reason a local campaign pays for clicks from other cities.
    geo_setting = None
    if presence_only is not None:
        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = campaign_path
        target_type = client.enums.PositiveGeoTargetTypeEnum
        campaign.geo_target_type_setting.positive_geo_target_type = (
            target_type.PRESENCE if presence_only
            else target_type.PRESENCE_OR_INTEREST
        )
        client.copy_from(
            campaign_operation.update_mask,
            field_mask_pb2.FieldMask(
                paths=["geo_target_type_setting.positive_geo_target_type"]
            ),
        )
        campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_operation]
        )
        geo_setting = "PRESENCE" if presence_only else "PRESENCE_OR_INTEREST"

    return {
        "campaign_id": campaign_id,
        "radius": radius,
        "radius_units": radius_units,
        "bid_modifier": bid_modifier,
        "positive_geo_target_type": geo_setting,
        "removed_criteria": removed,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.type,
                   campaign_criterion.bid_modifier,
                   campaign_criterion.negative,
                   campaign_criterion.proximity.radius,
                   campaign_criterion.proximity.radius_units,
                   campaign_criterion.proximity.address.city_name,
                   campaign_criterion.proximity.address.street_address,
                   campaign_criterion.location.geo_target_constant
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type IN (LOCATION, PROXIMITY)
            """,
        ),
    }


_AGE_RANGES = {
    "AGE_RANGE_18_24",
    "AGE_RANGE_25_34",
    "AGE_RANGE_35_44",
    "AGE_RANGE_45_54",
    "AGE_RANGE_55_64",
    "AGE_RANGE_65_UP",
    "AGE_RANGE_UNDETERMINED",
}

_GENDERS = {"MALE", "FEMALE", "UNDETERMINED"}


def _check_modifier(name: str, value: float) -> float:
    value = float(value)
    if not 0.1 <= value <= 10.0:
        raise ValueError(
            f"Bid modifier for {name} is {value}; Google accepts 0.1 to 10.0. "
            f"1.0 means no change, 1.2 bids 20% more, 0.8 bids 20% less."
        )
    return value


def _existing_criteria(
    customer_id: str, campaign_id: str, criterion_type: str, key_field: str
) -> Dict[str, Dict[str, Any]]:
    """Maps an enum value to the criterion already on the campaign.

    Demographic and location criteria cannot be created twice: a second
    create for the same age range, gender or location is rejected with
    CRITERION_ALREADY_EXISTS. So a bid modifier is a create the first time
    and an update every time after, and the caller cannot know which.
    """
    rows = _read_back(
        customer_id,
        f"""
        SELECT campaign_criterion.criterion_id,
               campaign_criterion.resource_name,
               campaign_criterion.negative,
               campaign_criterion.bid_modifier,
               campaign_criterion.{key_field}
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id}
          AND campaign_criterion.type = {criterion_type}
          AND campaign_criterion.status != 'REMOVED'
        """,
    )
    found: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        criterion = row.get("campaign_criterion", {})
        node = criterion
        for part in key_field.split("."):
            node = (node or {}).get(part) if isinstance(node, dict) else None
        if node:
            found[str(node)] = criterion
    return found


def _spend_gate(
    customer_id: str,
    campaign_id: str,
    raised: List[float],
    what: str,
    confirm_spend: bool,
) -> Dict[str, Any]:
    current = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign_budget.amount_micros
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not current:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")
    if raised and _campaign_is_serving(current[0]) and not confirm_spend:
        daily = _from_micros(
            int(current[0]["campaign_budget"]["amount_micros"])
        )
        raise SpendConfirmationRequired(
            f"'{current[0]['campaign']['name']}' is ENABLED and spending "
            f"{daily}/day in the account's currency. A {what} bid modifier "
            f"above 1.0 (highest here: {max(raised)}) raises cost per click "
            f"for those people straight away. Show this to the user and call "
            f"again with confirm_spend=True only after they agree."
        )
    return current[0]


@campaigns_mcp.tool
def set_demographic_bid_modifiers(
    customer_id: str,
    campaign_id: str,
    genders: Optional[Dict[str, float]] = None,
    age_ranges: Optional[Dict[str, float]] = None,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Bids more or less for an age range or gender, without excluding it.

    This is the tool to reach for when data says a group converts better --
    not exclude_demographics. A modifier keeps everyone eligible and only
    shifts what you pay; an exclusion throws the group away, and on Search
    that is usually a mistake because Google does not know most searchers'
    age or gender at all. Those unknowns land in AGE_RANGE_UNDETERMINED and
    UNDETERMINED, which are typically the largest buckets on the campaign,
    so bidding them down is bidding down most of your traffic.

    A modifier is a create the first time and an update afterwards; this
    tool works out which and does not fail on a second call.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        genders: Maps MALE, FEMALE or UNDETERMINED to a modifier, 0.1-10.0.
            1.2 bids 20% more, 0.8 bids 20% less, 1.0 is no change.
        age_ranges: Maps AGE_RANGE_18_24, AGE_RANGE_25_34, AGE_RANGE_35_44,
            AGE_RANGE_45_54, AGE_RANGE_55_64, AGE_RANGE_65_UP or
            AGE_RANGE_UNDETERMINED to a modifier, 0.1-10.0.
        confirm_spend: Required when the campaign is ENABLED and any modifier
            is above 1.0.

    Returns:
        What was created, what was updated, and the criteria read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    genders = {str(k).upper(): v for k, v in (genders or {}).items()}
    age_ranges = {str(k).upper(): v for k, v in (age_ranges or {}).items()}
    if not genders and not age_ranges:
        raise ValueError("Give genders, age_ranges, or both.")
    for value in genders:
        if value not in _GENDERS:
            raise ValueError(
                f"{value!r} is not a gender. Use one of {sorted(_GENDERS)}."
            )
    for value in age_ranges:
        if value not in _AGE_RANGES:
            raise ValueError(
                f"{value!r} is not an age range. Use one of "
                f"{sorted(_AGE_RANGES)}."
            )
    genders = {k: _check_modifier(k, v) for k, v in genders.items()}
    age_ranges = {k: _check_modifier(k, v) for k, v in age_ranges.items()}

    raised = [v for v in list(genders.values()) + list(age_ranges.values())
              if v > 1.0]
    _spend_gate(customer_id, campaign_id, raised, "demographic", confirm_spend)

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_path = utils.get_googleads_service(
        "CampaignService"
    ).campaign_path(customer_id, campaign_id)

    existing_gender = _existing_criteria(
        customer_id, campaign_id, "GENDER", "gender.type"
    )
    existing_age = _existing_criteria(
        customer_id, campaign_id, "AGE_RANGE", "age_range.type"
    )

    operations: List[Any] = []
    created: List[str] = []
    updated: List[str] = []
    skipped: List[str] = []

    def _plan(value: str, modifier: float, existing: Dict[str, Any], kind: str):
        row = existing.get(value)
        operation = client.get_type("CampaignCriterionOperation")
        if row is not None:
            if row.get("negative"):
                # An exclusion cannot also carry a bid modifier; changing it
                # here would silently widen reach.
                skipped.append(f"{value} (excluded; remove the exclusion first)")
                return
            criterion = operation.update
            criterion.resource_name = row["resource_name"]
            criterion.bid_modifier = modifier
            client.copy_from(
                operation.update_mask,
                field_mask_pb2.FieldMask(paths=["bid_modifier"]),
            )
            updated.append(value)
        else:
            criterion = operation.create
            criterion.campaign = campaign_path
            criterion.bid_modifier = modifier
            if kind == "GENDER":
                criterion.gender.type_ = getattr(
                    client.enums.GenderTypeEnum, value
                )
            else:
                criterion.age_range.type_ = getattr(
                    client.enums.AgeRangeTypeEnum, value
                )
            created.append(value)
        operations.append(operation)

    for value, modifier in genders.items():
        _plan(value, modifier, existing_gender, "GENDER")
    for value, modifier in age_ranges.items():
        _plan(value, modifier, existing_age, "AGE_RANGE")

    if operations:
        service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )

    result = {
        "campaign_id": campaign_id,
        "created": created,
        "updated": updated,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.type,
                   campaign_criterion.negative,
                   campaign_criterion.bid_modifier,
                   campaign_criterion.gender.type,
                   campaign_criterion.age_range.type
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type IN (GENDER, AGE_RANGE)
              AND campaign_criterion.status != 'REMOVED'
            """,
        ),
    }
    if skipped:
        result["skipped"] = skipped
    return result


@campaigns_mcp.tool
def set_location_bid_modifiers(
    customer_id: str,
    campaign_id: str,
    location_bids: Dict[str, float],
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Bids more or less inside named locations within what is already targeted.

    Use this to lean towards the places that actually buy without cutting the
    rest loose. Adding a district that sits inside an already-targeted city
    does not widen reach -- the city already covered it -- it only makes that
    district biddable on its own.

    Get the ids from find_location_ids first; never guess one. A location
    already on the campaign is updated rather than created, so calling this
    twice is safe.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        location_bids: Maps geo target constant ids to modifiers, 0.1-10.0.
            For example {"9198902": 1.25} bids 25% more in that district.
        confirm_spend: Required when the campaign is ENABLED and any modifier
            is above 1.0.

    Returns:
        What was created, what was updated, and the criteria read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    location_bids = {
        _clean_id(str(k)): _check_modifier(str(k), v)
        for k, v in (location_bids or {}).items()
    }
    if not location_bids:
        raise ValueError("Give at least one location id and a modifier.")

    raised = [v for v in location_bids.values() if v > 1.0]
    _spend_gate(customer_id, campaign_id, raised, "location", confirm_spend)

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_path = utils.get_googleads_service(
        "CampaignService"
    ).campaign_path(customer_id, campaign_id)

    existing = _existing_criteria(
        customer_id, campaign_id, "LOCATION", "location.geo_target_constant"
    )

    operations: List[Any] = []
    created: List[str] = []
    updated: List[str] = []
    skipped: List[str] = []

    for location_id, modifier in location_bids.items():
        row = existing.get(f"geoTargetConstants/{location_id}")
        operation = client.get_type("CampaignCriterionOperation")
        if row is not None:
            if row.get("negative"):
                skipped.append(
                    f"{location_id} (excluded; remove the exclusion first)"
                )
                continue
            criterion = operation.update
            criterion.resource_name = row["resource_name"]
            criterion.bid_modifier = modifier
            client.copy_from(
                operation.update_mask,
                field_mask_pb2.FieldMask(paths=["bid_modifier"]),
            )
            updated.append(location_id)
        else:
            criterion = operation.create
            criterion.campaign = campaign_path
            criterion.bid_modifier = modifier
            criterion.location.geo_target_constant = (
                f"geoTargetConstants/{location_id}"
            )
            created.append(location_id)
        operations.append(operation)

    if operations:
        service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )

    result = {
        "campaign_id": campaign_id,
        "created": created,
        "updated": updated,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.negative,
                   campaign_criterion.bid_modifier,
                   campaign_criterion.location.geo_target_constant
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = LOCATION
              AND campaign_criterion.status != 'REMOVED'
            """,
        ),
    }
    if skipped:
        result["skipped"] = skipped
    return result
