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

"""Shared negative keyword lists.

A negative added to one ad group protects one ad group. The same negative in
a shared list protects every campaign the list is attached to, and stays
correct when a new campaign is built next month. For an account managed over
time -- and for an agency managing several -- this is where the master
negative list belongs.

The trap this module exists to make visible: a list can hold hundreds of
terms and be attached to NOTHING. Google reports that as reference_count 0,
and an unattached list blocks exactly nothing while looking like protection.
Every read here returns that count.
"""

from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

shared_sets_mcp = FastMCP("shared_sets")

_MATCH_TYPES = {"EXACT", "PHRASE", "BROAD"}

# Google's own cap on a single shared set.
_MAX_MEMBERS = 5000


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


def _sets(customer_id: str, shared_set_id: Optional[str] = None) -> List[Dict]:
    where = ["shared_set.status != 'REMOVED'"]
    if shared_set_id:
        where.append(f"shared_set.id = {_clean_id(shared_set_id)}")
    rows = _read_back(
        customer_id,
        f"""
        SELECT shared_set.id, shared_set.name, shared_set.type,
               shared_set.status, shared_set.member_count,
               shared_set.reference_count
        FROM shared_set
        WHERE {' AND '.join(where)}
        ORDER BY shared_set.name
        """,
    )
    out = []
    for row in rows:
        s = row["shared_set"]
        references = int(s.get("reference_count") or 0)
        entry = {
            "shared_set_id": s["id"],
            "name": s["name"],
            "type": _enum(s, "type"),
            "status": s.get("status"),
            "terms": int(s.get("member_count") or 0),
            "campaigns_using_it": references,
        }
        if not references:
            entry["warning"] = (
                "Attached to no campaign, so it blocks nothing. "
                "attach_negative_keyword_list fixes this."
            )
        out.append(entry)
    return out


@shared_sets_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_negative_keyword_lists(
    customer_id: str,
) -> List[Dict[str, Any]]:
    """Lists the account's shared negative keyword lists.

    Read this before creating a new one: accounts accumulate near-duplicate
    lists, and a list attached to nothing is a common and invisible failure.

    Args:
        customer_id: The Google Ads account id.

    Returns:
        One row per list with its term count and how many campaigns use it.
    """
    return _sets(_clean_id(customer_id))


