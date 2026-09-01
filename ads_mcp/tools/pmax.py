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

"""Write tools for Performance Max campaigns and their asset groups.

Performance Max does not fit the campaign -> ad group -> ad shape the rest of
this server uses. A PMax campaign holds ASSET GROUPS; each asset group holds
loose assets (headlines, descriptions, images, a logo) that Google assembles
into ads itself, plus SIGNALS -- search themes and audiences -- that hint at
who to show them to. There are no keywords and no individual ads to pause.

The spend contract is unchanged where it matters: a PMax campaign is created
PAUSED and cannot serve. Signals and assets never raise the ceiling, so they
need no confirmation; removals do.

PMax needs conversion tracking. Its only bidding strategies are conversion
based, so an account with no conversion action is rejected outright -- create
one with create_conversion_action first.
"""

import base64
from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from google.protobuf import field_mask_pb2
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.tools.campaigns import SpendConfirmationRequired

pmax_mcp = FastMCP("pmax")

_MICROS = 1_000_000

# Asset text limits, per Google.
_MAX_HEADLINE = 30
_MAX_LONG_HEADLINE = 90
_MAX_DESCRIPTION = 90
_MAX_FIRST_DESCRIPTION = 60
_MAX_BUSINESS_NAME = 25

_MIN_HEADLINES, _MAX_HEADLINES = 3, 15
_MIN_LONG_HEADLINES, _MAX_LONG_HEADLINES = 1, 5
_MIN_DESCRIPTIONS, _MAX_DESCRIPTIONS = 2, 5

# Images that hang off the ASSET GROUP. The logo is not here: with Brand
# Guidelines enabled it belongs to the campaign, alongside the business name.
_GROUP_IMAGES = {
    "marketing_image_asset_ids": "MARKETING_IMAGE",
    "square_image_asset_ids": "SQUARE_MARKETING_IMAGE",
}

_MAX_IMAGE_BYTES = 5_000_000
_MAX_SEARCH_THEME = 80


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _to_micros(amount: float) -> int:
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return int(round(amount * _MICROS))


def _from_micros(micros: Any) -> float:
    return round(int(micros or 0) / _MICROS, 2)


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


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------


