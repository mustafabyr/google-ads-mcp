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

"""Write tools for ad extensions (assets): sitelinks, callouts, snippets,
call, price.

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

import base64
import datetime
import hashlib
import os
from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from google.protobuf import field_mask_pb2
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
_MAX_PRICE_HEADER = 25
_MAX_PRICE_DESC = 25
_MIN_PRICE_OFFERINGS = 3
_MAX_PRICE_OFFERINGS = 8

# 1 unit of the account currency = 1_000_000 micros. Same rule as everywhere
# else in this repo: the tool takes the human number and converts here.
_MICROS = 1_000_000

# What a price table's rows ARE. Not free text -- Google's own enum.
_PRICE_TYPES = {
    "BRANDS", "EVENTS", "LOCATIONS", "NEIGHBORHOODS", "PRODUCT_CATEGORIES",
    "PRODUCT_TIERS", "SERVICES", "SERVICE_CATEGORIES", "SERVICE_TIERS",
}
_PRICE_QUALIFIERS = {"FROM", "UP_TO", "AVERAGE"}
_PRICE_UNITS = {
    "PER_HOUR", "PER_DAY", "PER_WEEK", "PER_MONTH", "PER_YEAR", "PER_NIGHT",
}

# A price asset is not available everywhere, and the API only says so after
# the write is attempted: "The language code is not supported" and "The
# currency code is not supported", one INVALID_ARGUMENT for each. Google's
# own help page lists ten languages and twenty-three currencies, and Turkish
# and the Turkish lira are in neither -- so for a Turkish account in TRY the
# price extension does not exist at all, however well-formed the rows are.
# Checked here so the refusal carries that explanation instead of an opaque
# INVALID_ARGUMENT. See docs/kaynaklar.md rows 53-54.
_PRICE_LANGUAGES = {
    "en", "nl", "fr", "de", "it", "ja", "pl", "pt", "es", "sv",
}
_PRICE_CURRENCIES = {
    "ARS", "AUD", "BOB", "BRL", "CAD", "CHF", "CLP", "COP", "EUR", "GBP",
    "HKD", "INR", "JPY", "MXN", "NZD", "PEN", "PHP", "PLN", "SEK", "SGD",
    "USD", "VEF", "ZAR",
}
_PRICE_MARKET_HELP = (
    "https://support.google.com/google-ads/answer/7159010 lists the "
    "languages and currencies price assets support. Where they are not "
    "supported the extension cannot be created at all -- carry the figures "
    "in sitelink descriptions or callouts instead, which are separate "
    "assets and can be replaced without sending the ad back to review."
)

# Every extension this module can CREATE, it can also unlink. A create tool
# without its remove leaves the account dirty and the next session guessing,
# so this set tracks the add_* tools above -- add a type here in the same
# commit that adds the tool which creates it.
_REMOVABLE_FIELD_TYPES = {
    "SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "CALL", "PRICE",
    "MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE",
}

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


# Which asset sub-message carries the content, per extension type. The raw
# proto puts every unset sub-message in the row as well, so a listing that
# returns rows verbatim is mostly empty scaffolding -- 13 extensions came back
# as roughly 8k tokens of it. One entry per type keeps the output to what the
# extension actually says.
_ASSET_CONTENT_FIELD = {
    "SITELINK": "sitelink_asset",
    "CALLOUT": "callout_asset",
    "STRUCTURED_SNIPPET": "structured_snippet_asset",
    "CALL": "call_asset",
    "PROMOTION": "promotion_asset",
    "PRICE": "price_asset",
}

# LOCATION is deliberately absent above. It is a valid AssetFieldType, but
# `campaign_asset.field_type = 'LOCATION'` is rejected by GAQL with
# BAD_ENUM_CONSTANT: a campaign's addresses are not campaign_assets at all,
# they arrive through an AssetSet synced from Business Profile. Use
# list_location_asset_sets for those.


def _price_offering_out(offering: Dict[str, Any]) -> Dict[str, Any]:
    """One row of a price table as a human reads it, money out of micros."""
    money = offering.get("price") or {}
    micros = money.get("amount_micros")
    out: Dict[str, Any] = {
        "header": offering.get("header"),
        "description": offering.get("description"),
        "price": (
            int(micros) / _MICROS if str(micros or "").strip() else None
        ),
        "currency": money.get("currency_code"),
        "final_url": offering.get("final_url"),
    }
    unit = str(_enum(offering, "unit") or "")
    if unit and unit not in ("UNSPECIFIED", "UNKNOWN"):
        out["unit"] = unit
    return out


def _flatten_extension(row: Dict[str, Any]) -> Dict[str, Any]:
    """Turns one campaign_asset row into a flat, readable record."""
    # One shape for both link levels: a campaign_asset row (the extension
    # sits on one campaign) or a customer_asset row (it sits on the whole
    # account and every campaign without its own of that type inherits it).
    link_key = "customer_asset" if "customer_asset" in row else "campaign_asset"
    link = row.get(link_key, {})
    asset = row.get("asset", {})
    field_type = link.get("field_type")

    out: Dict[str, Any] = {
        "type": field_type,
        "status": link.get("status"),
        "asset_id": asset.get("id"),
        "asset_name": asset.get("name"),
        f"{link_key}_resource_name": link.get("resource_name"),
    }

    content = asset.get(_ASSET_CONTENT_FIELD.get(field_type, ""), {})
    for key, value in content.items():
        # ad_schedule_targets is an empty list on virtually every asset and
        # start/end date are empty strings unless someone set them.
        if key == "ad_schedule_targets" or value in ("", [], None):
            continue
        out[key] = value

    urls = asset.get("final_urls") or []
    if urls:
        out["final_urls"] = urls

    # The broken state a call extension can sit in: it says "count calls
    # against my own conversion action" and names none, so the button
    # shows, people call, and the call is counted nowhere.
    if field_type == "CALL":
        state = str(content.get("call_conversion_reporting_state") or "")
        if state == "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION" and not (
            content.get("call_conversion_action")
        ):
            out["warning"] = (
                "call_conversion_action is EMPTY while the reporting state "
                "is USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION: calls from "
                "this button are counted nowhere. Repoint it with "
                "update_call_extension(use_account_default=True)."
            )

    if field_type == "PRICE":
        # The generic loop above copied the sub-message verbatim: the rows as
        # protos with money in micros, and the price extension's OWN type
        # under proto-plus's renamed "type_" key -- which reads as a second,
        # contradictory "type" next to this record's asset field type.
        out.pop("type_", None)
        price_type = _enum(content, "type")
        if price_type and str(price_type) not in ("UNSPECIFIED", "UNKNOWN"):
            out["price_type"] = price_type
        if str(out.get("price_qualifier") or "") in ("UNSPECIFIED", "UNKNOWN"):
            out.pop("price_qualifier", None)
        out["offerings"] = [
            _price_offering_out(row)
            for row in (out.pop("price_offerings", None) or [])
        ]
    return out


def _campaign_extensions(customer_id: str, campaign_id: str) -> List[Dict]:
    """Lists a campaign's extensions as flat records, one per link."""
    rows = _read_back(
        customer_id,
        f"""
        SELECT campaign.id,
               campaign_asset.resource_name,
               campaign_asset.field_type, campaign_asset.status,
               asset.id, asset.name,
               asset.sitelink_asset.link_text,
               asset.sitelink_asset.description1,
               asset.sitelink_asset.description2,
               asset.callout_asset.callout_text,
               asset.structured_snippet_asset.header,
               asset.structured_snippet_asset.values,
               asset.call_asset.phone_number,
               asset.call_asset.country_code,
               asset.call_asset.call_conversion_reporting_state,
               asset.call_asset.call_conversion_action,
               asset.promotion_asset.promotion_target,
               asset.promotion_asset.discount_modifier,
               asset.promotion_asset.percent_off,
               asset.price_asset.type,
               asset.price_asset.price_qualifier,
               asset.price_asset.language_code,
               asset.price_asset.price_offerings,
               asset.final_urls
        FROM campaign_asset
        WHERE campaign.id = {campaign_id}
          AND campaign_asset.field_type IN
              ('SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET', 'CALL',
               'PROMOTION', 'PRICE')
          AND campaign_asset.status != 'REMOVED'
        """,
    )
    return [_flatten_extension(row) for row in rows]


