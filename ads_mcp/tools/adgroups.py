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

"""Write tools for ad groups, responsive search ads and keywords.

Spend is gated at the CAMPAIGN level: a campaign created by this server is
PAUSED and cannot serve, so ad groups, ads and keywords underneath it are
created ENABLED and still spend nothing. That keeps one clear gate instead of
several confusing ones -- enabling the campaign is the single decision that
starts money moving.

Platform limits that silently truncate or reject content are enforced here,
before the call leaves the process.
"""

from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

import ads_mcp.utils as utils

adgroups_mcp = FastMCP("adgroups")

_MICROS = 1_000_000

# Responsive search ad limits, per Google Ads.
_MAX_HEADLINE_CHARS = 30
_MAX_DESCRIPTION_CHARS = 90
_MAX_PATH_CHARS = 15
_MIN_HEADLINES = 3
_MAX_HEADLINES = 15
_MIN_DESCRIPTIONS = 2
_MAX_DESCRIPTIONS = 4

_MATCH_TYPES = {"EXACT", "PHRASE", "BROAD"}


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _to_micros(amount: float) -> int:
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return int(round(amount * _MICROS))


def _read_back(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    rows = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            rows.append(utils.format_output_value(row))
    return rows


# --------------------------------------------------------------------------
# Ad groups
# --------------------------------------------------------------------------


@adgroups_mcp.tool
def create_ad_group(
    customer_id: str,
    campaign_id: str,
    name: str,
    cpc_bid: Optional[float] = None,
) -> Dict[str, Any]:
    """Creates an ad group inside a campaign.

    Created ENABLED, which is safe: the parent campaign is PAUSED, so nothing
    serves until the campaign itself is enabled.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign this ad group belongs to.
        name: Ad group name. Must be unique within the campaign.
        cpc_bid: Optional max CPC in the account's own currency. Only used by
            manual bidding; automated strategies ignore it.

    Returns:
        The ad group's id and its read-back state.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupService")
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("AdGroupOperation")
    ad_group = operation.create
    ad_group.name = name
    ad_group.campaign = campaign_service.campaign_path(customer_id, campaign_id)
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    if cpc_bid is not None:
        ad_group.cpc_bid_micros = _to_micros(cpc_bid)

    response = service.mutate_ad_groups(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    ad_group_id = resource_name.rsplit("/", 1)[-1]

    return {
        "ad_group_id": ad_group_id,
        "resource_name": resource_name,
        "note": "Enabled, but the parent campaign is paused so nothing serves.",
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group.id, ad_group.name, ad_group.status,
                   ad_group.cpc_bid_micros, campaign.id, campaign.status
            FROM ad_group WHERE ad_group.id = {ad_group_id}
            """,
        ),
    }


@adgroups_mcp.tool
def set_ad_group_status(
    customer_id: str,
    ad_group_id: str,
    status: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Enables, pauses or removes an ad group.

    Unlike a campaign, enabling an ad group does not by itself start spending
    -- the campaign gate still applies -- so ENABLED needs no confirmation.
    REMOVED is not reversible and requires confirm_remove=True.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group to change.
        status: ENABLED, PAUSED or REMOVED.
        confirm_remove: Required for REMOVED.

    Returns:
        The ad group's state read back after the change.
    """
    customer_id = _clean_id(customer_id)
    status = status.upper()
    if status not in {"ENABLED", "PAUSED", "REMOVED"}:
        raise ValueError("status must be ENABLED, PAUSED or REMOVED.")
    if status == "REMOVED" and not confirm_remove:
        raise ValueError(
            "Removing an ad group also removes its ads and keywords, and is "
            "not reversible. Name what will be lost, then call again with "
            "confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    operation.update.resource_name = service.ad_group_path(
        customer_id, _clean_id(ad_group_id)
    )
    operation.update.status = getattr(client.enums.AdGroupStatusEnum, status)
    client.copy_from(
        operation.update_mask, client.get_type("FieldMask")(paths=["status"])
    )
    service.mutate_ad_groups(customer_id=customer_id, operations=[operation])

    return {
        "ad_group_id": ad_group_id,
        "new_status": status,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group.id, ad_group.name, ad_group.status
            FROM ad_group WHERE ad_group.id = {_clean_id(ad_group_id)}
            """,
        ),
    }


# --------------------------------------------------------------------------
# Responsive search ads
# --------------------------------------------------------------------------


