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

import proto

from fastmcp import FastMCP
from google.protobuf import field_mask_pb2

import ads_mcp.utils as utils
from ads_mcp.tools.campaigns import SpendConfirmationRequired

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


def _from_micros(micros: int) -> float:
    return round((micros or 0) / _MICROS, 2)


def _read_back(customer_id: str, query: str) -> List[Dict[str, Any]]:
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
    resource_name = service.ad_group_path(customer_id, _clean_id(ad_group_id))

    if status == "REMOVED":
        operation.remove = resource_name
    else:
        operation.update.resource_name = resource_name
        operation.update.status = getattr(
            client.enums.AdGroupStatusEnum, status
        )
        client.copy_from(
            operation.update_mask, field_mask_pb2.FieldMask(paths=["status"])
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


# --------------------------------------------------------------------------
# Bids and status changes
#
# Raising a bid raises what the account can spend, so it goes through the same
# confirm_spend gate as raising a budget. Lowering a bid, pausing anything, and
# every read are always free.
# --------------------------------------------------------------------------


@adgroups_mcp.tool
def update_ad_group_bid(
    customer_id: str,
    ad_group_id: str,
    cpc_bid: float,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Changes an ad group's max CPC bid.

    RAISING a bid increases what the account can spend per click and requires
    confirm_spend=True. Lowering it is allowed without confirmation.

    Only manual bidding uses this value; automated strategies ignore it.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group to change.
        cpc_bid: New max CPC in the account's own currency. Do not convert.
        confirm_spend: Must be True when the new bid is higher than the
            current one. Pass True only after the user has agreed.

    Returns:
        Old and new bid, read back from the API.
    """
    customer_id = _clean_id(customer_id)
    ad_group_id = _clean_id(ad_group_id)

    current = _read_back(
        customer_id,
        f"""
        SELECT ad_group.id, ad_group.name, ad_group.cpc_bid_micros
        FROM ad_group WHERE ad_group.id = {ad_group_id}
        """,
    )
    if not current:
        raise ValueError(f"No ad group {ad_group_id} in account {customer_id}.")

    old_micros = int(current[0]["ad_group"].get("cpc_bid_micros") or 0)
    old_bid = _from_micros(old_micros)
    new_micros = _to_micros(cpc_bid)

    if new_micros > old_micros and not confirm_spend:
        raise SpendConfirmationRequired(
            f"This raises the max CPC for "
            f"'{current[0]['ad_group']['name']}' from {old_bid} to {cpc_bid} "
            f"in the account's currency, so every click can cost more. Show "
            f"these numbers to the user and call again with confirm_spend=True "
            f"only after they agree."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    operation.update.resource_name = service.ad_group_path(
        customer_id, ad_group_id
    )
    operation.update.cpc_bid_micros = new_micros
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(paths=["cpc_bid_micros"]),
    )
    service.mutate_ad_groups(customer_id=customer_id, operations=[operation])

    return {
        "ad_group_id": ad_group_id,
        "old_cpc_bid": old_bid,
        "new_cpc_bid": cpc_bid,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group.id, ad_group.cpc_bid_micros
            FROM ad_group WHERE ad_group.id = {ad_group_id}
            """,
        ),
    }