# Field types an ACCOUNT-level link can carry and this server can unlink.
_ACCOUNT_REMOVABLE_FIELD_TYPES = {
    "SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "CALL", "PRICE",
    "PROMOTION",
}


def _account_extensions(
    customer_id: str,
    asset_id: Optional[str] = None,
    field_type: Optional[str] = None,
) -> List[Dict]:
    """Lists the account-level (customer_asset) extension links.

    With asset_id and field_type it narrows to that one link, which is what
    remove_account_extension reads before it writes -- so a plan carries the
    link's real before-state and refuses if it changed in between.
    """
    where = [
        "customer_asset.status != 'REMOVED'",
        "customer_asset.field_type IN ('SITELINK', 'CALLOUT', "
        "'STRUCTURED_SNIPPET', 'CALL', 'PROMOTION', 'PRICE')",
    ]
    if asset_id:
        where.append(f"asset.id = {asset_id}")
    if field_type:
        where.append(f"customer_asset.field_type = '{field_type}'")
    rows = _read_back(
        customer_id,
        f"""
        SELECT customer_asset.resource_name,
               customer_asset.field_type, customer_asset.status,
               asset.id, asset.name,
               asset.sitelink_asset.link_text,
               asset.sitelink_asset.description1,
               asset.sitelink_asset.description2,
               asset.callout_asset.callout_text,
               asset.structured_snippet_asset.header,
               asset.structured_snippet_asset.values,
               asset.call_asset.phone_number,
               asset.call_asset.country_code,
               asset.call_asset.call_conversion_reporting_state,
               asset.call_asset.call_conversion_action,
               asset.promotion_asset.promotion_target,
               asset.promotion_asset.discount_modifier,
               asset.promotion_asset.percent_off,
               asset.price_asset.type,
               asset.price_asset.price_qualifier,
               asset.price_asset.language_code,
               asset.price_asset.price_offerings,
               asset.final_urls
        FROM customer_asset
        WHERE {" AND ".join(where)}
        """,
    )
    return [_flatten_extension(row) for row in rows]


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
    """Lists the extensions linked to a campaign, as flat records.

    Covers sitelinks, callouts, structured snippets, call and promotion
    assets. Each row is the extension's own content plus its asset_id and
    status -- not the raw proto, which is mostly unset fields. Address
    extensions are NOT here: they come from an asset set synced from
    Business Profile, which list_location_asset_sets reports.

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
        field_type: SITELINK, CALLOUT, STRUCTURED_SNIPPET, CALL or PRICE.
        confirm_remove: Must be True. Say which extension is going first.

    Returns:
        The campaign's remaining extensions.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    asset_id = _clean_id(asset_id)
    field_type = field_type.upper()
    if field_type not in _REMOVABLE_FIELD_TYPES:
        raise ValueError(
            f"field_type must be one of {sorted(_REMOVABLE_FIELD_TYPES)}."
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


@extensions_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_account_extensions(customer_id: str) -> List[Dict[str, Any]]:
    """Lists the extensions linked at ACCOUNT level, as flat records.

    An account-level extension is inherited by every campaign that has no
    extension of that type of its own. That makes an old account-level
    sitelink the quiet way a forbidden claim ("Garantili", "%100") or a dead
    URL reaches a brand-new campaign: the campaign's own list looks clean,
    and the ad still shows the inherited one. list_campaign_extensions does
    not show these -- check here on every inherited account and before
    launching a campaign without its own sitelinks or callouts.

    Args:
        customer_id: The Google Ads account id.

    Returns:
        One row per account-level link, with its type, content and asset_id.
    """
    return _account_extensions(_clean_id(customer_id))


@extensions_mcp.tool
def remove_account_extension(
    customer_id: str,
    asset_id: str,
    field_type: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Unlinks an extension from the ACCOUNT level.

    Removes the account-level LINK, not the asset: the asset stays in the
    account and can be linked again (to the account or to a campaign), so
    this is recoverable. Every campaign that was inheriting the extension --
    that is, every campaign with no extension of that type of its own --
    stops showing it immediately, which is why it needs confirmation.
    Campaign-level links are untouched; use remove_campaign_extension for
    those.

    Args:
        customer_id: The Google Ads account id.
        asset_id: The asset's id, from list_account_extensions.
        field_type: SITELINK, CALLOUT, STRUCTURED_SNIPPET, CALL, PRICE or
            PROMOTION.
        confirm_remove: Must be True. Say which extension is going first.

    Returns:
        The removed link and the account-level extensions that remain.
    """
    customer_id = _clean_id(customer_id)
    asset_id = _clean_id(asset_id)
    field_type = field_type.upper()
    if field_type not in _ACCOUNT_REMOVABLE_FIELD_TYPES:
        raise ValueError(
            "field_type must be one of "
            f"{sorted(_ACCOUNT_REMOVABLE_FIELD_TYPES)}."
        )
    if not confirm_remove:
        raise ValueError(
            "Removing an account-level extension takes it off every campaign "
            "that inherits it, immediately. The asset itself stays in the "
            "account and can be re-linked. Name which extension is going, "
            "then call again with confirm_remove=True."
        )

    # Read the link first: the plan records it as the before-state, and a
    # link that is not there is refused here rather than as an API error.
    before = _account_extensions(customer_id, asset_id, field_type)
    if not before:
        raise ValueError(
            f"No account-level {field_type} link for asset {asset_id}. "
            "Check list_account_extensions -- it may be linked to a "
            "campaign instead (remove_campaign_extension), or already gone."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CustomerAssetService")
    operation = client.get_type("CustomerAssetOperation")
    operation.remove = service.customer_asset_path(
        customer_id, asset_id, field_type
    )
    service.mutate_customer_assets(
        customer_id=customer_id, operations=[operation]
    )

    return {
        "removed_asset_id": asset_id,
        "field_type": field_type,
        "removed": before[0],
        "note": (
            "The asset stays in the account and can be linked again. "
            "Campaigns with their own extension of this type were never "
            "showing it."
        ),
        "verified": _account_extensions(customer_id),
    }


@extensions_mcp.tool
def add_call_extension(
    customer_id: str,
    campaign_id: str,
    phone_number: str,
    country_code: str = "TR",
    call_conversion_action_id: Optional[str] = None,
    disable_call_conversion: bool = False,
    replace_existing: bool = False,
) -> Dict[str, Any]:
    """Adds a call extension, and with it the only biddable measure of calls.

    For a local business this is usually the highest-value extension there
    is: it puts a call button on the ad, and -- unlike the "click to call"
    action that comes from the Business Profile -- the calls it produces
    become a NORMAL conversion action that smart bidding can actually bid
    toward. Google-hosted local actions cannot; they are immutable and the
    API refuses to make them biddable.

    Measurement rides on a Google forwarding number, which Google swaps in
    for the real one so it can time the call. That needs call reporting
    switched on at account level -- this tool reads that setting back and
    says so when it is off, because with it off the extension still shows
    but nothing is measured.

    Extensions cannot raise spend beyond the campaign budget, so this needs
    no confirmation.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to add the call button to.
        phone_number: The business number as a local caller would dial it,
            e.g. "0530 407 49 50". Spaces and dashes are stripped.
        country_code: Two-letter country of the number, e.g. "TR".
        call_conversion_action_id: Count calls against THIS conversion action
            instead of the account default. Omit to use the account default,
            which is what most accounts want.
        disable_call_conversion: True to show the button but measure nothing.
            Only for an account that already tracks calls another way.
        replace_existing: A campaign with a call extension already gets the
            existing one back untouched, since a second one is duplication,
            not safety. True removes the old link first.

    Returns:
        The asset created, the account's call reporting state, and the
        campaign's extensions read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    country_code = str(country_code).strip().upper()
    if len(country_code) != 2 or not country_code.isalpha():
        raise ValueError(
            f"country_code must be two letters, e.g. 'TR': {country_code!r}"
        )

    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if not 7 <= len(digits) <= 15:
        raise ValueError(
            f"phone_number has {len(digits)} digits, expected 7-15: "
            f"{phone_number!r}"
        )
    if call_conversion_action_id and disable_call_conversion:
        raise ValueError(
            "call_conversion_action_id and disable_call_conversion "
            "contradict each other: pick one."
        )

    existing = [
        row
        for row in _campaign_extensions(customer_id, campaign_id)
        if row.get("type") == "CALL"
    ]
    if existing and not replace_existing:
        return {
            "campaign_id": campaign_id,
            "created": False,
            "note": (
                "This campaign already has a call extension. A second one "
                "duplicates the same slot rather than adding anything. Call "
                "again with replace_existing=True to swap the number."
            ),
            "existing": existing,
        }

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = f"Call: +{country_code} {digits}"
    asset.call_asset.country_code = country_code
    asset.call_asset.phone_number = digits

    states = client.enums.CallConversionReportingStateEnum
    if disable_call_conversion:
        asset.call_asset.call_conversion_reporting_state = states.DISABLED
    elif call_conversion_action_id:
        conversion_service = utils.get_googleads_service(
            "ConversionActionService"
        )
        asset.call_asset.call_conversion_action = (
            conversion_service.conversion_action_path(
                customer_id, _clean_id(call_conversion_action_id)
            )
        )
        asset.call_asset.call_conversion_reporting_state = (
            states.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )
    else:
        asset.call_asset.call_conversion_reporting_state = (
            states.USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION
        )

    response = asset_service.mutate_assets(
        customer_id=customer_id, operations=[operation]
    )
    asset_resource_name = response.results[0].resource_name

    if existing and replace_existing:
        campaign_asset_service = utils.get_googleads_service(
            "CampaignAssetService"
        )
        removals = []
        for row in existing:
            # The rows come from _flatten_extension, which puts the link's
            # resource name under this flat key -- not under a nested
            # "campaign_asset" dict. Reading the nested key raised KeyError
            # on every replace_existing=True call.
            removal = client.get_type("CampaignAssetOperation")
            removal.remove = row["campaign_asset_resource_name"]
            removals.append(removal)
        if removals:
            campaign_asset_service.mutate_campaign_assets(
                customer_id=customer_id, operations=removals
            )

    _link_assets_to_campaign(
        customer_id, campaign_id, [asset_resource_name], "CALL"
    )

    # With call reporting off the button still shows, but no call is timed
    # and nothing lands in any conversion action -- the whole reason for
    # adding this. Say so plainly rather than reporting a clean success.
    reporting = _read_back(
        customer_id,
        """
        SELECT customer.id,
               customer.call_reporting_setting.call_reporting_enabled,
               customer.call_reporting_setting.call_conversion_reporting_enabled
        FROM customer
        """,
    )
    setting = (
        reporting[0]["customer"].get("call_reporting_setting", {})
        if reporting
        else {}
    )
    warnings = []
    if not setting.get("call_reporting_enabled"):
        warnings.append(
            "Account-level call reporting is OFF: the call button will show "
            "but no Google forwarding number is used, so no call is measured."
        )
    if not disable_call_conversion and not setting.get(
        "call_conversion_reporting_enabled"
    ):
        warnings.append(
            "Account-level call CONVERSION reporting is OFF: calls will not "
            "become conversions, so smart bidding still has nothing to bid "
            "toward."
        )

    return {
        "campaign_id": campaign_id,
        "created": True,
        "asset_resource_name": asset_resource_name,
        "phone_number": digits,
        "country_code": country_code,
        "call_reporting_setting": setting,
        "warnings": warnings,
        "verified": _campaign_extensions(customer_id, campaign_id),
    }


@extensions_mcp.tool
def update_call_extension(
    customer_id: str,
    asset_id: str,
    call_conversion_action_id: Optional[str] = None,
    use_account_default: bool = False,
    disable_call_conversion: bool = False,
) -> Dict[str, Any]:
    """Repoints an existing call extension at a conversion action, in place.

    The common broken state, and the reason this exists: an extension left on
    USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION with no action actually set.
    The button shows, people call, and the call lands nowhere. Removing and
    re-creating the asset would fix it too, but that detaches the extension's
    history -- so change the field instead.

    Pass exactly one of use_account_default, call_conversion_action_id or
    disable_call_conversion.

    Args:
        customer_id: The Google Ads account id.
        asset_id: The call asset to fix, from list_campaign_extensions.
        call_conversion_action_id: Count calls against THIS action.
        use_account_default: Count calls against the account's own call
            conversion action. This is the right answer for most accounts,
            and the fix for the empty-resource-level state above.
        disable_call_conversion: Show the button, measure nothing.

    Returns:
        The asset before and after, plus the account's call reporting state --
        which has to be on for any of this to measure anything.
    """
    customer_id = _clean_id(customer_id)
    asset_id = _clean_id(asset_id)

    chosen = [
        bool(use_account_default),
        bool(call_conversion_action_id),
        bool(disable_call_conversion),
    ]
    if sum(chosen) != 1:
        raise ValueError(
            "Pass exactly one of use_account_default, "
            "call_conversion_action_id or disable_call_conversion."
        )

    before = _read_back(
        customer_id,
        f"""
        SELECT asset.id, asset.name, asset.type,
               asset.call_asset.phone_number,
               asset.call_asset.country_code,
               asset.call_asset.call_conversion_reporting_state,
               asset.call_asset.call_conversion_action
        FROM asset
        WHERE asset.id = {asset_id}
        """,
    )
    if not before:
        raise ValueError(f"No asset {asset_id} in account {customer_id}.")
    if before[0]["asset"].get("type_") != "CALL":
        raise ValueError(
            f"Asset {asset_id} is a {before[0]['asset'].get('type_')} asset, "
            f"not a CALL asset."
        )

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")
    states = client.enums.CallConversionReportingStateEnum

    operation = client.get_type("AssetOperation")
    asset = operation.update
    asset.resource_name = asset_service.asset_path(customer_id, asset_id)

    paths = ["call_asset.call_conversion_reporting_state"]
    if disable_call_conversion:
        asset.call_asset.call_conversion_reporting_state = states.DISABLED
    elif use_account_default:
        asset.call_asset.call_conversion_reporting_state = (
            states.USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION
        )
    else:
        conversion_service = utils.get_googleads_service(
            "ConversionActionService"
        )
        asset.call_asset.call_conversion_action = (
            conversion_service.conversion_action_path(
                customer_id, _clean_id(call_conversion_action_id)
            )
        )
        asset.call_asset.call_conversion_reporting_state = (
            states.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )
        paths.append("call_asset.call_conversion_action")

    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )
    asset_service.mutate_assets(
        customer_id=customer_id, operations=[operation]
    )

    reporting = _read_back(
        customer_id,
        """
        SELECT customer.id,
               customer.call_reporting_setting.call_reporting_enabled,
               customer.call_reporting_setting.call_conversion_reporting_enabled,
               customer.call_reporting_setting.call_conversion_action
        FROM customer
        """,
    )
    setting = (
        reporting[0]["customer"].get("call_reporting_setting", {})
        if reporting
        else {}
    )
    warnings = []
    if not setting.get("call_reporting_enabled"):
        warnings.append(
            "Account-level call reporting is OFF: no Google forwarding "
            "number is used, so no call is measured whatever this asset says."
        )
    if use_account_default and not setting.get("call_conversion_action"):
        warnings.append(
            "use_account_default was chosen but the account has no call "
            "conversion action set, so there is still nothing to count into."
        )

    return {
        "asset_id": asset_id,
        "updated_fields": paths,
        "before": before[0]["asset"],
        "after": _read_back(
            customer_id,
            f"""
            SELECT asset.id, asset.name,
                   asset.call_asset.phone_number,
                   asset.call_asset.country_code,
                   asset.call_asset.call_conversion_reporting_state,
                   asset.call_asset.call_conversion_action
            FROM asset
            WHERE asset.id = {asset_id}
            """,
        )[0]["asset"],
        "call_reporting_setting": setting,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Price extensions
#
# A price extension is the only place a figure belongs in a search ad. A price
# typed into a headline goes stale silently: it survives Google review, the ad
# keeps serving the old number, and nothing in a report looks wrong. A price
# asset carries the figure as data, shows the currency itself, and gives every
# row its own landing page.
#
# Its rows cannot be edited. Refreshing a price list means creating a new
# asset and unlinking the old one, which is what replace_existing does.
# --------------------------------------------------------------------------


@extensions_mcp.tool
def add_price_asset(
    customer_id: str,
    campaign_id: str,
    offerings: List[Dict[str, Any]],
    price_type: str = "PRODUCT_TIERS",
    price_qualifier: Optional[str] = "FROM",
    language_code: str = "en",
    currency_code: Optional[str] = None,
    asset_name: Optional[str] = None,
    replace_existing: bool = False,
) -> Dict[str, Any]:
    """Adds a price extension: a table of 3-8 priced rows under the ad.

    NOT AVAILABLE IN EVERY MARKET. Google supports price assets in ten
    languages and twenty-three currencies; Turkish and the Turkish lira are
    in neither, so a TRY account cannot have a price extension at all. This
    tool refuses such a call outright rather than letting the API answer
    with two opaque INVALID_ARGUMENTs after the fact, and says what to use
    instead.

    Prices are sent in the ACCOUNT's own currency. This tool reads that
    currency and refuses a different one rather than converting. Give the
    human number: 2850000 means 2.850.000 in a TRY account.

    A price asset's rows are IMMUTABLE, and so is its name. Changing a price
    is therefore a new asset plus replace_existing=True, not an edit -- and
    the new table goes through Google review before it can show, so a price
    that changes monthly is a monthly call, not a one-off setup.

    Extensions cannot raise spend beyond the campaign budget, so this needs
    no confirmation of its own.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to add the table to.
        offerings: 3-8 rows, e.g.
            [{"header": "1+1 Daire", "description": "Pesin fiyat",
              "price": 2850000, "url": "https://site.com/prices"}]
            "header" and "description" are at most 25 characters each and
            must DIFFER from each other -- Google rejects a row whose
            description repeats its header. "url" is that row's own landing
            page. "unit" is optional: PER_HOUR, PER_DAY, PER_WEEK, PER_MONTH,
            PER_YEAR or PER_NIGHT.
        price_type: What the rows are: PRODUCT_TIERS (default), BRANDS,
            EVENTS, LOCATIONS, NEIGHBORHOODS, PRODUCT_CATEGORIES, SERVICES,
            SERVICE_CATEGORIES or SERVICE_TIERS.
        price_qualifier: FROM (default), UP_TO, AVERAGE, or None for none.
            FROM is what keeps a starting price honest -- without it the
            number reads as the price of every unit in the row.
        language_code: BCP 47 tag of the row text, and one Google supports
            for price assets: en, nl, fr, de, it, ja, pl, pt, es or sv.
        currency_code: Three letters, and one Google supports for price
            assets. Omit to use the account's own currency, which is what it
            should be -- and if that currency is unsupported, the account
            cannot carry a price extension at all.
        asset_name: Overrides the generated name. Names are immutable and
            must be unique in the account, so the default carries the date
            and a digest of the rows: the same list twice on the same day is
            a real duplicate and Google says so.
        replace_existing: A campaign that already has a price extension gets
            the existing one back untouched -- only one price table shows at
            a time, so a second competes for the same slot. True unlinks the
            old link first, which is how a price list is refreshed.

    Returns:
        The asset created, the rows as sent, and the campaign's extensions
        read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    price_type = str(price_type).strip().upper()
    if price_type not in _PRICE_TYPES:
        raise ValueError(
            f"price_type must be one of {sorted(_PRICE_TYPES)}, got "
            f"{price_type!r}."
        )
    qualifier = (
        str(price_qualifier).strip().upper() if price_qualifier else None
    )
    if qualifier and qualifier not in _PRICE_QUALIFIERS:
        raise ValueError(
            f"price_qualifier must be one of {sorted(_PRICE_QUALIFIERS)} or "
            f"None, got {price_qualifier!r}."
        )
    language_code = str(language_code).strip()
    if not language_code:
        raise ValueError("language_code is required, e.g. 'en'.")
    if language_code.replace("_", "-").split("-")[0].lower() not in (
        _PRICE_LANGUAGES
    ):
        raise ValueError(
            f"Google does not support price assets in {language_code!r}. "
            f"Supported: {sorted(_PRICE_LANGUAGES)}. {_PRICE_MARKET_HELP}"
        )
    if not _MIN_PRICE_OFFERINGS <= len(offerings) <= _MAX_PRICE_OFFERINGS:
        raise ValueError(
            f"A price extension holds {_MIN_PRICE_OFFERINGS}-"
            f"{_MAX_PRICE_OFFERINGS} rows, got {len(offerings)}. Fewer than "
            f"{_MIN_PRICE_OFFERINGS} is rejected outright, not shown short."
        )

    # The account's own currency. Sending anything else would mean a
    # converted price, which this server never does.
    account = _read_back(
        customer_id,
        "SELECT customer.id, customer.currency_code FROM customer",
    )
    account_currency = (
        account[0]["customer"].get("currency_code") if account else None
    )
    if currency_code:
        currency_code = str(currency_code).strip().upper()
        if len(currency_code) != 3 or not currency_code.isalpha():
            raise ValueError(
                f"currency_code must be three letters, e.g. 'TRY': "
                f"{currency_code!r}"
            )
        if account_currency and currency_code != account_currency:
            raise ValueError(
                f"The account bills in {account_currency} and you passed "
                f"{currency_code}. Prices go out in the account's own "
                f"currency -- this server does not convert. Pass "
                f"{account_currency}, or omit currency_code."
            )
    elif account_currency:
        currency_code = account_currency
    else:
        raise ValueError(
            "Could not read the account's currency; pass currency_code."
        )
    if currency_code not in _PRICE_CURRENCIES:
        raise ValueError(
            f"Google does not support price assets priced in "
            f"{currency_code}"
            + (
                " -- which is this account's own currency, so a price asset "
                "cannot be built for this account at all."
                if currency_code == account_currency
                else "."
            )
            + f" Supported: {sorted(_PRICE_CURRENCIES)}. "
            f"{_PRICE_MARKET_HELP}"
        )

    rows: List[Dict[str, Any]] = []
    seen_headers = set()
    for entry in offerings:
        header = str(entry.get("header") or "").strip()
        description = str(entry.get("description") or "").strip()
        url = str(entry.get("url") or entry.get("final_url") or "").strip()
        if not header or not description or not url:
            raise ValueError(
                f"Every row needs 'header', 'description' and 'url': {entry!r}"
            )
        _check_len("Price header", header, _MAX_PRICE_HEADER)
        _check_len("Price description", description, _MAX_PRICE_DESC)
        if header.casefold() == description.casefold():
            raise ValueError(
                f"A row's description cannot repeat its header -- Google "
                f"rejects it as PRICE_HEADER_SAME_AS_DESCRIPTION: {header!r}"
            )
        if header.casefold() in seen_headers:
            raise ValueError(
                f"Two rows carry the header {header!r}. Each row names a "
                f"different thing, or the table says the same thing twice."
            )
        seen_headers.add(header.casefold())
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://: {url}")

        raw_price = entry.get("price")
        try:
            price_value = float(raw_price)
        except (TypeError, ValueError):
            raise ValueError(
                f"'price' must be a number in {currency_code} -- the human "
                f"number, not micros: {raw_price!r}"
            ) from None
        if price_value <= 0:
            raise ValueError(f"'price' must be above zero: {raw_price!r}")

        unit = entry.get("unit")
        if unit:
            unit = str(unit).strip().upper()
            if unit not in _PRICE_UNITS:
                raise ValueError(
                    f"'unit' must be one of {sorted(_PRICE_UNITS)}: {unit!r}"
                )
        rows.append(
            {
                "header": header,
                "description": description,
                "price": price_value,
                "amount_micros": int(round(price_value * _MICROS)),
                "unit": unit or None,
                "final_url": url,
            }
        )

    existing = [
        row
        for row in _campaign_extensions(customer_id, campaign_id)
        if row.get("type") == "PRICE"
    ]
    if existing and not replace_existing:
        return {
            "campaign_id": campaign_id,
            "created": False,
            "note": (
                "This campaign already has a price extension, and only one "
                "price table shows at a time, so a second would compete for "
                "the same slot. A price asset cannot be edited in place: "
                "call again with replace_existing=True to swap the whole "
                "list, which is how a refresh is done."
            ),
            "existing": existing,
        }

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    if asset_name:
        name = str(asset_name).strip()
    else:
        digest = hashlib.sha1(
            "|".join(
                f"{r['header']}~{r['description']}~{r['amount_micros']}"
                f"~{r['unit']}~{r['final_url']}"
                for r in rows
            ).encode("utf-8")
        ).hexdigest()[:6]
        today = datetime.date.today().isoformat()
        name = f"Price {price_type} {today} {digest}"

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = name
    asset.price_asset.type_ = getattr(
        client.enums.PriceExtensionTypeEnum, price_type
    )
    if qualifier:
        asset.price_asset.price_qualifier = getattr(
            client.enums.PriceExtensionPriceQualifierEnum, qualifier
        )
    asset.price_asset.language_code = language_code
    for row in rows:
        offering = client.get_type("PriceOffering")
        offering.header = row["header"]
        offering.description = row["description"]
        offering.price.currency_code = currency_code
        offering.price.amount_micros = row["amount_micros"]
        offering.final_url = row["final_url"]
        if row["unit"]:
            offering.unit = getattr(
                client.enums.PriceExtensionPriceUnitEnum, row["unit"]
            )
        asset.price_asset.price_offerings.append(offering)

    response = asset_service.mutate_assets(
        customer_id=customer_id, operations=[operation]
    )
    asset_resource_name = response.results[0].resource_name

    # The new table is linked FIRST and the old one unlinked after, so the
    # campaign is never a moment without a price extension. Google allows
    # both links to exist; what it will not do is show two at once.
    _link_assets_to_campaign(
        customer_id, campaign_id, [asset_resource_name], "PRICE"
    )

    replaced: List[Any] = []
    if existing and replace_existing:
        campaign_asset_service = utils.get_googleads_service(
            "CampaignAssetService"
        )
        removals = []
        for row in existing:
            # _flatten_extension puts the LINK's resource name under this
            # flat key, not under a nested "campaign_asset" dict.
            removal = client.get_type("CampaignAssetOperation")
            removal.remove = row["campaign_asset_resource_name"]
            removals.append(removal)
            replaced.append(row.get("asset_id"))
        if removals:
            campaign_asset_service.mutate_campaign_assets(
                customer_id=customer_id, operations=removals
            )

    return {
        "campaign_id": campaign_id,
        "created": True,
        "asset_resource_name": asset_resource_name,
        "asset_name": name,
        "price_type": price_type,
        "price_qualifier": qualifier,
        "language_code": language_code,
        "currency_code": currency_code,
        "rows": [
            {key: value for key, value in row.items() if key != "amount_micros"}
            for row in rows
        ],
        "replaced_asset_ids": replaced,
        "note": (
            "The table goes to Google review before it can show. Its rows "
            "and its name are immutable: the next price change is a new "
            "asset with replace_existing=True, not an edit of this one."
        ),
        "verified": _campaign_extensions(customer_id, campaign_id),
    }


# --------------------------------------------------------------------------
# Image assets
#
# On Search, the SQUARE image is the required one and the landscape is
# optional -- the opposite of what the aspect ratios suggest. Google decides
# which slot an image goes in by its ratio, so the caller does not pick:
# this module reads the pixels and routes it, which is also what stops a
# 4:1 logo being linked as a marketing image. Google's format policy bans
# "brand logos used as the image", and a logo's ratio gives it away before
# the file ever leaves the process.
# --------------------------------------------------------------------------

_MAX_IMAGE_BYTES = 5_120 * 1024
_MAX_CAMPAIGN_IMAGES = 20
_MIN_SQUARE = (300, 300)
_MIN_LANDSCAPE = (600, 314)
_RATIO_TOLERANCE = 0.02
_IMAGE_FIELD_TYPES = ("MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE")


def _image_size(data: bytes) -> tuple:
    """Width and height from a PNG or JPEG header, without a decoder.

    The ratio decides which slot the image can go in and whether it is
    allowed at all, so it has to be known BEFORE the upload -- not read back
    from an asset that should never have been created.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if data[12:16] != b"IHDR":
            raise ValueError("PNG header is malformed: no IHDR chunk.")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height

    if data[:2] == b"\xff\xd8":
        index = 2
        end = len(data)
        while index < end - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            # Standalone markers carry no length.
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(data[index + 2:index + 4], "big")
            # SOF0-SOF15, minus the two that are not frame headers.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 5:index + 7], "big")
                width = int.from_bytes(data[index + 7:index + 9], "big")
                return width, height
            index += 2 + length
        raise ValueError("JPEG has no frame header: dimensions unreadable.")

    raise ValueError(
        "Only PNG and JPEG are accepted -- those are the two formats Google "
        "takes for an image asset."
    )


def _image_field_type(width: int, height: int) -> str:
    """Which slot this shape belongs in, or why it belongs in none."""
    if not width or not height:
        raise ValueError(f"Unusable image dimensions: {width}x{height}.")
    ratio = width / height

    if abs(ratio - 1.0) <= _RATIO_TOLERANCE:
        if (width, height) < _MIN_SQUARE:
            raise ValueError(
                f"A square image must be at least {_MIN_SQUARE[0]}x"
                f"{_MIN_SQUARE[1]} (1200x1200 recommended); this is "
                f"{width}x{height}."
            )
        return "SQUARE_MARKETING_IMAGE"

    if abs(ratio - 1.91) <= 1.91 * _RATIO_TOLERANCE:
        if width < _MIN_LANDSCAPE[0] or height < _MIN_LANDSCAPE[1]:
            raise ValueError(
                f"A landscape image must be at least {_MIN_LANDSCAPE[0]}x"
                f"{_MIN_LANDSCAPE[1]} (1200x628 recommended); this is "
                f"{width}x{height}."
            )
        return "MARKETING_IMAGE"

    raise ValueError(
        f"{width}x{height} is a ratio of {ratio:.2f}:1, and an image asset "
        f"is either 1:1 (square, required on Search) or 1.91:1 (landscape, "
        f"optional). A 4:1 or similar wordmark is a LOGO, and Google's "
        f"format policy refuses a brand logo used as the image itself -- "
        f"link it as a logo asset instead, not here."
    )


def _campaign_images(
    customer_id: str, campaign_id: str
) -> List[Dict[str, Any]]:
    """The image assets already linked to a campaign, as flat records."""
    rows = _read_back(
        customer_id,
        f"""
        SELECT campaign.id,
               campaign_asset.resource_name,
               campaign_asset.field_type, campaign_asset.status,
               asset.id, asset.name,
               asset.image_asset.file_size,
               asset.image_asset.mime_type,
               asset.image_asset.full_size.width_pixels,
               asset.image_asset.full_size.height_pixels
        FROM campaign_asset
        WHERE campaign.id = {campaign_id}
          AND campaign_asset.field_type IN
              ('MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE')
          AND campaign_asset.status != 'REMOVED'
        """,
    )
    out = []
    for row in rows:
        link = row.get("campaign_asset", {})
        asset = row.get("asset", {})
        size = (asset.get("image_asset") or {}).get("full_size") or {}
        out.append(
            {
                "type": link.get("field_type"),
                "status": link.get("status"),
                "asset_id": asset.get("id"),
                "asset_name": asset.get("name"),
                "width": size.get("width_pixels"),
                "height": size.get("height_pixels"),
                "campaign_asset_resource_name": link.get("resource_name"),
            }
        )
    return out


@extensions_mcp.tool
def add_image_assets(
    customer_id: str,
    campaign_id: str,
    images: Optional[List[Dict[str, Any]]] = None,
    asset_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Puts images on a Search campaign's ads, in the right slot for each.

    The SQUARE image is the one Search actually needs; the landscape is
    optional and shown where it fits. This tool does not ask which slot an
    image is for -- it reads the pixel dimensions and routes each one, and
    refuses anything whose shape is neither, which is how a 4:1 wordmark is
    caught before it becomes a policy violation rather than after.

    Eligibility is on the ACCOUNT, not the campaign, and Google enforces it
    at serving time: the account must be at least 60 days old, have spent on
    Search in the last 30 days, and have a clean policy history. A young
    account can hold these assets and still not show them.

    Assets live at account level and are reused by linking, so pass
    asset_ids for an image already uploaded rather than uploading it again.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The Search campaign to put them on.
        images: New images to upload and link, e.g.
            [{"name": "AVM sokak 1:1", "url": "https://..."}],
            [{"name": "...", "path": "/path/on/the/server/image.jpg"}] or
            [{"name": "...", "base64": "<PNG or JPEG bytes, base64>"}].
            'path' reads the file on the machine running this server, which
            is why it is refused when the server is serving HTTP. The name
            must be unique in the account. PNG or JPEG, under 5 MB.
        asset_ids: Ids of image assets already in the account, to link
            without re-uploading. Use discover_existing_assets to find them.

    Returns:
        What was linked, in which slot, and the campaign's images read back.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    images = list(images or [])
    asset_ids = [_clean_id(a) for a in (asset_ids or [])]
    if not images and not asset_ids:
        raise ValueError("Give at least one image or one asset_id.")

    existing = _campaign_images(customer_id, campaign_id)
    already = {str(row.get("asset_id")) for row in existing}
    repeats = [a for a in asset_ids if a in already]
    if repeats:
        raise ValueError(
            f"Already on this campaign: {repeats}. Linking an image twice "
            f"does not weight it more heavily, it just duplicates the row."
        )
    total = len(existing) + len(images) + len(asset_ids)
    if total > _MAX_CAMPAIGN_IMAGES:
        raise ValueError(
            f"A campaign holds at most {_MAX_CAMPAIGN_IMAGES} images; this "
            f"would make {total} ({len(existing)} already linked)."
        )

    client = utils.get_googleads_client()
    asset_service = utils.get_googleads_service("AssetService")

    # --- everything is checked before anything is written -----------------
    pending: List[Dict[str, Any]] = []
    for entry in images:
        name = str(entry.get("name") or "").strip()
        url = entry.get("url") or entry.get("image_url")
        encoded = entry.get("base64") or entry.get("image_base64")
        if not name:
            raise ValueError(f"Every image needs a 'name': {entry!r}")
        path = entry.get("path") or entry.get("file_path")
        given = [x for x in (url, encoded, path) if x]
        if len(given) != 1:
            raise ValueError(
                f"Give exactly one of 'url', 'path' or 'base64' for {name!r}."
            )
        if path:
            # The server process reads it, so a path only means anything when
            # the caller owns that process. Over HTTP it would be a file on
            # somebody else's machine, so it is refused there.
            from ads_mcp.server import chosen_transport

            if chosen_transport() == "http":
                raise ValueError(
                    "'path' reads a file on the SERVER's disk and this "
                    "server is running over HTTP, where that is somebody "
                    "else's machine. Send the bytes as 'base64' or host the "
                    "image and send a 'url'."
                )
            if not os.path.isfile(path):
                raise ValueError(f"No file at {path!r}.")
            with open(path, "rb") as handle:
                data = handle.read()
        elif url:
            if not str(url).startswith(("http://", "https://")):
                raise ValueError(
                    f"url must start with http:// or https://: {url}"
                )
            import requests

            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.content
        else:
            try:
                data = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError(
                    f"base64 for {name!r} is not valid base64: {exc}"
                ) from None
        if not data:
            raise ValueError(f"The image {name!r} is empty.")
        if len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(
                f"{name!r} is {len(data)} bytes; Google's limit is "
                f"{_MAX_IMAGE_BYTES}."
            )
        width, height = _image_size(data)
        pending.append(
            {
                "name": name,
                "data": data,
                "width": width,
                "height": height,
                "field_type": _image_field_type(width, height),
            }
        )

    linked_existing: List[Dict[str, Any]] = []
    for asset_id in asset_ids:
        rows = _read_back(
            customer_id,
            f"""
            SELECT asset.id, asset.name, asset.type,
                   asset.image_asset.full_size.width_pixels,
                   asset.image_asset.full_size.height_pixels
            FROM asset WHERE asset.id = {asset_id}
            """,
        )
        if not rows:
            raise ValueError(f"No asset {asset_id} in this account.")
        asset_row = rows[0]["asset"]
        if str(_enum(asset_row, "type") or "") != "IMAGE":
            raise ValueError(
                f"Asset {asset_id} is not an image "
                f"({_enum(asset_row, 'type')})."
            )
        size = (asset_row.get("image_asset") or {}).get("full_size") or {}
        width = int(size.get("width_pixels") or 0)
        height = int(size.get("height_pixels") or 0)
        linked_existing.append(
            {
                "asset_id": asset_id,
                "name": asset_row.get("name"),
                "width": width,
                "height": height,
                "field_type": _image_field_type(width, height),
                "resource_name": (
                    f"customers/{customer_id}/assets/{asset_id}"
                ),
            }
        )

    # --- writes -----------------------------------------------------------
    uploaded: List[Dict[str, Any]] = []
    if pending:
        operations = []
        for item in pending:
            operation = client.get_type("AssetOperation")
            asset = operation.create
            asset.name = item["name"]
            asset.type_ = client.enums.AssetTypeEnum.IMAGE
            asset.image_asset.data = item["data"]
            operations.append(operation)
        response = asset_service.mutate_assets(
            customer_id=customer_id, operations=operations
        )
        for item, result in zip(pending, response.results):
            uploaded.append(
                {
                    "asset_id": result.resource_name.rsplit("/", 1)[-1],
                    "name": item["name"],
                    "width": item["width"],
                    "height": item["height"],
                    "field_type": item["field_type"],
                    "resource_name": result.resource_name,
                    "bytes": len(item["data"]),
                }
            )

    for item in uploaded + linked_existing:
        _link_assets_to_campaign(
            customer_id,
            campaign_id,
            [item["resource_name"]],
            item["field_type"],
        )

    squares = [
        i
        for i in uploaded + linked_existing
        if i["field_type"] == "SQUARE_MARKETING_IMAGE"
    ]
    warnings = []
    if not squares and not any(
        row.get("type") == "SQUARE_MARKETING_IMAGE" for row in existing
    ):
        warnings.append(
            "This campaign has no SQUARE image. On Search the square is the "
            "required format and the landscape is optional, so with only "
            "landscapes linked nothing shows."
        )

    return {
        "campaign_id": campaign_id,
        "uploaded": uploaded,
        "linked_existing": linked_existing,
        "warnings": warnings,
        "note": (
            "Images go to Google review before they can show, and the "
            "account must be 60 days old with Search spend in the last 30 "
            "days for image assets to serve at all."
        ),
        "verified": _campaign_images(customer_id, campaign_id),
    }


# --------------------------------------------------------------------------
# Location extensions
#
# Location extensions do not work like the others. A sitelink is an asset you
# write; a location comes from the advertiser's Google Business Profile,
# which Google syncs into an ASSET SET. So there is nothing to create here --
# the work is finding the synced set and attaching it to the campaigns that
# should show an address.
# --------------------------------------------------------------------------

_LOCATION_SET_TYPES = {
    "LOCATION_SYNC",
    "BUSINESS_PROFILE_DYNAMIC_LOCATION_GROUP",
    "CHAIN_DYNAMIC_LOCATION_GROUP",
    "STATIC_LOCATION_GROUP",
}


def _campaign_asset_sets(customer_id: str, campaign_id: str) -> List[Dict]:
    rows = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, asset_set.id, asset_set.name, asset_set.type,
               campaign_asset_set.status
        FROM campaign_asset_set
        WHERE campaign.id = {campaign_id}
          AND campaign_asset_set.status != 'REMOVED'
        """,
    )
    return [
        {
            "asset_set_id": row["asset_set"]["id"],
            "name": row["asset_set"]["name"],
            "type": _enum(row["asset_set"], "type"),
            "status": row["campaign_asset_set"].get("status"),
        }
        for row in rows
    ]