@shared_sets_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_negative_keyword_list_terms(
    customer_id: str,
    shared_set_id: str,
) -> Dict[str, Any]:
    """Gets the terms inside one shared negative keyword list.

    Args:
        customer_id: The Google Ads account id.
        shared_set_id: The list, from list_negative_keyword_lists.

    Returns:
        The list's details, its terms, and the campaigns it is attached to.
    """
    customer_id = _clean_id(customer_id)
    shared_set_id = _clean_id(shared_set_id)

    details = _sets(customer_id, shared_set_id)
    if not details:
        raise ValueError(
            f"No shared set {shared_set_id} in account {customer_id}."
        )

    terms = [
        {
            "text": row["shared_criterion"]["keyword"]["text"],
            "match_type": row["shared_criterion"]["keyword"]["match_type"],
            "criterion_id": row["shared_criterion"]["criterion_id"],
        }
        for row in _read_back(
            customer_id,
            f"""
            SELECT shared_criterion.criterion_id,
                   shared_criterion.keyword.text,
                   shared_criterion.keyword.match_type
            FROM shared_criterion
            WHERE shared_set.id = {shared_set_id}
            """,
        )
    ]

    campaigns = [
        {
            "campaign_id": row["campaign"]["id"],
            "name": row["campaign"]["name"],
        }
        for row in _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign_shared_set.status
            FROM campaign_shared_set
            WHERE shared_set.id = {shared_set_id}
              AND campaign_shared_set.status != 'REMOVED'
            """,
        )
    ]

    return {
        **details[0],
        "terms_list": terms,
        "attached_to": campaigns,
    }


@shared_sets_mcp.tool
def create_negative_keyword_list(
    customer_id: str,
    name: str,
    keywords: Optional[List[str]] = None,
    match_type: str = "BROAD",
) -> Dict[str, Any]:
    """Creates a shared negative keyword list, optionally with its first terms.

    Creating a list spends nothing and blocks nothing until it is attached to
    a campaign with attach_negative_keyword_list. That second step is the one
    people forget.

    Args:
        customer_id: The Google Ads account id.
        name: A name that says what the list excludes, e.g.
            "Free and DIY" or "Competitor names". Must be unique.
        keywords: Optional first batch of terms.
        match_type: EXACT, PHRASE or BROAD for those terms. BROAD blocks any
            query containing all the words -- the usual choice for a
            one-word exclusion. Negatives never match close variants, so
            spelling and accents must be listed explicitly.

    Returns:
        The new list's id and state.
    """
    customer_id = _clean_id(customer_id)
    name = str(name).strip()
    if not name:
        raise ValueError("Give the list a name that says what it excludes.")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("SharedSetService")

    operation = client.get_type("SharedSetOperation")
    shared_set = operation.create
    shared_set.name = name
    shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS

    response = service.mutate_shared_sets(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    shared_set_id = resource_name.rsplit("/", 1)[-1]

    added = 0
    if keywords:
        added = _add_terms(
            customer_id, shared_set_id, keywords, match_type
        )["added"]

    return {
        "shared_set_id": shared_set_id,
        "name": name,
        "terms_added": added,
        "next_step": (
            "This list blocks nothing yet. Call "
            "attach_negative_keyword_list with the campaigns it should "
            "protect."
        ),
        "verified": _sets(customer_id, shared_set_id),
    }


def _add_terms(
    customer_id: str,
    shared_set_id: str,
    keywords: List[str],
    match_type: str,
) -> Dict[str, Any]:
    """Adds terms to a shared set, skipping ones already in it."""
    customer_id = _clean_id(customer_id)
    shared_set_id = _clean_id(shared_set_id)
    match_type = str(match_type).upper()
    if match_type not in _MATCH_TYPES:
        raise ValueError(f"match_type must be one of {sorted(_MATCH_TYPES)}.")
    if not keywords:
        raise ValueError("Give at least one keyword.")

    existing_rows = _read_back(
        customer_id,
        f"""
        SELECT shared_criterion.keyword.text,
               shared_criterion.keyword.match_type
        FROM shared_criterion
        WHERE shared_set.id = {shared_set_id}
        """,
    )
    existing = {
        (
            row["shared_criterion"]["keyword"]["text"].casefold(),
            row["shared_criterion"]["keyword"]["match_type"],
        )
        for row in existing_rows
    }
    if len(existing) + len(keywords) > _MAX_MEMBERS:
        raise ValueError(
            f"A shared set holds at most {_MAX_MEMBERS} terms; this list "
            f"already has {len(existing)}."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("SharedCriterionService")
    shared_set_path = utils.get_googleads_service(
        "SharedSetService"
    ).shared_set_path(customer_id, shared_set_id)

    operations = []
    skipped = []
    for text in keywords:
        text = str(text).strip()
        if not text:
            continue
        if (text.casefold(), match_type) in existing:
            skipped.append(text)
            continue
        operation = client.get_type("SharedCriterionOperation")
        criterion = operation.create
        criterion.shared_set = shared_set_path
        criterion.keyword.text = text
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, match_type
        )
        operations.append(operation)

    if operations:
        service.mutate_shared_criteria(
            customer_id=customer_id, operations=operations
        )

    return {
        "shared_set_id": shared_set_id,
        "added": len(operations),
        "already_present": skipped,
        "match_type": match_type,
        "verified": _sets(customer_id, shared_set_id),
    }


@shared_sets_mcp.tool
def add_to_negative_keyword_list(
    customer_id: str,
    shared_set_id: str,
    keywords: List[str],
    match_type: str = "BROAD",
) -> Dict[str, Any]:
    """Adds negative keywords to an existing shared list.

    Terms already in the list are skipped rather than duplicated, so this is
    safe to call repeatedly with an overlapping batch.

    Negatives never match close variants, so a Turkish list needs both the
    accented and the unaccented spelling of a word to block both.

    Args:
        customer_id: The Google Ads account id.
        shared_set_id: The list to add to.
        keywords: The terms to exclude.
        match_type: EXACT, PHRASE or BROAD.

    Returns:
        How many were added, and which were already there.
    """
    return _add_terms(customer_id, shared_set_id, keywords, match_type)


@shared_sets_mcp.tool
def attach_negative_keyword_list(
    customer_id: str,
    shared_set_id: str,
    campaign_ids: List[str],
) -> Dict[str, Any]:
    """Attaches a shared negative keyword list to one or more campaigns.

    This is the step that makes the list do anything. Attaching narrows what
    a campaign can match, so it can only reduce spend, never increase it --
    but it can silence traffic somebody wanted, so read the list's terms
    first with get_negative_keyword_list_terms.

    Args:
        customer_id: The Google Ads account id.
        shared_set_id: The list to attach.
        campaign_ids: The campaigns it should protect.

    Returns:
        Which campaigns it was attached to, and which already had it.
    """
    customer_id = _clean_id(customer_id)
    shared_set_id = _clean_id(shared_set_id)
    if not campaign_ids:
        raise ValueError("Give at least one campaign id.")

    already = {
        str(row["campaign"]["id"])
        for row in _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign_shared_set.status
            FROM campaign_shared_set
            WHERE shared_set.id = {shared_set_id}
              AND campaign_shared_set.status != 'REMOVED'
            """,
        )
    }

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignSharedSetService")
    campaign_service = utils.get_googleads_service("CampaignService")
    shared_set_path = utils.get_googleads_service(
        "SharedSetService"
    ).shared_set_path(customer_id, shared_set_id)

    operations = []
    attached = []
    skipped = []
    for campaign_id in campaign_ids:
        campaign_id = _clean_id(campaign_id)
        if campaign_id in already:
            skipped.append(campaign_id)
            continue
        operation = client.get_type("CampaignSharedSetOperation")
        link = operation.create
        link.campaign = campaign_service.campaign_path(
            customer_id, campaign_id
        )
        link.shared_set = shared_set_path
        operations.append(operation)
        attached.append(campaign_id)

    if operations:
        service.mutate_campaign_shared_sets(
            customer_id=customer_id, operations=operations
        )

    return {
        "shared_set_id": shared_set_id,
        "attached": attached,
        "already_attached": skipped,
        "verified": _sets(customer_id, shared_set_id),
    }


