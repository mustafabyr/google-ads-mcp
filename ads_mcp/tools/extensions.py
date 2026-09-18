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

"""Write tools for ad extensions (assets): sitelinks, callouts, snippets, call.

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


def _flatten_extension(row: Dict[str, Any]) -> Dict[str, Any]:
    """Turns one campaign_asset row into a flat, readable record."""
    link = row.get("campaign_asset", {})
    asset = row.get("asset", {})
    field_type = link.get("field_type")

    out: Dict[str, Any] = {
        "type": field_type,
        "status": link.get("status"),
        "asset_id": asset.get("id"),
        "asset_name": asset.get("name"),
        "campaign_asset_resource_name": link.get("resource_name"),
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
               asset.final_urls
        FROM campaign_asset
        WHERE campaign.id = {campaign_id}
          AND campaign_asset.field_type IN
              ('SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET', 'CALL',
               'PROMOTION')
          AND campaign_asset.status != 'REMOVED'
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