@adgroups_mcp.tool
def create_responsive_search_ad(
    customer_id: str,
    ad_group_id: str,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    path1: Optional[str] = None,
    path2: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a responsive search ad.

    Google's character limits are checked here BEFORE the call, so a too-long
    headline fails loudly with the offending text instead of being rejected
    with an opaque API error.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group to put the ad in.
        final_url: Landing page URL, including https://.
        headlines: 3-15 headlines, each at most 30 characters. Google mixes
            and matches them, so each must stand on its own.
        descriptions: 2-4 descriptions, each at most 90 characters.
        path1: Optional display-URL path segment, at most 15 characters.
        path2: Optional second path segment, at most 15 characters.

    Returns:
        The ad's id and its read-back state.
    """
    customer_id = _clean_id(customer_id)
    ad_group_id = _clean_id(ad_group_id)

    if not _MIN_HEADLINES <= len(headlines) <= _MAX_HEADLINES:
        raise ValueError(
            f"Need {_MIN_HEADLINES}-{_MAX_HEADLINES} headlines, got "
            f"{len(headlines)}."
        )
    if not _MIN_DESCRIPTIONS <= len(descriptions) <= _MAX_DESCRIPTIONS:
        raise ValueError(
            f"Need {_MIN_DESCRIPTIONS}-{_MAX_DESCRIPTIONS} descriptions, got "
            f"{len(descriptions)}."
        )
    for text in headlines:
        if len(text) > _MAX_HEADLINE_CHARS:
            raise ValueError(
                f"Headline is {len(text)} characters, limit is "
                f"{_MAX_HEADLINE_CHARS}: {text!r}"
            )
    for text in descriptions:
        if len(text) > _MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"Description is {len(text)} characters, limit is "
                f"{_MAX_DESCRIPTION_CHARS}: {text!r}"
            )
    for label, value in (("path1", path1), ("path2", path2)):
        if value and len(value) > _MAX_PATH_CHARS:
            raise ValueError(
                f"{label} is {len(value)} characters, limit is "
                f"{_MAX_PATH_CHARS}: {value!r}"
            )
    if not final_url.startswith(("http://", "https://")):
        raise ValueError("final_url must start with http:// or https://")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupAdService")
    ad_group_service = utils.get_googleads_service("AdGroupService")

    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = ad_group_service.ad_group_path(
        customer_id, ad_group_id
    )
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(final_url)

    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad_group_ad.ad.responsive_search_ad.headlines.append(asset)
    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad_group_ad.ad.responsive_search_ad.descriptions.append(asset)
    if path1:
        ad_group_ad.ad.responsive_search_ad.path1 = path1
    if path2:
        ad_group_ad.ad.responsive_search_ad.path2 = path2

    response = service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    ad_id = resource_name.rsplit("~", 1)[-1]

    return {
        "ad_id": ad_id,
        "resource_name": resource_name,
        "headlines": len(headlines),
        "descriptions": len(descriptions),
        "note": (
            "The ad goes to Google review before it can serve. The parent "
            "campaign is still the spend gate."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group_ad.ad.id, ad_group_ad.status,
                   ad_group_ad.policy_summary.approval_status,
                   ad_group_ad.ad.final_urls
            FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id}
            """,
        ),
    }


# --------------------------------------------------------------------------
# Keywords
# --------------------------------------------------------------------------