@shared_sets_mcp.tool
def detach_negative_keyword_list(
    customer_id: str,
    shared_set_id: str,
    campaign_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Detaches a shared negative keyword list from a campaign.

    Detaching REOPENS every query the list was blocking, which is a spend
    increase, so it needs confirmation.

    Args:
        customer_id: The Google Ads account id.
        shared_set_id: The list to detach.
        campaign_id: The campaign to remove it from.
        confirm: Required, because this widens what the campaign can match.

    Returns:
        The list's state after detaching.
    """
    customer_id = _clean_id(customer_id)
    shared_set_id = _clean_id(shared_set_id)
    campaign_id = _clean_id(campaign_id)

    details = _sets(customer_id, shared_set_id)
    if not details:
        raise ValueError(f"No shared set {shared_set_id}.")
    if not confirm:
        raise ValueError(
            f"Detaching '{details[0]['name']}' reopens {details[0]['terms']} "
            f"excluded terms on campaign {campaign_id}, which will let it "
            f"spend on traffic that is currently blocked. Say what those "
            f"terms are, then call again with confirm=True."
        )

    rows = _read_back(
        customer_id,
        f"""
        SELECT campaign_shared_set.resource_name
        FROM campaign_shared_set
        WHERE shared_set.id = {shared_set_id}
          AND campaign.id = {campaign_id}
          AND campaign_shared_set.status != 'REMOVED'
        """,
    )
    if not rows:
        return {
            "shared_set_id": shared_set_id,
            "campaign_id": campaign_id,
            "detached": False,
            "reason": "That list is not attached to that campaign.",
        }

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignSharedSetService")
    operation = client.get_type("CampaignSharedSetOperation")
    operation.remove = rows[0]["campaign_shared_set"]["resource_name"]
    service.mutate_campaign_shared_sets(
        customer_id=customer_id, operations=[operation]
    )

    return {
        "shared_set_id": shared_set_id,
        "campaign_id": campaign_id,
        "detached": True,
        "verified": _sets(customer_id, shared_set_id),
    }


@shared_sets_mcp.tool
def remove_negative_keyword_list(
    customer_id: str,
    shared_set_id: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Deletes a shared negative keyword list entirely.

    Removing a list that is still attached to campaigns REOPENS every term
    it was blocking on all of them at once, so the count of attached
    campaigns is named in the confirmation rather than left for the caller
    to look up.

    Args:
        customer_id: The Google Ads account id.
        shared_set_id: The list to delete.
        confirm_remove: Required. The list and its terms are gone for good.

    Returns:
        What was deleted.
    """
    customer_id = _clean_id(customer_id)
    shared_set_id = _clean_id(shared_set_id)

    details = _sets(customer_id, shared_set_id)
    if not details:
        raise ValueError(f"No shared set {shared_set_id}.")
    entry = details[0]
    if not confirm_remove:
        attached = entry["campaigns_using_it"]
        raise ValueError(
            f"Deleting '{entry['name']}' discards {entry['terms']} terms "
            f"permanently"
            + (
                f" and reopens them on {attached} campaign(s) that currently "
                f"use it"
                if attached
                else ""
            )
            + ". Call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("SharedSetService")
    operation = client.get_type("SharedSetOperation")
    operation.remove = service.shared_set_path(customer_id, shared_set_id)
    service.mutate_shared_sets(
        customer_id=customer_id, operations=[operation]
    )
    return {
        "shared_set_id": shared_set_id,
        "name": entry["name"],
        "terms_deleted": entry["terms"],
        "removed": True,
    }