@pmax_mcp.tool
def upload_image_asset(
    customer_id: str,
    name: str,
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
) -> Dict[str, Any]:
    """Uploads an image so it can be used in a Performance Max asset group.

    Give a URL to fetch, or the bytes directly as base64. The image lives at
    account level and can be reused by any asset group, so upload once and
    pass the id around rather than uploading the same file per campaign.

    Google's minimums, which it enforces on use rather than on upload:
    landscape marketing images at least 600x314, square images and logos at
    least 128x128 (300x300 recommended), under 5 MB.

    Args:
        customer_id: The Google Ads account id.
        name: A name for the asset. Must be unique within the account.
        image_url: URL to fetch the image from. Give this or image_base64.
        image_base64: The image bytes, base64 encoded.

    Returns:
        The asset's id and its read-back size and dimensions.
    """
    customer_id = _clean_id(customer_id)
    if bool(image_url) == bool(image_base64):
        raise ValueError("Give exactly one of image_url or image_base64.")

    if image_url:
        if not str(image_url).startswith(("http://", "https://")):
            raise ValueError("image_url must start with http:// or https://")
        import requests

        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        data = response.content
    else:
        try:
            data = base64.b64decode(image_base64, validate=True)
        except Exception as exc:
            raise ValueError(f"image_base64 is not valid base64: {exc}") from None

    if not data:
        raise ValueError("The image is empty.")
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"The image is {len(data)} bytes; Google's limit is "
            f"{_MAX_IMAGE_BYTES}."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AssetService")

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = name
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = data

    response = service.mutate_assets(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    asset_id = resource_name.rsplit("/", 1)[-1]

    return {
        "asset_id": asset_id,
        "resource_name": resource_name,
        "name": name,
        "bytes": len(data),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT asset.id, asset.name, asset.type,
                   asset.image_asset.file_size,
                   asset.image_asset.mime_type,
                   asset.image_asset.full_size.width_pixels,
                   asset.image_asset.full_size.height_pixels
            FROM asset WHERE asset.id = {asset_id}
            """,
        ),
    }


@pmax_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def discover_existing_assets(
    customer_id: str,
    asset_type: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Lists assets already in the account, so they can be reused.

    Check this before uploading: assets are account-level and reusable, and
    uploading the same logo for every campaign clutters the account without
    making anything work better.

    Args:
        customer_id: The Google Ads account id.
        asset_type: Filter by type, e.g. IMAGE, TEXT, YOUTUBE_VIDEO. Omit for
            all types.
        limit: Maximum rows.

    Returns:
        One row per asset, with its id, name, type and image dimensions.
    """
    customer_id = _clean_id(customer_id)
    where = ""
    if asset_type:
        where = f"WHERE asset.type = '{str(asset_type).upper()}'"

    rows = _read_back(
        customer_id,
        f"""
        SELECT asset.id, asset.name, asset.type,
               asset.image_asset.file_size,
               asset.image_asset.full_size.width_pixels,
               asset.image_asset.full_size.height_pixels,
               asset.text_asset.text
        FROM asset
        {where}
        LIMIT {int(limit)}
        """,
    )
    out = []
    for row in rows:
        a = row["asset"]
        image = a.get("image_asset", {}) or {}
        size = image.get("full_size", {}) or {}
        out.append(
            {
                "asset_id": a["id"],
                "name": a.get("name"),
                "type": a.get("type_"),
                "text": (a.get("text_asset") or {}).get("text"),
                "width": size.get("width_pixels"),
                "height": size.get("height_pixels"),
                "file_size": image.get("file_size"),
            }
        )
    return out


# --------------------------------------------------------------------------
# Campaign and asset group
# --------------------------------------------------------------------------


@pmax_mcp.tool
def create_pmax_campaign(
    customer_id: str,
    name: str,
    budget_resource_name: str,
    final_url: str,
    business_name: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    marketing_image_asset_ids: List[str],
    square_image_asset_ids: List[str],
    logo_asset_ids: List[str],
    asset_group_name: Optional[str] = None,
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
) -> Dict[str, Any]:
    """Creates a Performance Max campaign. ALWAYS created PAUSED.

    Builds the campaign, one asset group, and the links between the asset
    group and the assets you pass -- upload images first with
    upload_image_asset, or find existing ones with discover_existing_assets.

    Performance Max only bids toward conversions, so the account needs a
    conversion action before this will work. Omit both targets for plain
    Maximize Conversions; give target_cpa or target_roas to add a target.

    Unlike a Search campaign there are no keywords here, and no individual ads
    to pause: Google assembles ads from these assets and decides where they
    run. What steers it is the asset set plus the signals you add afterwards
    with add_pmax_search_themes and add_pmax_audience_signal.

    Args:
        customer_id: The Google Ads account id.
        name: Campaign name. Must be unique within the account.
        budget_resource_name: From create_campaign_budget.
        final_url: Landing page, e.g. "https://mdkampus.com/yks".
        business_name: The advertiser's name, at most 25 characters.
        headlines: 3-15 headlines, each at most 30 characters.
        long_headlines: 1-5 long headlines, each at most 90 characters.
        descriptions: 2-5 descriptions, at most 90 characters -- the FIRST is
            limited to 60, because Google uses it where space is tightest.
        marketing_image_asset_ids: At least one landscape (1.91:1) image id.
        square_image_asset_ids: At least one square (1:1) image id.
        logo_asset_ids: At least one logo id, square.
        asset_group_name: Name for the asset group. Defaults to the campaign
            name.
        target_cpa: Optional target CPA, in the account's own currency.
        target_roas: Optional target ROAS as a ratio (4.0 means 400%).

    Returns:
        The campaign and asset group ids, the PAUSED status, and read-backs.
    """
    customer_id = _clean_id(customer_id)

    if target_cpa is not None and target_roas is not None:
        raise ValueError("Give target_cpa or target_roas, not both.")
    if not str(final_url).startswith(("http://", "https://")):
        raise ValueError("final_url must start with http:// or https://")

    _check_len("business_name", business_name, _MAX_BUSINESS_NAME)
    for label, items, lo, hi, limit in (
        ("headlines", headlines, _MIN_HEADLINES, _MAX_HEADLINES, _MAX_HEADLINE),
        ("long_headlines", long_headlines, _MIN_LONG_HEADLINES,
         _MAX_LONG_HEADLINES, _MAX_LONG_HEADLINE),
        ("descriptions", descriptions, _MIN_DESCRIPTIONS, _MAX_DESCRIPTIONS,
         _MAX_DESCRIPTION),
    ):
        if not lo <= len(items) <= hi:
            raise ValueError(f"Need {lo}-{hi} {label}, got {len(items)}.")
        for text in items:
            _check_len(label.rstrip("s").replace("_", " ").capitalize(),
                       text, limit)
    if descriptions and len(descriptions[0]) > _MAX_FIRST_DESCRIPTION:
        raise ValueError(
            f"The FIRST description is limited to {_MAX_FIRST_DESCRIPTION} "
            f"characters, not {_MAX_DESCRIPTION}; this one is "
            f"{len(descriptions[0])}: {descriptions[0]!r}"
        )

    images = {
        "marketing_image_asset_ids": marketing_image_asset_ids,
        "square_image_asset_ids": square_image_asset_ids,
        "logo_asset_ids": logo_asset_ids,
    }
    for arg_name, ids in images.items():
        if not ids:
            raise ValueError(
                f"Performance Max needs at least one {arg_name[:-10]}: Google "
                f"will not serve an asset group without a landscape image, a "
                f"square image and a logo. Upload one with upload_image_asset."
            )

    client = utils.get_googleads_client()
    ga_service = utils.get_googleads_service("GoogleAdsService")
    asset_service = utils.get_googleads_service("AssetService")

    # Text assets first, as real resources. The atomic request below then only
    # has to invent names for the campaign and the asset group.
    text_plan = (
        [("HEADLINE", t) for t in headlines]
        + [("LONG_HEADLINE", t) for t in long_headlines]
        + [("DESCRIPTION", t) for t in descriptions]
    )
    text_operations = []
    for _, text in text_plan + [("BUSINESS_NAME", business_name)]:
        text_operation = client.get_type("AssetOperation")
        text_operation.create.text_asset.text = text
        text_operations.append(text_operation)
    text_response = asset_service.mutate_assets(
        customer_id=customer_id, operations=text_operations
    )
    text_resources = [r.resource_name for r in text_response.results]
    business_name_resource = text_resources.pop()

    # Performance Max has to be built in ONE atomic request. With Brand
    # Guidelines on, Google validates at campaign-create time that a business
    # name and a logo are already linked as CampaignAssets -- which cannot
    # exist before the campaign does. Temporary negative resource names break
    # the cycle, and the whole thing succeeds or fails together, so a failure
    # cannot leave an orphan campaign behind.
    def temp(kind: str, index: int) -> str:
        return f"customers/{customer_id}/{kind}/-{index}"

    campaign_temp = temp("campaigns", 1)
    group_temp = temp("assetGroups", 2)

    operations = []

    def add(field: str):
        operation = client.get_type("MutateOperation")
        operations.append(operation)
        return getattr(operation, field)

    # --- campaign, PAUSED --------------------------------------------------
    campaign = add("campaign_operation").create
    campaign.resource_name = campaign_temp
    campaign.name = name
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    )
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_resource_name
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum
        .DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    # Brand Guidelines is Google's default for new Performance Max campaigns
    # and moves the business name and logo up to CAMPAIGN level.
    campaign.brand_guidelines_enabled = True
    if target_roas is not None:
        campaign.maximize_conversion_value.target_roas = target_roas
    else:
        client.copy_from(
            campaign.maximize_conversions,
            client.get_type("MaximizeConversions"),
        )
        if target_cpa is not None:
            campaign.maximize_conversions.target_cpa_micros = _to_micros(
                target_cpa
            )

    # --- campaign-level brand assets: business name and logo ---------------
    brand_link = add("campaign_asset_operation").create
    brand_link.campaign = campaign_temp
    brand_link.asset = business_name_resource
    brand_link.field_type = client.enums.AssetFieldTypeEnum.BUSINESS_NAME

    for asset_id in logo_asset_ids:
        logo_link = add("campaign_asset_operation").create
        logo_link.campaign = campaign_temp
        logo_link.asset = f"customers/{customer_id}/assets/{_clean_id(asset_id)}"
        logo_link.field_type = client.enums.AssetFieldTypeEnum.LOGO

    # --- asset group -------------------------------------------------------
    group = add("asset_group_operation").create
    group.resource_name = group_temp
    group.name = asset_group_name or name
    group.campaign = campaign_temp
    group.final_urls.append(final_url)
    group.status = client.enums.AssetGroupStatusEnum.ENABLED

    # --- asset group text assets ------------------------------------------
    # Already created above, with real resource names: Google does not resolve
    # a temporary asset name when validating the asset group that references
    # it, and reports the group as NOT_ENOUGH_HEADLINE_ASSET instead.
    for (field_type, _), resource_name in zip(text_plan, text_resources):
        link = add("asset_group_asset_operation").create
        link.asset_group = group_temp
        link.asset = resource_name
        link.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type)

    # --- asset group images ------------------------------------------------
    for arg_name, field_type in _GROUP_IMAGES.items():
        for asset_id in images[arg_name]:
            link = add("asset_group_asset_operation").create
            link.asset_group = group_temp
            link.asset = (
                f"customers/{customer_id}/assets/{_clean_id(asset_id)}"
            )
            link.field_type = getattr(
                client.enums.AssetFieldTypeEnum, field_type
            )

    response = ga_service.mutate(
        customer_id=customer_id, mutate_operations=operations
    )

    campaign_resource = response.mutate_operation_responses[
        0
    ].campaign_result.resource_name
    campaign_id = campaign_resource.rsplit("/", 1)[-1]
    asset_group_id = None
    for result in response.mutate_operation_responses:
        if result._pb.HasField("asset_group_result"):
            asset_group_id = (
                result.asset_group_result.resource_name.rsplit("/", 1)[-1]
            )
            break

    return {
        "campaign_id": campaign_id,
        "campaign_resource_name": campaign_resource,
        "asset_group_id": asset_group_id,
        "status": "PAUSED",
        "assets_linked": len(operations) - 2,
        "brand_assets": (
            "Brand Guidelines is on: the business name and logo are linked at "
            "CAMPAIGN level and apply to every asset group in this campaign."
        ),
        "note": (
            "Created PAUSED. It is not serving and is not spending. Add "
            "search themes and audience signals, check ad strength, then "
            "enable with set_campaign_status(confirm_spend=True). Performance "
            "Max has no keywords and no individual ads to pause -- the asset "
            "group is the unit of control."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   asset_group.id, asset_group.name, asset_group.status,
                   asset_group.ad_strength, asset_group.final_urls
            FROM asset_group WHERE campaign.id = {campaign_id}
            """,
        ),
    }


# --------------------------------------------------------------------------
# Signals: search themes and audiences
# --------------------------------------------------------------------------


@pmax_mcp.tool
def add_pmax_search_themes(
    customer_id: str,
    asset_group_id: str,
    themes: List[str],
) -> Dict[str, Any]:
    """Adds search themes to a Performance Max asset group.

    Search themes are hints, not keywords: Google treats them as a starting
    point for what people might search, and will still serve outside them.
    They cannot be negated and they do not cap anything, so they never raise
    spend and need no confirmation.

    Args:
        customer_id: The Google Ads account id.
        asset_group_id: The asset group to add them to.
        themes: Phrases, each at most 80 characters, e.g.
            ["yks hazirlik kursu", "istanbul ozel ders"].

    Returns:
        The asset group's search themes, read back.
    """
    customer_id = _clean_id(customer_id)
    asset_group_id = _clean_id(asset_group_id)
    if not themes:
        raise ValueError("Give at least one search theme.")
    for theme in themes:
        _check_len("Search theme", theme, _MAX_SEARCH_THEME)

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AssetGroupSignalService")
    group_service = utils.get_googleads_service("AssetGroupService")
    group_path = group_service.asset_group_path(customer_id, asset_group_id)

    operations = []
    for theme in themes:
        operation = client.get_type("AssetGroupSignalOperation")
        operation.create.asset_group = group_path
        operation.create.search_theme.text = theme
        operations.append(operation)

    service.mutate_asset_group_signals(
        customer_id=customer_id, operations=operations
    )

    return {
        "asset_group_id": asset_group_id,
        "added": len(operations),
        "note": (
            "Search themes are hints. Performance Max still serves outside "
            "them and they cannot be used as negatives."
        ),
        "verified": _pmax_signals(customer_id, asset_group_id),
    }


def _pmax_signals(customer_id: str, asset_group_id: str) -> List[Dict]:
    return _read_back(
        customer_id,
        f"""
        SELECT asset_group.id,
               asset_group_signal.search_theme.text,
               asset_group_signal.audience.audience,
               asset_group_signal.approval_status
        FROM asset_group_signal
        WHERE asset_group.id = {asset_group_id}
        """,
    )


@pmax_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_pmax_search_themes(
    customer_id: str,
    asset_group_id: str,
) -> List[Dict[str, Any]]:
    """Lists an asset group's search themes.

    Args:
        customer_id: The Google Ads account id.
        asset_group_id: The asset group to inspect.

    Returns:
        One row per search theme, with its approval status.
    """
    rows = _pmax_signals(_clean_id(customer_id), _clean_id(asset_group_id))
    out = []
    for row in rows:
        signal = row["asset_group_signal"]
        theme = (signal.get("search_theme") or {}).get("text")
        if theme:
            out.append(
                {
                    "search_theme": theme,
                    "approval_status": signal.get("approval_status"),
                    "criterion_id": row["asset_group_signal"].get(
                        "resource_name", ""
                    ).rsplit("~", 1)[-1],
                }
            )
    return out


@pmax_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_pmax_audience_signals(
    customer_id: str,
    asset_group_id: str,
) -> List[Dict[str, Any]]:
    """Lists an asset group's audience signals.

    Args:
        customer_id: The Google Ads account id.
        asset_group_id: The asset group to inspect.

    Returns:
        One row per audience signal.
    """
    rows = _pmax_signals(_clean_id(customer_id), _clean_id(asset_group_id))
    out = []
    for row in rows:
        signal = row["asset_group_signal"]
        audience = (signal.get("audience") or {}).get("audience")
        if audience:
            out.append(
                {
                    "audience_resource_name": audience,
                    "audience_id": audience.rsplit("/", 1)[-1],
                    "approval_status": signal.get("approval_status"),
                }
            )
    return out


@pmax_mcp.tool
def add_pmax_audience_signal(
    customer_id: str,
    asset_group_id: str,
    audience_id: str,
) -> Dict[str, Any]:
    """Adds an audience signal to a Performance Max asset group.

    An audience signal tells Google who to start with; like a search theme it
    is a hint, not a restriction, and Performance Max will reach beyond it.
    It cannot raise spend, so it needs no confirmation.

    The audience must already exist in the account. This server does not
    create audiences.

    Args:
        customer_id: The Google Ads account id.
        asset_group_id: The asset group to add the signal to.
        audience_id: An existing audience's id.

    Returns:
        The asset group's signals, read back.
    """
    customer_id = _clean_id(customer_id)
    asset_group_id = _clean_id(asset_group_id)

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AssetGroupSignalService")
    group_service = utils.get_googleads_service("AssetGroupService")
    ga_service = utils.get_googleads_service("GoogleAdsService")

    operation = client.get_type("AssetGroupSignalOperation")
    operation.create.asset_group = group_service.asset_group_path(
        customer_id, asset_group_id
    )
    operation.create.audience.audience = ga_service.audience_path(
        customer_id, _clean_id(audience_id)
    )

    service.mutate_asset_group_signals(
        customer_id=customer_id, operations=[operation]
    )

    return {
        "asset_group_id": asset_group_id,
        "audience_id": audience_id,
        "note": (
            "An audience signal is a hint. Performance Max still reaches "
            "beyond it."
        ),
        "verified": _pmax_signals(customer_id, asset_group_id),
    }


@pmax_mcp.tool
def remove_pmax_signal(
    customer_id: str,
    asset_group_id: str,
    criterion_id: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Removes a search theme or audience signal from an asset group.

    Covers both signal kinds: the resource is the same, only its payload
    differs. Removing a signal changes who Google starts with, so it takes
    confirmation even though nothing is destroyed permanently -- the signal
    can be added back.

    Args:
        customer_id: The Google Ads account id.
        asset_group_id: The asset group to remove the signal from.
        criterion_id: The signal's id, from get_pmax_search_themes or
            get_pmax_audience_signals.
        confirm_remove: Must be True. Name the signal going first.

    Returns:
        The asset group's remaining signals.
    """
    customer_id = _clean_id(customer_id)
    asset_group_id = _clean_id(asset_group_id)
    if not confirm_remove:
        raise ValueError(
            "Removing this signal changes who Performance Max starts with. "
            "It can be added back. Name the signal, then call again with "
            "confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AssetGroupSignalService")
    operation = client.get_type("AssetGroupSignalOperation")
    operation.remove = service.asset_group_signal_path(
        customer_id, asset_group_id, _clean_id(criterion_id)
    )
    service.mutate_asset_group_signals(
        customer_id=customer_id, operations=[operation]
    )

    return {
        "asset_group_id": asset_group_id,
        "removed_criterion_id": criterion_id,
        "verified": _pmax_signals(customer_id, asset_group_id),
    }