@adgroups_mcp.tool
def set_keyword_status(
    customer_id: str,
    ad_group_id: str,
    criterion_ids: List[str],
    status: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Enables, pauses or removes keywords.

    PAUSED is the safe way to stop a keyword: it stops serving but keeps its
    history, so it can be turned back on. Prefer this over remove_keywords.
    ENABLED needs no confirmation -- the campaign is still the spend gate.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group the keywords belong to.
        criterion_ids: Criterion ids, from add_keywords or a search query.
        status: ENABLED, PAUSED or REMOVED.
        confirm_remove: Required for REMOVED, which discards history.

    Returns:
        The keywords' state read back after the change.
    """
    customer_id = _clean_id(customer_id)
    ad_group_id = _clean_id(ad_group_id)
    status = status.upper()
    if status not in {"ENABLED", "PAUSED", "REMOVED"}:
        raise ValueError("status must be ENABLED, PAUSED or REMOVED.")
    if status == "REMOVED" and not confirm_remove:
        raise ValueError(
            f"Removing {len(criterion_ids)} keyword(s) discards their history "
            f"and is not reversible. Pausing keeps the history and can be "
            f"undone. To remove anyway, call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupCriterionService")

    operations = []
    for criterion_id in criterion_ids:
        operation = client.get_type("AdGroupCriterionOperation")
        resource_name = service.ad_group_criterion_path(
            customer_id, ad_group_id, _clean_id(criterion_id)
        )
        if status == "REMOVED":
            operation.remove = resource_name
        else:
            operation.update.resource_name = resource_name
            operation.update.status = getattr(
                client.enums.AdGroupCriterionStatusEnum, status
            )
            client.copy_from(
                operation.update_mask,
                field_mask_pb2.FieldMask(paths=["status"]),
            )
        operations.append(operation)

    service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=operations
    )

    ids = ",".join(_clean_id(c) for c in criterion_ids)
    return {
        "ad_group_id": ad_group_id,
        "new_status": status,
        "changed": len(operations),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   ad_group_criterion.status
            FROM ad_group_criterion
            WHERE ad_group_criterion.criterion_id IN ({ids})
              AND ad_group.id = {ad_group_id}
            """,
        ),
    }


@adgroups_mcp.tool
def update_keyword(
    customer_id: str,
    ad_group_id: str,
    criterion_id: str,
    cpc_bid: Optional[float] = None,
    match_type: Optional[str] = None,
    confirm_spend: bool = False,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Changes a keyword's bid, its match type, or both.

    The two changes are not alike:

      * A BID change is an in-place update. Raising it needs confirm_spend.
      * A MATCH TYPE change is not possible in place -- Google treats the
        keyword text and match type as the criterion's identity. Changing it
        means removing the keyword and adding it back, which DISCARDS ITS
        HISTORY and gives it a new criterion id, so it needs confirm_remove.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group the keyword belongs to.
        criterion_id: The keyword to change.
        cpc_bid: New max CPC in the account's own currency. Omit to keep.
        match_type: EXACT, PHRASE or BROAD. Omit to keep.
        confirm_spend: Required when raising the bid.
        confirm_remove: Required when changing the match type, because the
            keyword's history is lost.

    Returns:
        What changed, and the keyword read back. When the match type changed,
        `new_criterion_id` differs from the one passed in.
    """
    customer_id = _clean_id(customer_id)
    ad_group_id = _clean_id(ad_group_id)
    criterion_id = _clean_id(criterion_id)

    if cpc_bid is None and match_type is None:
        raise ValueError("Give cpc_bid, match_type, or both.")
    if match_type is not None:
        match_type = match_type.upper()
        if match_type not in _MATCH_TYPES:
            raise ValueError(
                f"match_type must be one of {sorted(_MATCH_TYPES)}"
            )

    current = _read_back(
        customer_id,
        f"""
        SELECT ad_group_criterion.criterion_id,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               ad_group_criterion.status,
               ad_group_criterion.cpc_bid_micros
        FROM ad_group_criterion
        WHERE ad_group_criterion.criterion_id = {criterion_id}
          AND ad_group.id = {ad_group_id}
        """,
    )
    if not current:
        raise ValueError(
            f"No keyword {criterion_id} in ad group {ad_group_id}."
        )

    row = current[0]["ad_group_criterion"]
    text = row["keyword"]["text"]
    old_match = row["keyword"]["match_type"]
    old_micros = int(row.get("cpc_bid_micros") or 0)
    old_bid = _from_micros(old_micros)

    if cpc_bid is not None:
        new_micros = _to_micros(cpc_bid)
        if new_micros > old_micros and not confirm_spend:
            raise SpendConfirmationRequired(
                f"This raises the max CPC for '{text}' from {old_bid} to "
                f"{cpc_bid} in the account's currency, so every click can "
                f"cost more. Show these numbers to the user and call again "
                f"with confirm_spend=True only after they agree."
            )

    changing_match = match_type is not None and match_type != old_match
    if changing_match and not confirm_remove:
        raise ValueError(
            f"Google cannot change a keyword's match type in place. Moving "
            f"'{text}' from {old_match} to {match_type} means removing it and "
            f"adding it back: its performance history and criterion id are "
            f"lost. To proceed, call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupCriterionService")
    ad_group_service = utils.get_googleads_service("AdGroupService")
    ad_group_path = ad_group_service.ad_group_path(customer_id, ad_group_id)

    if not changing_match:
        # Bid-only change: a plain in-place update.
        operation = client.get_type("AdGroupCriterionOperation")
        operation.update.resource_name = service.ad_group_criterion_path(
            customer_id, ad_group_id, criterion_id
        )
        operation.update.cpc_bid_micros = _to_micros(cpc_bid)
        client.copy_from(
            operation.update_mask,
            field_mask_pb2.FieldMask(paths=["cpc_bid_micros"]),
        )
        service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=[operation]
        )
        new_criterion_id = criterion_id
    else:
        # Match type change: remove, then re-create with the new match type.
        remove_op = client.get_type("AdGroupCriterionOperation")
        remove_op.remove = service.ad_group_criterion_path(
            customer_id, ad_group_id, criterion_id
        )

        create_op = client.get_type("AdGroupCriterionOperation")
        criterion = create_op.create
        criterion.ad_group = ad_group_path
        criterion.keyword.text = text
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, match_type
        )
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        bid_micros = _to_micros(cpc_bid) if cpc_bid is not None else old_micros
        if bid_micros:
            criterion.cpc_bid_micros = bid_micros

        response = service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=[remove_op, create_op]
        )
        new_criterion_id = response.results[-1].resource_name.rsplit("~", 1)[-1]

    return {
        "ad_group_id": ad_group_id,
        "keyword": text,
        "old_criterion_id": criterion_id,
        "new_criterion_id": new_criterion_id,
        "old_match_type": old_match,
        "new_match_type": match_type or old_match,
        "old_cpc_bid": old_bid,
        "new_cpc_bid": cpc_bid if cpc_bid is not None else old_bid,
        "history_discarded": changing_match,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   ad_group_criterion.status,
                   ad_group_criterion.cpc_bid_micros
            FROM ad_group_criterion
            WHERE ad_group_criterion.criterion_id = {new_criterion_id}
              AND ad_group.id = {ad_group_id}
            """,
        ),
    }


