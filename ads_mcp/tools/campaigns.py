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
               campaign_budget.amount_micros
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not current:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")

    old_strategy = current[0]["campaign"]["bidding_strategy_type"]
    daily = _from_micros(int(current[0]["campaign_budget"]["amount_micros"]))

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

    # The update mask has to name a LEAF field: masking the strategy message
    # itself is rejected with FIELD_HAS_SUBFIELDS. For the strategies that
    # carry no settings, mask a leaf we deliberately leave unset, which both
    # selects the strategy and clears any stale value on it.
    if strategy == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False
        paths = ["manual_cpc.enhanced_cpc_enabled"]
    elif strategy == "MAXIMIZE_CLICKS":
        client.copy_from(
            campaign.target_spend, client.get_type("TargetSpend")
        )
        if cpc_bid_ceiling is not None:
            campaign.target_spend.cpc_bid_ceiling_micros = _to_micros(
                cpc_bid_ceiling
            )
        paths = ["target_spend.cpc_bid_ceiling_micros"]
    elif strategy == "MAXIMIZE_CONVERSIONS":
        client.copy_from(
            campaign.maximize_conversions,
            client.get_type("MaximizeConversions"),
        )
        paths = ["maximize_conversions.target_cpa_micros"]
    elif strategy == "TARGET_CPA":
        if target_cpa is None:
            raise ValueError("TARGET_CPA requires target_cpa.")
        # Target CPA is no longer a strategy of its own: it is Maximize
        # Conversions with a target. The standalone TargetCpa is rejected on
        # Search campaigns with OPERATION_NOT_PERMITTED_FOR_CONTEXT.
        campaign.maximize_conversions.target_cpa_micros = _to_micros(
            target_cpa
        )
        paths = ["maximize_conversions.target_cpa_micros"]
    elif strategy == "TARGET_ROAS":
        if target_roas is None:
            raise ValueError("TARGET_ROAS requires target_roas.")
        # Likewise: Target ROAS is Maximize Conversion Value with a target.
        campaign.maximize_conversion_value.target_roas = target_roas
        paths = ["maximize_conversion_value.target_roas"]
    elif strategy == "TARGET_IMPRESSION_SHARE":
        if (
            impression_share_location is None
            or impression_share_percent is None
            or cpc_bid_ceiling is None
        ):
            raise ValueError(
                "TARGET_IMPRESSION_SHARE requires impression_share_location, "
                "impression_share_percent and cpc_bid_ceiling. The ceiling is "
                "required by Google, not by this tool: without it the "
                "strategy pays whatever holding the position costs."
            )
        location = impression_share_location.upper()
        if location not in _IMPRESSION_SHARE_LOCATIONS:
            raise ValueError(
                f"impression_share_location must be one of "
                f"{sorted(_IMPRESSION_SHARE_LOCATIONS)}, not "
                f"{impression_share_location!r}."
            )
        if not 1 <= impression_share_percent <= 100:
            raise ValueError(
                "impression_share_percent is a percentage and must be "
                "between 1 and 100."
            )
        campaign.target_impression_share.location = getattr(
            client.enums.TargetImpressionShareLocationEnum, location
        )
        # Google expresses this share in micros of a whole, so 100% is
        # 1_000_000 and 1% is 10_000 -- not the money micros used elsewhere.
        campaign.target_impression_share.location_fraction_micros = int(
            round(impression_share_percent * 10_000)
        )
        campaign.target_impression_share.cpc_bid_ceiling_micros = _to_micros(
            cpc_bid_ceiling
        )
        paths = [
            "target_impression_share.location",
            "target_impression_share.location_fraction_micros",
            "target_impression_share.cpc_bid_ceiling_micros",
        ]
    else:
        raise ValueError(f"Unknown bidding strategy: {bidding_strategy}")

    if cpc_bid_ceiling is not None and strategy in (
        "MANUAL_CPC",
        "MAXIMIZE_CONVERSIONS",
        "TARGET_CPA",
        "TARGET_ROAS",
    ):
        raise ValueError(
            f"{strategy} has no cpc_bid_ceiling. Only MAXIMIZE_CLICKS and "
            f"TARGET_IMPRESSION_SHARE accept one."
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