@extensions_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_location_asset_sets(
    customer_id: str,
) -> Dict[str, Any]:
    """Lists the location asset sets Google synced from Business Profile.

    If this comes back empty, no Google Business Profile is linked to the
    account and no campaign can show an address. That link is made in the
    Google Ads interface, not through the API, and it is worth checking on
    every local-business account: an address extension is one of the few
    things that both raises click-through rate and pre-qualifies the click
    by showing how far away the shop is.

    Args:
        customer_id: The Google Ads account id.

    Returns:
        The location asset sets, with a note when none exist.
    """
    customer_id = _clean_id(customer_id)
    rows = _read_back(
        customer_id,
        """
        SELECT asset_set.id, asset_set.name, asset_set.type,
               asset_set.status
        FROM asset_set
        WHERE asset_set.status = 'ENABLED'
        """,
    )
    sets = [
        {
            "asset_set_id": row["asset_set"]["id"],
            "name": row["asset_set"]["name"],
            "type": _enum(row["asset_set"], "type"),
        }
        for row in rows
        if _enum(row["asset_set"], "type") in _LOCATION_SET_TYPES
    ]
    result: Dict[str, Any] = {"location_asset_sets": sets}
    if not sets:
        result["note"] = (
            "No location asset set exists, so no campaign in this account "
            "can show an address. Link the Google Business Profile in the "
            "Google Ads interface first; the API cannot make that link."
        )
    return result