@adgroups_mcp.tool
def remove_negative_keywords(
    customer_id: str,
    criterion_ids: List[str],
    campaign_id: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Removes negative keywords, at campaign or ad group level.

    Note which way this cuts: a negative keyword BLOCKS traffic, so removing
    one lets that traffic back in and the account starts paying for it again.
    The campaign budget still caps the daily total, but what the money is
    spent on changes. Say which terms will start matching again before
    confirming.

    Args:
        customer_id: The Google Ads account id.
        criterion_ids: Criterion ids of the negatives to remove.
        campaign_id: Remove from campaign level. Give this OR ad_group_id.
        ad_group_id: Remove from ad group level. Give this OR campaign_id.
        confirm_remove: Must be True. Name the terms that will start matching
            again first.

    Returns:
        How many were removed, and from where.
    """
    customer_id = _clean_id(customer_id)
    if bool(campaign_id) == bool(ad_group_id):
        raise ValueError("Give exactly one of campaign_id or ad_group_id.")
    if not confirm_remove:
        level = "campaign" if campaign_id else "ad group"
        raise ValueError(
            f"Removing {len(criterion_ids)} negative keyword(s) from the "
            f"{level} lets the blocked searches match again, so the account "
            f"will start paying for that traffic. List the terms for the user, "
            f"then call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    operations = []

    if campaign_id:
        campaign_id = _clean_id(campaign_id)
        service = utils.get_googleads_service("CampaignCriterionService")
        for criterion_id in criterion_ids:
            operation = client.get_type("CampaignCriterionOperation")
            operation.remove = service.campaign_criterion_path(
                customer_id, campaign_id, _clean_id(criterion_id)
            )
            operations.append(operation)
        service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        verified = _read_back(
            customer_id,
            f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.keyword.text,
                   campaign_criterion.negative
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = KEYWORD
              AND campaign_criterion.negative = true
            """,
        )
        level, parent = "campaign", campaign_id
    else:
        ad_group_id = _clean_id(ad_group_id)
        service = utils.get_googleads_service("AdGroupCriterionService")
        for criterion_id in criterion_ids:
            operation = client.get_type("AdGroupCriterionOperation")
            operation.remove = service.ad_group_criterion_path(
                customer_id, ad_group_id, _clean_id(criterion_id)
            )
            operations.append(operation)
        service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        verified = _read_back(
            customer_id,
            f"""
            SELECT ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.negative
            FROM ad_group_criterion
            WHERE ad_group.id = {ad_group_id}
              AND ad_group_criterion.negative = true
            """,
        )
        level, parent = "ad_group", ad_group_id

    return {
        "level": level,
        "parent_id": parent,
        "removed": len(operations),
        "note": (
            "These searches can match again. The campaign budget still caps "
            "the daily total."
        ),
        "remaining_negatives": verified,
    }


