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

"""Audience and demographic targeting on Search campaigns.

The distinction this module exists to keep straight:

  * OBSERVATION adds an audience to watch and, optionally, to bid differently
    on. Reach does not change. On a Search campaign this is almost always
    what you want, and it is what makes an audience bid modifier possible
    later.
  * TARGETING restricts the campaign to that audience only. Reach collapses
    to the audience's size, which on a small local list can mean the campaign
    effectively stops.

Google's default for Search is observation. Switching a live campaign to
targeting is a reach cut, so it is gated behind a confirmation here.
"""

from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from google.protobuf import field_mask_pb2
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

targeting_mcp = FastMCP("targeting")

_AGE_RANGES = {
    "AGE_RANGE_18_24", "AGE_RANGE_25_34", "AGE_RANGE_35_44",
    "AGE_RANGE_45_54", "AGE_RANGE_55_64", "AGE_RANGE_65_UP",
    "AGE_RANGE_UNDETERMINED",
}
_GENDERS = {"MALE", "FEMALE", "UNDETERMINED"}


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


def _read_back(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    rows = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            rows.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return rows


def _targeting_snapshot(customer_id: str, campaign_id: str) -> Dict[str, Any]:
    """Reads a campaign's criteria, grouped by kind."""

    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    rows = _read_back(
        customer_id,
        f"""
        SELECT campaign_criterion.criterion_id, campaign_criterion.type,
               campaign_criterion.negative, campaign_criterion.status,
               campaign_criterion.bid_modifier,
               campaign_criterion.display_name,
               campaign_criterion.location.geo_target_constant,
               campaign_criterion.proximity.radius,
               campaign_criterion.proximity.radius_units,
               campaign_criterion.age_range.type,
               campaign_criterion.gender.type,
               campaign_criterion.device.type,
               campaign_criterion.user_list.user_list,
               campaign_criterion.language.language_constant,
               campaign_criterion.keyword.text,
               campaign_criterion.keyword.match_type
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id}
          AND campaign_criterion.status != 'REMOVED'
        """,
    )

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    negative_keywords: List[Dict[str, Any]] = []
    for row in rows:
        crit = row["campaign_criterion"]
        kind = _enum(crit, "type") or "UNKNOWN"
        if kind == "KEYWORD" and crit.get("negative"):
            negative_keywords.append(
                {
                    "text": crit.get("keyword", {}).get("text"),
                    "match_type": crit.get("keyword", {}).get("match_type"),
                    "criterion_id": crit.get("criterion_id"),
                }
            )
            continue
        entry = {
            "criterion_id": crit.get("criterion_id"),
            "negative": bool(crit.get("negative")),
            "status": crit.get("status"),
        }
        if crit.get("bid_modifier"):
            entry["bid_modifier"] = round(float(crit["bid_modifier"]), 2)
        if crit.get("display_name"):
            entry["name"] = crit["display_name"]
        for key in (
            "location", "proximity", "age_range", "gender", "device",
            "user_list", "language", "keyword",
        ):
            payload = crit.get(key)
            if not payload:
                continue
            # proto returns every unset sub-field as "" / 0 / "UNSPECIFIED"
            # rather than omitting it, so a criterion row carries fifteen
            # empty fields for the one that is actually set.
            filled = {
                f"{key}_{k}": v
                for k, v in payload.items()
                if v not in ("", None, 0, [], "UNSPECIFIED")
            }
            entry.update(filled)
        grouped.setdefault(kind, []).append(entry)

    settings = _read_back(
        customer_id,
        f"""
        SELECT campaign.id,
               campaign.targeting_setting.target_restrictions,
               campaign.geo_target_type_setting.positive_geo_target_type
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    audience_mode = "OBSERVATION (Google's default)"
    geo_mode = None
    if settings:
        campaign = settings[0]["campaign"]
        geo_mode = campaign.get("geo_target_type_setting", {}).get(
            "positive_geo_target_type"
        )
        for restriction in campaign.get("targeting_setting", {}).get(
            "target_restrictions", []
        ):
            if restriction.get("targeting_dimension") == "AUDIENCE":
                audience_mode = (
                    "OBSERVATION" if restriction.get("bid_only")
                    else "TARGETING -- reach is restricted to these audiences"
                )

    result: Dict[str, Any] = {
        "campaign_id": campaign_id,
        "audience_mode": audience_mode,
        "positive_geo_target_type": geo_mode,
        "criteria": grouped,
        "counts": {k: len(v) for k, v in grouped.items()},
    }
    if negative_keywords:
        result["counts"]["NEGATIVE_KEYWORD"] = len(negative_keywords)
        result["negative_keywords"] = negative_keywords
    if geo_mode == "PRESENCE_OR_INTEREST":
        result["warning"] = (
            "positive_geo_target_type is PRESENCE_OR_INTEREST, so the ads "
            "also reach people merely INTERESTED in the targeted area from "
            "anywhere else. For a business people walk into, that is usually "
            "the largest single source of wasted spend. "
            "get_performance_by_segment with segment='geo' measures how much "
            "of the cost it is."
        )
    return result


@targeting_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_campaign_targeting(
    customer_id: str,
    campaign_id: str,
) -> Dict[str, Any]:
    """Lists everything a campaign targets or excludes, grouped by kind.

    Locations, audiences, demographics, languages, devices and negative
    criteria all live in the same campaign_criterion table, which is why they
    are easy to lose track of. This groups them and states whether audiences
    are in observation or targeting mode, and what
    positive_geo_target_type is -- PRESENCE_OR_INTEREST there is the
    commonest silent leak on a local account.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to inspect.

    Returns:
        Criteria grouped by type, plus `audience_mode`.
    """
    return _targeting_snapshot(customer_id, campaign_id)


@targeting_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_audiences(
    customer_id: str,
) -> Dict[str, Any]:
    """Lists the audience segments this account can target or observe.

    Includes the account's own remarketing lists, whose size matters: Google
    requires roughly 1,000 active visitors before a Search remarketing list
    can serve, and a list below that is listed here but will never match.

    Args:
        customer_id: The Google Ads account id.

    Returns:
        `user_lists` with their sizes, and `audiences` (the newer combined
        audience resource) if any exist.
    """
    customer_id = _clean_id(customer_id)

    user_lists = []
    for row in _read_back(
        customer_id,
        """
        SELECT user_list.id, user_list.name, user_list.type,
               user_list.membership_status, user_list.size_for_search,
               user_list.size_for_display, user_list.membership_life_span,
               user_list.eligible_for_search, user_list.resource_name
        FROM user_list
        WHERE user_list.membership_status = 'OPEN'
        """,
    ):
        u = row["user_list"]
        size = int(u.get("size_for_search") or 0)
        entry = {
            "user_list_id": u["id"],
            "name": u["name"],
            "type": _enum(u, "type"),
            "size_for_search": size,
            "size_for_display": int(u.get("size_for_display") or 0),
            "membership_days": u.get("membership_life_span"),
            "eligible_for_search": bool(u.get("eligible_for_search")),
            "resource_name": u["resource_name"],
        }
        if not u.get("eligible_for_search"):
            entry["warning"] = (
                "Too small to serve on Search. Google needs roughly 1,000 "
                "active members; this list can be attached but will not match."
            )
        user_lists.append(entry)
    user_lists.sort(key=lambda u: u["size_for_search"], reverse=True)

    audiences = [
        {
            "audience_id": row["audience"]["id"],
            "name": row["audience"]["name"],
            "status": row["audience"].get("status"),
            "resource_name": row["audience"]["resource_name"],
        }
        for row in _read_back(
            customer_id,
            """
            SELECT audience.id, audience.name, audience.status,
                   audience.resource_name
            FROM audience WHERE audience.status = 'ENABLED'
            """,
        )
    ]

    return {
        "user_lists": user_lists,
        "audiences": audiences,
        "note": (
            "On Search, add these in OBSERVATION mode first: reach is "
            "unchanged and after a few weeks the data says whether an "
            "audience deserves a bid modifier."
        ),
    }


@targeting_mcp.tool
def add_audience_to_campaign(
    customer_id: str,
    campaign_id: str,
    user_list_ids: Optional[List[str]] = None,
    audience_ids: Optional[List[str]] = None,
    bid_modifier: Optional[float] = None,
    mode: str = "OBSERVATION",
    confirm_reach_cut: bool = False,
) -> Dict[str, Any]:
    """Adds audiences to a Search campaign, in observation or targeting mode.

    OBSERVATION changes nothing about who sees the ads; it starts collecting
    performance per audience and lets a bid modifier apply. TARGETING
    restricts the campaign to these audiences only, which on Search usually
    collapses volume -- so it needs confirm_reach_cut.

    A bid modifier of 1.2 bids 20% more for people in the audience; 0.8 bids
    20% less. Setting one before there is data is guessing.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to add them to.
        user_list_ids: Remarketing / customer-match list ids, from
            list_audiences.
        audience_ids: Ids from the newer combined `audience` resource.
        bid_modifier: Optional multiplier, 0.1 to 10.0.
        mode: OBSERVATION or TARGETING.
        confirm_reach_cut: Required for TARGETING.

    Returns:
        What was added and the campaign's audience mode afterwards.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    mode = str(mode).upper()
    if mode not in {"OBSERVATION", "TARGETING"}:
        raise ValueError("mode must be OBSERVATION or TARGETING.")
    if not user_list_ids and not audience_ids:
        raise ValueError("Give user_list_ids, audience_ids, or both.")
    if bid_modifier is not None and not 0.1 <= bid_modifier <= 10.0:
        raise ValueError("bid_modifier must be between 0.1 and 10.0.")
    if mode == "TARGETING" and not confirm_reach_cut:
        raise ValueError(
            "TARGETING restricts the campaign to these audiences only, so "
            "everyone outside them stops seeing the ads. On Search that "
            "usually means a large volume drop. Say what the expected drop "
            "is, then call again with confirm_reach_cut=True. OBSERVATION "
            "collects the same data without touching reach."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_path = utils.get_googleads_service(
        "CampaignService"
    ).campaign_path(customer_id, campaign_id)

    operations = []
    for user_list_id in user_list_ids or []:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path
        criterion.user_list.user_list = (
            f"customers/{customer_id}/userLists/{_clean_id(user_list_id)}"
        )
        if bid_modifier is not None:
            criterion.bid_modifier = bid_modifier
        operations.append(operation)

    for audience_id in audience_ids or []:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path
        criterion.audience.audience = (
            f"customers/{customer_id}/audiences/{_clean_id(audience_id)}"
        )
        if bid_modifier is not None:
            criterion.bid_modifier = bid_modifier
        operations.append(operation)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    # The observation/targeting switch lives on the campaign, not the
    # criterion: without it, criteria added for observation can silently
    # behave as targeting on a campaign somebody set that way earlier.
    campaign_service = utils.get_googleads_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.update
    campaign.resource_name = campaign_path
    restriction = client.get_type("TargetRestriction")
    restriction.targeting_dimension = (
        client.enums.TargetingDimensionEnum.AUDIENCE
    )
    restriction.bid_only = mode == "OBSERVATION"
    campaign.targeting_setting.target_restrictions.append(restriction)
    client.copy_from(
        campaign_operation.update_mask,
        field_mask_pb2.FieldMask(paths=["targeting_setting"]),
    )
    campaign_service.mutate_campaigns(
        customer_id=customer_id, operations=[campaign_operation]
    )

    return {
        "campaign_id": campaign_id,
        "added": len(operations),
        "mode": mode,
        "bid_modifier": bid_modifier,
        "verified": _targeting_snapshot(customer_id, campaign_id),
    }


@targeting_mcp.tool
def exclude_demographics(
    customer_id: str,
    campaign_id: str,
    age_ranges: Optional[List[str]] = None,
    genders: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Excludes age ranges or genders from a campaign.

    Use this only where the exclusion is a fact about the service, not a
    guess about who buys: a men's barber excluding nothing is usually right,
    because a woman booking for her son is a real customer. Excluding
    AGE_RANGE_UNDETERMINED is almost always a mistake -- it is the largest
    single bucket on Search, since Google does not know most searchers' age.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        age_ranges: Any of AGE_RANGE_18_24, AGE_RANGE_25_34, AGE_RANGE_35_44,
            AGE_RANGE_45_54, AGE_RANGE_55_64, AGE_RANGE_65_UP,
            AGE_RANGE_UNDETERMINED.
        genders: Any of MALE, FEMALE, UNDETERMINED.

    Returns:
        What was excluded, with a warning if an UNDETERMINED bucket was hit.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    age_ranges = [str(a).upper() for a in (age_ranges or [])]
    genders = [str(g).upper() for g in (genders or [])]
    if not age_ranges and not genders:
        raise ValueError("Give age_ranges, genders, or both.")
    for value in age_ranges:
        if value not in _AGE_RANGES:
            raise ValueError(
                f"{value!r} is not an age range. Use one of "
                f"{sorted(_AGE_RANGES)}."
            )
    for value in genders:
        if value not in _GENDERS:
            raise ValueError(
                f"{value!r} is not a gender. Use one of {sorted(_GENDERS)}."
            )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    campaign_path = utils.get_googleads_service(
        "CampaignService"
    ).campaign_path(customer_id, campaign_id)

    operations = []
    for value in age_ranges:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path
        criterion.negative = True
        criterion.age_range.type_ = getattr(
            client.enums.AgeRangeTypeEnum, value
        )
        operations.append(operation)
    for value in genders:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path
        criterion.negative = True
        criterion.gender.type_ = getattr(client.enums.GenderTypeEnum, value)
        operations.append(operation)

    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    result = {
        "campaign_id": campaign_id,
        "excluded_age_ranges": age_ranges,
        "excluded_genders": genders,
        "excluded": len(operations),
    }
    if "AGE_RANGE_UNDETERMINED" in age_ranges or "UNDETERMINED" in genders:
        result["warning"] = (
            "An UNDETERMINED bucket was excluded. Google does not know the "
            "age or gender of most searchers, so this bucket carries a large "
            "share of ordinary traffic -- this is usually a much bigger cut "
            "than intended."
        )
    return result


@targeting_mcp.tool
def remove_campaign_criteria(
    customer_id: str,
    campaign_id: str,
    criterion_ids: List[str],
    confirm_widens_reach: bool = False,
) -> Dict[str, Any]:
    """Removes campaign criteria: audiences, demographic exclusions, and so on.

    This is the inverse of add_audience_to_campaign and
    exclude_demographics. Removing a NEGATIVE criterion widens what the
    campaign can reach and therefore what it can spend, which is why that
    case is gated; removing a positive one narrows it and is not.

    Criterion ids come from list_campaign_targeting.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to change.
        criterion_ids: The criteria to remove.
        confirm_widens_reach: Required when any of them is an exclusion.

    Returns:
        What was removed, and the campaign's targeting afterwards.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    if not criterion_ids:
        raise ValueError("Give at least one criterion id.")
    wanted = {_clean_id(c) for c in criterion_ids}

    rows = _read_back(
        customer_id,
        f"""
        SELECT campaign_criterion.criterion_id, campaign_criterion.type,
               campaign_criterion.negative,
               campaign_criterion.display_name,
               campaign_criterion.resource_name
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id}
          AND campaign_criterion.status != 'REMOVED'
        """,
    )
    found = {
        str(row["campaign_criterion"]["criterion_id"]): row["campaign_criterion"]
        for row in rows
        if str(row["campaign_criterion"]["criterion_id"]) in wanted
    }
    missing = sorted(wanted - set(found))
    if not found:
        raise ValueError(
            f"None of those criteria are on campaign {campaign_id}: "
            f"{missing}."
        )

    exclusions = [
        {
            "criterion_id": cid,
            "type": _enum(crit, "type"),
            "name": crit.get("display_name"),
        }
        for cid, crit in found.items()
        if crit.get("negative")
    ]
    if exclusions and not confirm_widens_reach:
        raise ValueError(
            f"{len(exclusions)} of these are EXCLUSIONS: {exclusions}. "
            f"Removing them lets the campaign reach and spend on traffic it "
            f"currently refuses. Say what that traffic is, then call again "
            f"with confirm_widens_reach=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignCriterionService")
    operations = []
    for crit in found.values():
        operation = client.get_type("CampaignCriterionOperation")
        operation.remove = crit["resource_name"]
        operations.append(operation)
    service.mutate_campaign_criteria(
        customer_id=customer_id, operations=operations
    )

    return {
        "campaign_id": campaign_id,
        "removed": sorted(found),
        "not_found": missing,
        "exclusions_removed": exclusions,
        "verified": _targeting_snapshot(customer_id, campaign_id),
    }