@extensions_mcp.tool
def attach_location_asset_set(
    customer_id: str,
    campaign_id: str,
    asset_set_id: str,
) -> Dict[str, Any]:
    """Attaches a location asset set to a campaign, so its ads show an address.

    Adding an extension does not raise the bid or the budget; it makes the ad
    bigger and more specific, which usually raises click-through rate. On a
    local account it also filters: someone who sees the address and the
    distance and still clicks is closer to walking in.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign that should show the address.
        asset_set_id: From list_location_asset_sets.

    Returns:
        The campaign's asset sets afterwards.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)
    asset_set_id = _clean_id(asset_set_id)

    already = {
        str(entry["asset_set_id"])
        for entry in _campaign_asset_sets(customer_id, campaign_id)
    }
    if asset_set_id in already:
        return {
            "campaign_id": campaign_id,
            "asset_set_id": asset_set_id,
            "attached": False,
            "reason": "Already attached to this campaign.",
            "verified": _campaign_asset_sets(customer_id, campaign_id),
        }

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignAssetSetService")
    operation = client.get_type("CampaignAssetSetOperation")
    link = operation.create
    link.campaign = utils.get_googleads_service(
        "CampaignService"
    ).campaign_path(customer_id, campaign_id)
    link.asset_set = (
        f"customers/{customer_id}/assetSets/{asset_set_id}"
    )
    service.mutate_campaign_asset_sets(
        customer_id=customer_id, operations=[operation]
    )

    return {
        "campaign_id": campaign_id,
        "asset_set_id": asset_set_id,
        "attached": True,
        "verified": _campaign_asset_sets(customer_id, campaign_id),
    }
