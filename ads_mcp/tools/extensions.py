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

"""Write tools for ad extensions (assets): sitelinks, callouts, snippets.

Extensions never raise what an account can spend -- the campaign budget still
caps the day -- so nothing here needs confirm_spend. They do change what the
ad looks like in the auction, which is usually the cheapest available way to
lift click-through rate on a search campaign.

Every extension is created in two steps, which this module hides: the asset is
created once at account level, then LINKED to a campaign. The same asset can
be linked to several campaigns, so creating a sitelink twice under the same
name is waste, not safety.

Platform limits are enforced here, before the call leaves the process, so a
too-long line fails with its own text and length instead of an opaque API
error.
"""

from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

extensions_mcp = FastMCP("extensions")

# Google Ads asset limits.
_MAX_SITELINK_TEXT = 25
_MAX_SITELINK_DESC = 35
_MAX_CALLOUT_TEXT = 25
_MAX_SNIPPET_VALUE = 25
_MIN_SNIPPET_VALUES = 3
_MAX_SNIPPET_VALUES = 10

# Structured snippet headers Google accepts. The header is not free text.
_SNIPPET_HEADERS = {
    "Amenities", "Brands", "Courses", "Degree programs", "Destinations",
    "Featured hotels", "Insurance coverage", "Models", "Neighborhoods",
    "Service catalog", "Shows", "Styles", "Types",
}


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _read_back(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    rows = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            rows.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return rows


def _check_len(label: str, text: str, limit: int) -> None:
    if len(text) > limit:
        raise ValueError(
            f"{label} is {len(text)} characters, limit is {limit}: {text!r}"
        )


def _link_assets_to_campaign(
    customer_id: str,
    campaign_id: str,
    asset_resource_names: List[str],
    field_type: str,
) -> int:
    """Links already-created assets to a campaign. Returns how many linked."""
    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignAssetService")
    campaign_service = utils.get_googleads_service("CampaignService")
    campaign_path = campaign_service.campaign_path(customer_id, campaign_id)

    operations = []
    for resource_name in asset_resource_names:
        operation = client.get_type("CampaignAssetOperation")
        link = operation.create
        link.campaign = campaign_path
        link.asset = resource_name
        link.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type)
        operations.append(operation)

    service.mutate_campaign_assets(
        customer_id=customer_id, operations=operations
    )
    return len(operations)


def _campaign_extensions(customer_id: str, campaign_id: str) -> List[Dict]:
    return _read_back(
        customer_id,
        f"""
        SELECT campaign.id,
               campaign_asset.field_type, campaign_asset.status,
               asset.id, asset.name,
               asset.sitelink_asset.link_text,
               asset.sitelink_asset.description1,
               asset.sitelink_asset.description2,
               asset.callout_asset.callout_text,
               asset.structured_snippet_asset.header,
               asset.structured_snippet_asset.values,
               asset.final_urls
        FROM campaign_asset
        WHERE campaign.id = {campaign_id}
          AND campaign_asset.field_type IN
              ('SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET')
          AND campaign_asset.status != 'REMOVED'
        """,
    )