@adgroups_mcp.tool
def add_keywords(
    customer_id: str,
    ad_group_id: str,
    keywords: List[Dict[str, Any]],
    default_match_type: str = "PHRASE",
) -> Dict[str, Any]:
    """Adds keywords to an ad group.

    BROAD match reaches the widest and wastes the most; it is never the
    default here. Choose it deliberately, and pair it with negative keywords.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group to add keywords to.
        keywords: One dict per keyword, e.g.
            [{"text": "tus hazirlik kursu", "match_type": "PHRASE"},
             {"text": "tus kampi", "cpc_bid": 12.5}]
            "match_type" defaults to default_match_type; "cpc_bid" is optional
            and in the account's own currency.
        default_match_type: EXACT, PHRASE or BROAD. Defaults to PHRASE.

    Returns:
        How many were added, and the ad group's keywords read back.
    """
    customer_id = _clean_id(customer_id)
    ad_group_id = _clean_id(ad_group_id)
    default_match_type = default_match_type.upper()
    if default_match_type not in _MATCH_TYPES:
        raise ValueError(f"match type must be one of {sorted(_MATCH_TYPES)}")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupCriterionService")
    ad_group_service = utils.get_googleads_service("AdGroupService")

    operations = []
    for entry in keywords:
        text = str(entry["text"]).strip()
        if not text:
            raise ValueError("A keyword's text cannot be empty.")
        match_type = str(
            entry.get("match_type", default_match_type)
        ).upper()
        if match_type not in _MATCH_TYPES:
            raise ValueError(
                f"Unknown match type {match_type!r} for keyword {text!r}."
            )

        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = ad_group_service.ad_group_path(
            customer_id, ad_group_id
        )
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.keyword.text = text
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, match_type
        )
        if entry.get("cpc_bid") is not None:
            criterion.cpc_bid_micros = _to_micros(float(entry["cpc_bid"]))
        operations.append(operation)

    service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=operations
    )

    return {
        "ad_group_id": ad_group_id,
        "added": len(operations),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   ad_group_criterion.status,
                   ad_group_criterion.negative
            FROM ad_group_criterion
            WHERE ad_group.id = {ad_group_id}
              AND ad_group_criterion.type = KEYWORD
            """,
        ),
    }


@adgroups_mcp.tool
def add_negative_keywords(
    customer_id: str,
    keywords: List[str],
    campaign_id: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    match_type: str = "PHRASE",
) -> Dict[str, Any]:
    """Adds negative keywords at campaign or ad group level.

    Negatives only ever REDUCE spend, so they need no confirmation. Campaign
    level is usually right for terms that are wrong for the whole account
    (free, jobs, torrent); ad group level for terms that belong to a sibling
    ad group.

    Args:
        customer_id: The Google Ads account id.
        keywords: The terms to exclude.
        campaign_id: Add at campaign level. Give this OR ad_group_id.
        ad_group_id: Add at ad group level. Give this OR campaign_id.
        match_type: EXACT, PHRASE or BROAD. Defaults to PHRASE.

    Returns:
        How many were added, and where.
    """
    customer_id = _clean_id(customer_id)
    if bool(campaign_id) == bool(ad_group_id):
        raise ValueError(
            "Give exactly one of campaign_id or ad_group_id."
        )
    match_type = match_type.upper()
    if match_type not in _MATCH_TYPES:
        raise ValueError(f"match type must be one of {sorted(_MATCH_TYPES)}")

    client = utils.get_googleads_client()
    operations = []

    if campaign_id:
        campaign_id = _clean_id(campaign_id)
        service = utils.get_googleads_service("CampaignCriterionService")
        campaign_service = utils.get_googleads_service("CampaignService")
        for text in keywords:
            operation = client.get_type("CampaignCriterionOperation")
            criterion = operation.create
            criterion.campaign = campaign_service.campaign_path(
                customer_id, campaign_id
            )
            criterion.negative = True
            criterion.keyword.text = text
            criterion.keyword.match_type = getattr(
                client.enums.KeywordMatchTypeEnum, match_type
            )
            operations.append(operation)
        service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        scope, scope_id = "campaign", campaign_id
    else:
        ad_group_id = _clean_id(ad_group_id)
        service = utils.get_googleads_service("AdGroupCriterionService")
        ad_group_service = utils.get_googleads_service("AdGroupService")
        for text in keywords:
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = ad_group_service.ad_group_path(
                customer_id, ad_group_id
            )
            criterion.negative = True
            criterion.keyword.text = text
            criterion.keyword.match_type = getattr(
                client.enums.KeywordMatchTypeEnum, match_type
            )
            operations.append(operation)
        service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        scope, scope_id = "ad_group", ad_group_id

    return {
        "scope": scope,
        "scope_id": scope_id,
        "added": len(operations),
        "match_type": match_type,
    }


@adgroups_mcp.tool
def remove_keywords(
    customer_id: str,
    ad_group_id: str,
    criterion_ids: List[str],
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Removes keywords from an ad group.

    Removing a keyword discards its history and is not reversible, so this
    requires confirm_remove=True. Pausing a keyword is usually the better
    move; only remove when the user has asked for it.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group the keywords belong to.
        criterion_ids: Criterion ids from add_keywords or a search query.
        confirm_remove: Must be True. Name what will be lost first.

    Returns:
        How many were removed.
    """
    customer_id = _clean_id(customer_id)
    ad_group_id = _clean_id(ad_group_id)
    if not confirm_remove:
        raise ValueError(
            f"Removing {len(criterion_ids)} keyword(s) discards their history "
            f"and is not reversible. Consider pausing instead. To proceed, "
            f"call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupCriterionService")

    operations = []
    for criterion_id in criterion_ids:
        operation = client.get_type("AdGroupCriterionOperation")
        operation.remove = service.ad_group_criterion_path(
            customer_id, ad_group_id, _clean_id(criterion_id)
        )
        operations.append(operation)

    service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=operations
    )
    return {"ad_group_id": ad_group_id, "removed": len(operations)}
