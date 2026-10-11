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

"""Write tools for YouTube (video) and Demand Gen campaigns.

Both are created PAUSED, like every campaign this server makes.

They sit between the two shapes the rest of the server knows. A YouTube
campaign keeps the familiar campaign -> ad group -> ad hierarchy but its ad is
built from a YouTube video rather than text. Demand Gen uses asset groups, the
way Performance Max does, and inherits the same atomic-creation requirement.

Neither takes keywords. What steers them is the creative plus the audience,
which is why both need a video or image asset before they can exist at all.
"""

from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils

campaign_types_mcp = FastMCP("campaign_types")

_MICROS = 1_000_000

_MAX_VIDEO_HEADLINE = 30
_MAX_VIDEO_LONG_HEADLINE = 90
_MAX_VIDEO_DESCRIPTION = 90
_MAX_BUSINESS_NAME = 25

_CALL_TO_ACTIONS = {
    "LEARN_MORE", "SIGN_UP", "SHOP_NOW", "GET_QUOTE", "APPLY_NOW",
    "BOOK_NOW", "CONTACT_US", "SUBSCRIBE", "DOWNLOAD", "VISIT_SITE",
}


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
            rows.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return rows


def _check_len(label: str, text: str, limit: int) -> None:
    if len(text) > limit:
        raise ValueError(
            f"{label} is {len(text)} characters, limit is {limit}: {text!r}"
        )


def _no_political(client, campaign) -> None:
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum
        .DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )


@campaign_types_mcp.tool
def upload_youtube_video_asset(
    customer_id: str,
    youtube_video_id: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Registers an existing YouTube video as an asset.

    Nothing is uploaded to YouTube here -- the video must already be public or
    unlisted on a channel you control. This only tells Google Ads that the
    video exists so a campaign can point at it.

    Args:
        customer_id: The Google Ads account id.
        youtube_video_id: The id from the watch URL, e.g. "dQw4w9WgXcQ" from
            https://www.youtube.com/watch?v=dQw4w9WgXcQ -- the id only, not
            the whole URL.
        title: A name for the asset. Defaults to the video id.

    Returns:
        The asset's id and its read-back state.
    """
    customer_id = _clean_id(customer_id)
    video_id = str(youtube_video_id).strip()
    if not video_id or "/" in video_id or "?" in video_id:
        raise ValueError(
            f"Pass the video id alone, not a URL: {youtube_video_id!r}"
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("AssetService")

    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = title or f"YouTube {video_id}"
    asset.type_ = client.enums.AssetTypeEnum.YOUTUBE_VIDEO
    asset.youtube_video_asset.youtube_video_id = video_id
    if title:
        asset.youtube_video_asset.youtube_video_title = title

    response = service.mutate_assets(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    asset_id = resource_name.rsplit("/", 1)[-1]

    return {
        "asset_id": asset_id,
        "resource_name": resource_name,
        "youtube_video_id": video_id,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT asset.id, asset.name, asset.type,
                   asset.youtube_video_asset.youtube_video_id,
                   asset.youtube_video_asset.youtube_video_title
            FROM asset WHERE asset.id = {asset_id}
            """,
        ),
    }