# --------------------------------------------------------------------------
# Ad lifecycle
# --------------------------------------------------------------------------


@adgroups_mcp.tool
def set_ad_status(
    customer_id: str,
    ad_group_id: str,
    ad_id: str,
    status: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Enables, pauses or removes a single ad.

    Pausing a weak ad while leaving its siblings running is the normal way to
    test creative. Enabling needs no confirmation -- the campaign is still the
    spend gate.

    Args:
        customer_id: The Google Ads account id.
        ad_group_id: The ad group the ad belongs to.
        ad_id: The ad to change.
        status: ENABLED, PAUSED or REMOVED.
        confirm_remove: Required for REMOVED, which is not reversible.

    Returns:
        The ad's state read back after the change.
    """
    customer_id = _clean_id(customer_id)
    ad_group_id = _clean_id(ad_group_id)
    ad_id = _clean_id(ad_id)
    status = status.upper()
    if status not in {"ENABLED", "PAUSED", "REMOVED"}:
        raise ValueError("status must be ENABLED, PAUSED or REMOVED.")
    if status == "REMOVED" and not confirm_remove:
        raise ValueError(
            "Removing an ad discards its history and is not reversible. "
            "Pausing stops it serving and can be undone. To remove anyway, "
            "call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdGroupAdService")
    operation = client.get_type("AdGroupAdOperation")
    resource_name = service.ad_group_ad_path(customer_id, ad_group_id, ad_id)

    if status == "REMOVED":
        # Deletion is its own operation. Setting status=REMOVED through an
        # update is refused with INVALID_ENUM_VALUE: "Enum value 'REMOVED'
        # cannot be used" -- see docs/kaynaklar.md.
        operation.remove = resource_name
    else:
        operation.update.resource_name = resource_name
        operation.update.status = getattr(
            client.enums.AdGroupAdStatusEnum, status
        )
        client.copy_from(
            operation.update_mask, field_mask_pb2.FieldMask(paths=["status"])
        )

    service.mutate_ad_group_ads(customer_id=customer_id, operations=[operation])

    return {
        "ad_group_id": ad_group_id,
        "ad_id": ad_id,
        "new_status": status,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group_ad.ad.id, ad_group_ad.status,
                   ad_group_ad.policy_summary.approval_status
            FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id}
            """,
        ),
    }


