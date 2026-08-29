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

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

campaigns_mcp = FastMCP("campaigns")

# Google Ads expresses money in "micros": 1 unit of currency = 1_000_000.
_MICROS = 1_000_000

_DAYS = {
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
    "FRIDAY", "SATURDAY", "SUNDAY",
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
            rows.append(utils.format_output_value(row))
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
        client.get_type("FieldMask")(paths=["amount_micros"]),
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

    strategy = bidding_strategy.upper()
    if strategy == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False
    elif strategy == "MAXIMIZE_CLICKS":
        campaign.target_spend.CopyFrom(client.get_type("TargetSpend"))
    elif strategy == "MAXIMIZE_CONVERSIONS":
        campaign.maximize_conversions.CopyFrom(
            client.get_type("MaximizeConversions")
        )
    elif strategy == "TARGET_CPA":
        if target_cpa is None:
            raise ValueError("TARGET_CPA requires target_cpa.")
        campaign.target_cpa.target_cpa_micros = _to_micros(target_cpa)
    elif strategy == "TARGET_ROAS":
        if target_roas is None:
            raise ValueError("TARGET_ROAS requires target_roas.")
        campaign.target_roas.target_roas = target_roas
    else:
        raise ValueError(f"Unknown bidding strategy: {bidding_strategy}")

    if start_date:
        campaign.start_date = start_date.replace("-", "")
    if end_date:
        campaign.end_date = end_date.replace("-", "")

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
                   campaign.start_date, campaign.end_date,
                   campaign_budget.amount_micros
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
    """Enables, pauses or removes a campaign.

    ENABLED starts real spending and requires confirm_spend=True.
    REMOVED is not reversible and requires confirm_remove=True.
    PAUSED always works without confirmation — stopping spend is always safe.

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
    operation.update.resource_name = service.campaign_path(
        customer_id, campaign_id
    )
    operation.update.status = getattr(
        client.enums.CampaignStatusEnum, status
    )
    client.copy_from(
        operation.update_mask, client.get_type("FieldMask")(paths=["status"])
    )
    service.mutate_campaigns(customer_id=customer_id, operations=[operation])

    return {
        "campaign_id": campaign_id,
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
# Ad scheduling
# --------------------------------------------------------------------------


@campaigns_mcp.tool
def set_campaign_ad_schedule(
    customer_id: str,
    campaign_id: str,
    schedule: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Sets when a campaign is allowed to serve, in the ACCOUNT'S time zone.

    A campaign with no ad schedule runs all week. Once any schedule entry
    exists, the campaign serves ONLY inside the entries you add — so add every
    day you want covered in one call.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to schedule.
        schedule: One dict per time block, e.g.
            [{"day": "MONDAY", "start_hour": 9, "end_hour": 23},
             {"day": "TUESDAY", "start_hour": 9, "end_hour": 23}]
            Hours are 0-24. Minutes may be 0, 15, 30 or 45 via the optional
            "start_minute"/"end_minute" keys; end_hour 24 means midnight.

    Returns:
        The campaign's full ad schedule, read back after the change.
    """
    customer_id = _clean_id(customer_id)
    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_service = utils.get_googleads_service("CampaignService")

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
        operations.append(operation)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    return {
        "campaign_id": campaign_id,
        "added": len(operations),
        "note": (
            "The campaign now serves ONLY during these blocks, in the "
            "account's time zone."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.ad_schedule.day_of_week,
                   campaign_criterion.ad_schedule.start_hour,
                   campaign_criterion.ad_schedule.end_hour
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = AD_SCHEDULE
            """,
        ),
    }