@campaign_types_mcp.tool
def create_youtube_campaign(
    customer_id: str,
    name: str,
    budget_resource_name: str,
    final_url: str,
    business_name: str,
    video_asset_ids: List[str],
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    logo_asset_ids: List[str],
    call_to_action: str = "LEARN_MORE",
    target_cpa: Optional[float] = None,
) -> Dict[str, Any]:
    """Creates a YouTube video campaign. ALWAYS created PAUSED.

    Builds a VIDEO campaign with a responsive video ad. It keeps the
    campaign -> ad group -> ad shape, so set_ad_group_status and set_ad_status
    work on it as usual.

    No channel subtype is set: Google has folded Video Action into Demand Gen
    and now rejects the subtype on create.

    NOT VERIFIED END TO END. Google returns MUTATE_NOT_ALLOWED for VIDEO
    campaign creation on the test account this fork is developed against, and
    that could mean either "test accounts cannot do video" or "the API no
    longer creates video campaigns at all". The tool translates that error
    into an explanation rather than pretending to work. Everything up to the
    campaign create -- validation, the video asset, the ad structure -- is
    exercised; the create itself is not. Prefer create_demandgen_campaign for
    action-focused video until this is confirmed on a production account.

    Register the video first with upload_youtube_video_asset, and the logo
    with upload_image_asset.

    Args:
        customer_id: The Google Ads account id.
        name: Campaign name. Must be unique within the account.
        budget_resource_name: From create_campaign_budget.
        final_url: Landing page the ad clicks through to.
        business_name: At most 25 characters.
        video_asset_ids: One or more YouTube video asset ids.
        headlines: Short headlines, each at most 30 characters.
        long_headlines: Long headlines, each at most 90 characters.
        descriptions: Descriptions, each at most 90 characters.
        logo_asset_ids: One or more square logo image asset ids.
        call_to_action: Button text, one of LEARN_MORE, SIGN_UP, SHOP_NOW,
            GET_QUOTE, APPLY_NOW, BOOK_NOW, CONTACT_US, SUBSCRIBE, DOWNLOAD,
            VISIT_SITE.
        target_cpa: Optional target CPA in the account's own currency. Needs
            conversion tracking.

    Returns:
        The campaign, ad group and ad ids, the PAUSED status, and read-backs.
    """
    customer_id = _clean_id(customer_id)
    if not str(final_url).startswith(("http://", "https://")):
        raise ValueError("final_url must start with http:// or https://")
    if not video_asset_ids:
        raise ValueError(
            "A YouTube campaign needs at least one video. Register one with "
            "upload_youtube_video_asset."
        )
    if not logo_asset_ids:
        raise ValueError(
            "A responsive video ad needs at least one logo. Upload one with "
            "upload_image_asset."
        )
    call_to_action = call_to_action.upper()
    if call_to_action not in _CALL_TO_ACTIONS:
        raise ValueError(
            f"call_to_action must be one of {sorted(_CALL_TO_ACTIONS)}."
        )
    _check_len("business_name", business_name, _MAX_BUSINESS_NAME)
    for label, items, limit in (
        ("headlines", headlines, _MAX_VIDEO_HEADLINE),
        ("long_headlines", long_headlines, _MAX_VIDEO_LONG_HEADLINE),
        ("descriptions", descriptions, _MAX_VIDEO_DESCRIPTION),
    ):
        if not items:
            raise ValueError(f"Give at least one of {label}.")
        for text in items:
            _check_len(label.rstrip("s").replace("_", " ").capitalize(),
                       text, limit)

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.create
    campaign.name = name
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.VIDEO
    )
    # No advertising_channel_sub_type: Google has folded Video Action into
    # Demand Gen and refuses the subtype on create with MUTATE_NOT_ALLOWED.
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_resource_name
    _no_political(client, campaign)
    if target_cpa is not None:
        campaign.target_cpa.target_cpa_micros = _to_micros(target_cpa)
    else:
        client.copy_from(campaign.target_cpm, client.get_type("TargetCpm"))

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[operation]
        )
    except GoogleAdsException as exc:
        if any(
            error.error_code.mutate_error.name == "MUTATE_NOT_ALLOWED"
            for error in exc.failure.errors
        ):
            raise ValueError(
                "Google refused to create a VIDEO campaign on this account "
                "(MUTATE_NOT_ALLOWED). Two things cause this and they need "
                "different answers: video campaigns cannot serve on a TEST "
                "account at all, and separately Google has been closing API "
                "creation of video campaigns as Video Action moves into "
                "Demand Gen. If this is a test account, try a production one. "
                "If it is production, use create_demandgen_campaign instead, "
                "which is where Google now puts action-focused video."
            ) from None
        raise

    campaign_resource = response.results[0].resource_name
    campaign_id = campaign_resource.rsplit("/", 1)[-1]

    # --- ad group ----------------------------------------------------------
    group_service = utils.get_googleads_service("AdGroupService")
    group_operation = client.get_type("AdGroupOperation")
    group = group_operation.create
    group.name = f"{name} - Video"
    group.campaign = campaign_resource
    group.type_ = client.enums.AdGroupTypeEnum.VIDEO_RESPONSIVE
    group.status = client.enums.AdGroupStatusEnum.ENABLED

    group_response = group_service.mutate_ad_groups(
        customer_id=customer_id, operations=[group_operation]
    )
    ad_group_resource = group_response.results[0].resource_name
    ad_group_id = ad_group_resource.rsplit("/", 1)[-1]

    # --- responsive video ad ----------------------------------------------
    ad_service = utils.get_googleads_service("AdGroupAdService")
    asset_service = utils.get_googleads_service("AssetService")
    ad_operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = ad_operation.create
    ad_group_ad.ad_group = ad_group_resource
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(final_url)

    video_ad = ad_group_ad.ad.video_responsive_ad
    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        video_ad.headlines.append(asset)
    for text in long_headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        video_ad.long_headlines.append(asset)
    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        video_ad.descriptions.append(asset)

    cta = client.get_type("AdTextAsset")
    cta.text = call_to_action.replace("_", " ").title()
    video_ad.call_to_actions.append(cta)

    name_asset = client.get_type("AdTextAsset")
    name_asset.text = business_name
    video_ad.business_name.CopyFrom(name_asset._pb)

    for asset_id in video_asset_ids:
        video = client.get_type("AdVideoAsset")
        video.asset = asset_service.asset_path(customer_id, _clean_id(asset_id))
        video_ad.videos.append(video)
    for asset_id in logo_asset_ids:
        logo = client.get_type("AdImageAsset")
        logo.asset = asset_service.asset_path(customer_id, _clean_id(asset_id))
        video_ad.logo_images.append(logo)

    ad_response = ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[ad_operation]
    )
    ad_id = ad_response.results[0].resource_name.rsplit("~", 1)[-1]

    return {
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "ad_id": ad_id,
        "status": "PAUSED",
        "note": (
            "Created PAUSED. The ad goes to Google review before it can "
            "serve; the campaign is still the spend gate."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type,
                   campaign.advertising_channel_sub_type,
                   campaign.bidding_strategy_type
            FROM campaign WHERE campaign.id = {campaign_id}
            """,
        ),
    }


@campaign_types_mcp.tool
def create_demandgen_campaign(
    customer_id: str,
    name: str,
    budget_resource_name: str,
    final_url: str,
    business_name: str,
    headlines: List[str],
    descriptions: List[str],
    marketing_image_asset_ids: List[str],
    square_image_asset_ids: List[str],
    logo_asset_ids: List[str],
    asset_group_name: Optional[str] = None,
    target_cpa: Optional[float] = None,
) -> Dict[str, Any]:
    """Creates a Demand Gen campaign. ALWAYS created PAUSED.

    Demand Gen runs on YouTube, Discover and Gmail. It looks like Performance
    Max from the outside but is NOT built from asset groups -- it keeps ad
    groups, and all the creative goes into one multi-asset ad.

    Where Performance Max chases conversions across every Google surface,
    Demand Gen is the visual, discovery-led one: it wants strong images and
    is usually paired with an audience rather than intent signals.

    Args:
        customer_id: The Google Ads account id.
        name: Campaign name. Must be unique within the account.
        budget_resource_name: From create_campaign_budget.
        final_url: Landing page.
        business_name: At most 25 characters.
        headlines: 3-5 headlines, each at most 30 characters.
        descriptions: 2-5 descriptions, each at most 90 characters.
        marketing_image_asset_ids: At least one landscape (1.91:1) image id.
        square_image_asset_ids: At least one square (1:1) image id.
        logo_asset_ids: At least one square logo id.
        asset_group_name: Ad group name. Defaults to the campaign name.
        target_cpa: Optional target CPA in the account's own currency.

    Returns:
        The campaign, ad group and ad ids, the PAUSED status, and read-backs.
    """
    customer_id = _clean_id(customer_id)
    if not str(final_url).startswith(("http://", "https://")):
        raise ValueError("final_url must start with http:// or https://")
    _check_len("business_name", business_name, _MAX_BUSINESS_NAME)
    if not 3 <= len(headlines) <= 5:
        raise ValueError(f"Need 3-5 headlines, got {len(headlines)}.")
    if not 2 <= len(descriptions) <= 5:
        raise ValueError(f"Need 2-5 descriptions, got {len(descriptions)}.")
    for text in headlines:
        _check_len("Headline", text, _MAX_VIDEO_HEADLINE)
    for text in descriptions:
        _check_len("Description", text, _MAX_VIDEO_DESCRIPTION)
    images = {
        "marketing_image_asset_ids": (
            marketing_image_asset_ids, "MARKETING_IMAGE"),
        "square_image_asset_ids": (
            square_image_asset_ids, "SQUARE_MARKETING_IMAGE"),
        "logo_asset_ids": (logo_asset_ids, "LOGO"),
    }
    for arg_name, (ids, _) in images.items():
        if not ids:
            raise ValueError(
                f"Demand Gen needs at least one {arg_name[:-10]}. Upload one "
                f"with upload_image_asset."
            )

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")
    asset_service = utils.get_googleads_service("AssetService")

    # --- campaign, PAUSED --------------------------------------------------
    operation = client.get_type("CampaignOperation")
    campaign = operation.create
    campaign.name = name
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.DEMAND_GEN
    )
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_resource_name
    _no_political(client, campaign)
    if target_cpa is not None:
        campaign.maximize_conversions.target_cpa_micros = _to_micros(target_cpa)
    else:
        client.copy_from(
            campaign.maximize_conversions,
            client.get_type("MaximizeConversions"),
        )

    response = campaign_service.mutate_campaigns(
        customer_id=customer_id, operations=[operation]
    )
    campaign_resource = response.results[0].resource_name
    campaign_id = campaign_resource.rsplit("/", 1)[-1]

    # --- ad group ----------------------------------------------------------
    # Demand Gen does NOT use asset groups, despite looking like Performance
    # Max from the outside: adding one is rejected with
    # CANNOT_ADD_ASSET_GROUP_FOR_CAMPAIGN_TYPE. It keeps ad groups, and the
    # creative lives in a single multi-asset ad.
    group_service = utils.get_googleads_service("AdGroupService")
    group_operation = client.get_type("AdGroupOperation")
    group = group_operation.create
    group.name = asset_group_name or f"{name} - Ad Group"
    group.campaign = campaign_resource
    group.status = client.enums.AdGroupStatusEnum.ENABLED

    group_response = group_service.mutate_ad_groups(
        customer_id=customer_id, operations=[group_operation]
    )
    ad_group_resource = group_response.results[0].resource_name
    ad_group_id = ad_group_resource.rsplit("/", 1)[-1]

    # --- multi asset ad ----------------------------------------------------
    ad_service = utils.get_googleads_service("AdGroupAdService")
    ad_operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = ad_operation.create
    ad_group_ad.ad_group = ad_group_resource
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(final_url)

    ad = ad_group_ad.ad.demand_gen_multi_asset_ad
    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.headlines.append(asset)
    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.descriptions.append(asset)
    ad.business_name = business_name

    for arg_name, (ids, _) in images.items():
        target = {
            "marketing_image_asset_ids": ad.marketing_images,
            "square_image_asset_ids": ad.square_marketing_images,
            "logo_asset_ids": ad.logo_images,
        }[arg_name]
        for asset_id in ids:
            image = client.get_type("AdImageAsset")
            image.asset = asset_service.asset_path(
                customer_id, _clean_id(asset_id)
            )
            target.append(image)

    ad_response = ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[ad_operation]
    )
    ad_id = ad_response.results[0].resource_name.rsplit("~", 1)[-1]

    return {
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "ad_id": ad_id,
        "status": "PAUSED",
        "note": (
            "Created PAUSED. Demand Gen has no keywords; the creative is one "
            "multi-asset ad, and set_ad_status can pause it independently of "
            "the campaign."
        ),
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   ad_group.id, ad_group.name, ad_group.status
            FROM ad_group WHERE campaign.id = {campaign_id}
            """,
        ),
    }