@extensions_mcp.tool
def add_sitelinks(
    customer_id: str,
    campaign_id: str,
    sitelinks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Adds sitelink extensions to a campaign.

    Sitelinks are extra links under the ad, each to a different page. Google
    generally needs at least FOUR before it will show them, and it picks which
    to show, so give it a real set rather than one.

    Extensions cannot raise spend beyond the campaign budget, so this needs no
    confirmation.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to add them to.
        sitelinks: One dict per sitelink, e.g.
            [{"text": "Ucretsiz Deneme", "url": "https://site.com/deneme",
              "description1": "Ilk ders bizden",
              "description2": "Hemen kayit ol"}]
            "text" is at most 25 characters, the two descriptions 35 each and
            are optional -- but supply both or neither, Google ignores a lone
            one.

    Returns:
        How many were added, and the campaign's extensions read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    if not sitelinks:
        raise ValueError("Give at least one sitelink.")

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    operations = []
    for entry in sitelinks:
        text = entry.get("text")
        url = entry.get("url")
        if not text or not url:
            raise ValueError(
                f"Every sitelink needs 'text' and 'url': {entry!r}"
            )
        if not str(url).startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://: {url}")
        _check_len("Sitelink text", text, _MAX_SITELINK_TEXT)
        desc1 = entry.get("description1")
        desc2 = entry.get("description2")
        for label, value in (("description1", desc1), ("description2", desc2)):
            if value:
                _check_len(f"Sitelink {label}", value, _MAX_SITELINK_DESC)

        operation = client.get_type("AssetOperation")
        asset = operation.create
        asset.name = f"Sitelink: {text}"
        asset.final_urls.append(url)
        asset.sitelink_asset.link_text = text
        if desc1:
            asset.sitelink_asset.description1 = desc1
        if desc2:
            asset.sitelink_asset.description2 = desc2
        operations.append(operation)

    response = asset_service.mutate_assets(
        customer_id=customer_id, operations=operations
    )
    resource_names = [r.resource_name for r in response.results]
    linked = _link_assets_to_campaign(
        customer_id, campaign_id, resource_names, "SITELINK"
    )

    return {
        "campaign_id": campaign_id,
        "added": linked,
        "asset_resource_names": resource_names,
        "note": (
            "Google usually needs at least four sitelinks on a campaign before "
            "it shows any, and chooses which to show per auction."
        ),
        "verified": _campaign_extensions(customer_id, campaign_id),
    }


@extensions_mcp.tool
def add_callout_extensions(
    customer_id: str,
    campaign_id: str,
    callouts: List[str],
) -> Dict[str, Any]:
    """Adds callout extensions to a campaign.

    Callouts are short, non-clickable selling points shown under the ad
    ("Ucretsiz Deneme Dersi", "7/24 Destek"). Google needs at least two and
    shows up to six. They are the cheapest extension to write and the least
    risky, because nothing links anywhere.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to add them to.
        callouts: Callout texts, each at most 25 characters.

    Returns:
        How many were added, and the campaign's extensions read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    if not callouts:
        raise ValueError("Give at least one callout.")

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    operations = []
    for text in callouts:
        _check_len("Callout", text, _MAX_CALLOUT_TEXT)
        operation = client.get_type("AssetOperation")
        asset = operation.create
        asset.name = f"Callout: {text}"
        asset.callout_asset.callout_text = text
        operations.append(operation)

    response = asset_service.mutate_assets(
        customer_id=customer_id, operations=operations
    )
    resource_names = [r.resource_name for r in response.results]
    linked = _link_assets_to_campaign(
        customer_id, campaign_id, resource_names, "CALLOUT"
    )

    return {
        "campaign_id": campaign_id,
        "added": linked,
        "asset_resource_names": resource_names,
        "note": "Google shows callouts only once a campaign has at least two.",
        "verified": _campaign_extensions(customer_id, campaign_id),
    }


@extensions_mcp.tool
def add_structured_snippets(
    customer_id: str,
    campaign_id: str,
    header: str,
    values: List[str],
) -> Dict[str, Any]:
    """Adds a structured snippet extension to a campaign.

    A structured snippet is a header plus a list, shown as "Courses: YKS, TYT,
    AYT". The header is NOT free text -- Google only accepts its own list, and
    the values must actually be instances of it.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to add it to.
        header: One of Google's headers, e.g. "Courses", "Degree programs",
            "Service catalog", "Types", "Brands", "Styles", "Amenities",
            "Destinations", "Insurance coverage", "Models", "Neighborhoods",
            "Shows", "Featured hotels".
        values: 3-10 values, each at most 25 characters.

    Returns:
        What was added, and the campaign's extensions read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    match = next(
        (h for h in _SNIPPET_HEADERS if h.lower() == str(header).lower()), None
    )
    if match is None:
        raise ValueError(
            f"'{header}' is not a Google structured snippet header. "
            f"Valid headers: {sorted(_SNIPPET_HEADERS)}"
        )
    if not _MIN_SNIPPET_VALUES <= len(values) <= _MAX_SNIPPET_VALUES:
        raise ValueError(
            f"Need {_MIN_SNIPPET_VALUES}-{_MAX_SNIPPET_VALUES} values, got "
            f"{len(values)}."
        )
    for value in values:
        _check_len("Snippet value", value, _MAX_SNIPPET_VALUE)

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = f"Snippet: {match}"
    asset.structured_snippet_asset.header = match
    asset.structured_snippet_asset.values.extend(values)

    response = asset_service.mutate_assets(
        customer_id=customer_id, operations=[operation]
    )
    resource_names = [r.resource_name for r in response.results]
    linked = _link_assets_to_campaign(
        customer_id, campaign_id, resource_names, "STRUCTURED_SNIPPET"
    )

    return {
        "campaign_id": campaign_id,
        "header": match,
        "values": values,
        "added": linked,
        "asset_resource_names": resource_names,
        "verified": _campaign_extensions(customer_id, campaign_id),
    }


@extensions_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_campaign_extensions(
    customer_id: str,
    campaign_id: str,
) -> List[Dict[str, Any]]:
    """Lists the sitelinks, callouts and structured snippets on a campaign.

    Check this before adding: extensions are assets that get LINKED, so
    running add_sitelinks twice does not update the first set, it creates a
    second one and the campaign ends up with duplicates competing for the
    same slot.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to inspect.

    Returns:
        One row per linked extension, with its type and content.
    """
    return _campaign_extensions(_clean_id(customer_id), _clean_id(campaign_id))


@extensions_mcp.tool
def remove_campaign_extension(
    customer_id: str,
    campaign_id: str,
    asset_id: str,
    field_type: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Unlinks an extension from a campaign.

    This removes the LINK, not the asset: the asset stays in the account and
    can be linked again, so this is recoverable. It still needs confirmation
    because the ad loses that extension immediately.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to remove it from.
        asset_id: The asset's id, from list_campaign_extensions.
        field_type: SITELINK, CALLOUT or STRUCTURED_SNIPPET.
        confirm_remove: Must be True. Say which extension is going first.

    Returns:
        The campaign's remaining extensions.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    asset_id = _clean_id(asset_id)
    field_type = field_type.upper()
    if field_type not in {"SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"}:
        raise ValueError(
            "field_type must be SITELINK, CALLOUT or STRUCTURED_SNIPPET."
        )
    if not confirm_remove:
        raise ValueError(
            "Removing this extension takes it off the ad immediately. The "
            "asset itself stays in the account and can be re-linked. Name "
            "which extension is going, then call again with "
            "confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignAssetService")
    operation = client.get_type("CampaignAssetOperation")
    operation.remove = service.campaign_asset_path(
        customer_id, campaign_id, asset_id, field_type
    )
    service.mutate_campaign_assets(
        customer_id=customer_id, operations=[operation]
    )

    return {
        "campaign_id": campaign_id,
        "removed_asset_id": asset_id,
        "field_type": field_type,
        "note": "The asset stays in the account and can be linked again.",
        "verified": _campaign_extensions(customer_id, campaign_id),
    }