@adgroups_mcp.tool
def update_ad_text(
    customer_id: str,
    ad_id: str,
    headlines: Optional[List[str]] = None,
    descriptions: Optional[List[str]] = None,
    final_url: Optional[str] = None,
    path1: Optional[str] = None,
    path2: Optional[str] = None,
) -> Dict[str, Any]:
    """Edits a responsive search ad's copy, landing page or display paths.

    Whichever list you pass REPLACES that list entirely -- pass every headline
    you want the ad to keep, not just the new ones. The character limits are
    checked here first, so a too-long line fails with its own text and length
    instead of an opaque API error.

    `final_url` exists so a wrong landing page costs an edit rather than the
    whole ad. It does NOT excuse guessing: a URL that does not resolve gets
    the ad disapproved, so copy it from an already-approved ad in the same
    account or confirm the page loads before calling.

    Editing an ad sends it back to Google for review and resets its
    performance history, so prefer it for fixing copy; to test a new angle,
    create a second ad and pause the first.

    Args:
        customer_id: The Google Ads account id.
        ad_id: The ad to edit.
        headlines: 3-15 headlines, each at most 30 characters. Omit to keep
            the current ones.
        descriptions: 2-4 descriptions, each at most 90 characters. Omit to
            keep the current ones.
        final_url: The landing page. Must be a verified, resolving http(s)
            URL. Omit to keep the current one.
        path1: First display path segment, at most 15 characters. Pass an
            empty string to clear it.
        path2: Second display path segment. Needs path1 to be set.

    Returns:
        What was replaced, and the ad read back.
    """
    customer_id = _clean_id(customer_id)
    ad_id = _clean_id(ad_id)

    if all(
        v is None
        for v in (headlines, descriptions, final_url, path1, path2)
    ):
        raise ValueError(
            "Give at least one of headlines, descriptions, final_url, "
            "path1 or path2."
        )

    if final_url is not None:
        final_url = final_url.strip()
        if not final_url.startswith(("http://", "https://")):
            raise ValueError(
                f"final_url must start with http:// or https://, got "
                f"{final_url!r}."
            )
    for name, value in (("path1", path1), ("path2", path2)):
        if value is not None and len(value) > _MAX_PATH_CHARS:
            raise ValueError(
                f"{name} is {len(value)} characters, limit is "
                f"{_MAX_PATH_CHARS}: {value!r}"
            )
    if path2 and not path1:
        raise ValueError("path2 needs path1 to be set as well.")

    if headlines is not None:
        if not _MIN_HEADLINES <= len(headlines) <= _MAX_HEADLINES:
            raise ValueError(
                f"Need {_MIN_HEADLINES}-{_MAX_HEADLINES} headlines, got "
                f"{len(headlines)}."
            )
        for text in headlines:
            if len(text) > _MAX_HEADLINE_CHARS:
                raise ValueError(
                    f"Headline is {len(text)} characters, limit is "
                    f"{_MAX_HEADLINE_CHARS}: {text!r}"
                )
    if descriptions is not None:
        if not _MIN_DESCRIPTIONS <= len(descriptions) <= _MAX_DESCRIPTIONS:
            raise ValueError(
                f"Need {_MIN_DESCRIPTIONS}-{_MAX_DESCRIPTIONS} descriptions, "
                f"got {len(descriptions)}."
            )
        for text in descriptions:
            if len(text) > _MAX_DESCRIPTION_CHARS:
                raise ValueError(
                    f"Description is {len(text)} characters, limit is "
                    f"{_MAX_DESCRIPTION_CHARS}: {text!r}"
                )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AdService")

    operation = client.get_type("AdOperation")
    ad = operation.update
    ad.resource_name = service.ad_path(customer_id, ad_id)

    paths = []
    if headlines is not None:
        for text in headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            ad.responsive_search_ad.headlines.append(asset)
        paths.append("responsive_search_ad.headlines")
    if descriptions is not None:
        for text in descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            ad.responsive_search_ad.descriptions.append(asset)
        paths.append("responsive_search_ad.descriptions")
    if final_url is not None:
        ad.final_urls.append(final_url)
        paths.append("final_urls")
    if path1 is not None:
        ad.responsive_search_ad.path1 = path1
        paths.append("responsive_search_ad.path1")
    if path2 is not None:
        ad.responsive_search_ad.path2 = path2
        paths.append("responsive_search_ad.path2")

    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )
    service.mutate_ads(customer_id=customer_id, operations=[operation])

    return {
        "ad_id": ad_id,
        "fields_changed": paths,
        "headlines_replaced": len(headlines) if headlines else 0,
        "descriptions_replaced": len(descriptions) if descriptions else 0,
        "final_url": final_url,
        "note": (
            "The edited ad goes back to Google for review. The parent campaign "
            "is still the spend gate."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT ad_group_ad.ad.id,
                   ad_group_ad.ad.final_urls,
                   ad_group_ad.ad.responsive_search_ad.path1,
                   ad_group_ad.ad.responsive_search_ad.path2,
                   ad_group_ad.status,
                   ad_group_ad.ad_strength,
                   ad_group_ad.policy_summary.approval_status
            FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id}
            """,
        ),
    }
