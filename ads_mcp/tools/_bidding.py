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

"""Bidding strategy shapes, in one place, with no FastMCP in sight.

Two tools build the same oneof from different sides:

  * ``campaigns.update_bid_strategy`` gives a campaign its OWN strategy;
  * ``bidding_strategies.detach_campaign_from_portfolio`` takes a campaign
    off a portfolio strategy and gives it its own in the same operation,
    because Google will not let it do one without the other.

Both have to pick the right leaf fields for the update mask, and a mask
that names a message instead of a leaf is the single most repeated mistake
in this codebase. So there is exactly one copy of that logic, here, and
both tools call it as a plain function -- never through the other tool's
FastMCP wrapper, whose shape is framework internals.

This module registers no tools and holds no FastMCP instance, so the
coordinator imports it and finds nothing to mount.
"""

from typing import Any, Dict, List, Optional

from google.protobuf import field_mask_pb2

_MICROS = 1_000_000

# Where on the page Target Impression Share aims to appear.
IMPRESSION_SHARE_LOCATIONS = {
    "ANYWHERE_ON_PAGE",
    "TOP_OF_PAGE",
    "ABSOLUTE_TOP_OF_PAGE",
}

# The strategies a CAMPAIGN may carry on itself, as the caller names them.
# MAXIMIZE_CLICKS is the interface's name for TARGET_SPEND; Target CPA and
# Target ROAS are no longer strategies of their own at campaign level (see
# apply_campaign_scheme).
CAMPAIGN_STRATEGIES = (
    "MANUAL_CPC",
    "MAXIMIZE_CLICKS",
    "MAXIMIZE_CONVERSIONS",
    "TARGET_CPA",
    "TARGET_ROAS",
    "TARGET_IMPRESSION_SHARE",
)

# Strategies that take no cpc_bid_ceiling at CAMPAIGN level. The field
# exists on the message for several of them, but Google refuses it on a
# campaign; on a PORTFOLIO strategy of the same type it is accepted, which
# is why PORTFOLIO_CEILING_PATHS below is a longer list than this one.
CAMPAIGN_STRATEGIES_WITHOUT_CEILING = (
    "MANUAL_CPC",
    "MAXIMIZE_CONVERSIONS",
    "TARGET_CPA",
    "TARGET_ROAS",
)

# A portfolio (shared) BiddingStrategy is a resource of its own, and its
# scheme is a oneof: exactly one of these blocks is set. This maps the
# strategy's own type, as bidding_strategy.type reports it, to the dotted
# path of the CPC ceiling inside its block.
#
# Every path here must also appear in the limits layer's _CPC_FIELDS, or a
# ceiling could be raised past the account's max_cpc_ceiling_try without
# anything refusing it.
PORTFOLIO_CEILING_PATHS: Dict[str, str] = {
    "TARGET_SPEND": "target_spend.cpc_bid_ceiling_micros",
    "TARGET_IMPRESSION_SHARE": (
        "target_impression_share.cpc_bid_ceiling_micros"
    ),
    "MAXIMIZE_CONVERSIONS": "maximize_conversions.cpc_bid_ceiling_micros",
    "MAXIMIZE_CONVERSION_VALUE": (
        "maximize_conversion_value.cpc_bid_ceiling_micros"
    ),
    "TARGET_CPA": "target_cpa.cpc_bid_ceiling_micros",
    "TARGET_ROAS": "target_roas.cpc_bid_ceiling_micros",
}

# The settings worth reading back per portfolio strategy type, so a caller
# sees the target as well as the ceiling.
PORTFOLIO_SETTING_PATHS: Dict[str, List[str]] = {
    "TARGET_SPEND": [
        "target_spend.target_spend_micros",
        "target_spend.cpc_bid_ceiling_micros",
    ],
    "TARGET_IMPRESSION_SHARE": [
        "target_impression_share.location",
        "target_impression_share.location_fraction_micros",
        "target_impression_share.cpc_bid_ceiling_micros",
    ],
    "MAXIMIZE_CONVERSIONS": [
        "maximize_conversions.target_cpa_micros",
        "maximize_conversions.cpc_bid_ceiling_micros",
        "maximize_conversions.cpc_bid_floor_micros",
    ],
    "MAXIMIZE_CONVERSION_VALUE": [
        "maximize_conversion_value.target_roas",
        "maximize_conversion_value.cpc_bid_ceiling_micros",
        "maximize_conversion_value.cpc_bid_floor_micros",
    ],
    "TARGET_CPA": [
        "target_cpa.target_cpa_micros",
        "target_cpa.cpc_bid_ceiling_micros",
        "target_cpa.cpc_bid_floor_micros",
    ],
    "TARGET_ROAS": [
        "target_roas.target_roas",
        "target_roas.cpc_bid_ceiling_micros",
        "target_roas.cpc_bid_floor_micros",
    ],
}

# bidding_strategy_system_status values that mean the strategy is working,
# or is simply not being asked to work. Everything else is a problem
# Google is already reporting and nobody is reading.
SYSTEM_STATUS_OK = {"ENABLED", "PAUSED", "UNSPECIFIED", "UNKNOWN", ""}

# What the bad ones mean, in the words the account owner needs.
SYSTEM_STATUS_MEANING: Dict[str, str] = {
    "MISCONFIGURED_CONVERSION_TYPES": (
        "the strategy bids for conversions, but the account's conversion "
        "goals do not give it a usable conversion type -- it is "
        "optimising towards nothing"
    ),
    "MISCONFIGURED_CONVERSION_SETTINGS": (
        "the conversion settings behind the strategy are not usable "
        "(window, counting or attribution)"
    ),
    "MISCONFIGURED_SHARED_BUDGET": (
        "the shared budget this strategy is aligned to does not match the "
        "campaigns on it"
    ),
    "MISCONFIGURED_STRATEGY_TYPE": (
        "the strategy type is not allowed for this campaign type"
    ),
    "MISCONFIGURED_ZERO_ELIGIBILITY": (
        "nothing under this campaign is eligible to serve"
    ),
    "LIMITED_BY_CPC_BID_CEILING": (
        "the CPC ceiling is holding the strategy back: it would bid more "
        "to win the auctions it is losing"
    ),
    "LIMITED_BY_CPC_BID_FLOOR": "the CPC floor is forcing bids up",
    "LIMITED_BY_BUDGET": "the budget, not the strategy, is the limit",
    "LIMITED_BY_DATA": "too little conversion data to bid on",
    "LIMITED_BY_INVENTORY": "not enough inventory to spend on",
    "LIMITED_BY_LOW_QUALITY": "quality is holding eligibility back",
    "LIMITED_BY_LOW_PRIORITY_SPEND": "another campaign is taking the spend",
    "LEARNING_NEW": "learning: new strategy",
    "LEARNING_SETTING_CHANGE": "learning after a setting change",
    "LEARNING_BUDGET_CHANGE": "learning after a budget change",
    "LEARNING_COMPOSITION_CHANGE": "learning after a composition change",
    "LEARNING_CONVERSION_TYPE_CHANGE": (
        "learning after a conversion type change"
    ),
    "LEARNING_CONVERSION_SETTING_CHANGE": (
        "learning after a conversion setting change"
    ),
    "MULTIPLE_MISCONFIGURED": "several misconfigurations at once",
    "MULTIPLE_LIMITED": "several limits at once",
    "MULTIPLE_LEARNING": "learning for several reasons at once",
    "MULTIPLE": "several statuses at once",
    "UNAVAILABLE": "Google does not report a status for this campaign",
}


def clean_id(value: Any) -> str:
    """Normalizes '123-456-7890' or 123456789 to a bare digit string."""
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def to_micros(amount: float) -> int:
    """Converts an amount in the account's own currency to micros."""
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return int(round(amount * _MICROS))


def from_micros(micros: Any) -> float:
    try:
        return round(int(micros or 0) / _MICROS, 2)
    except (TypeError, ValueError):
        return 0.0


def enum_name(value: Any) -> str:
    """proto-plus hands enums back as objects on some paths, strings on others."""
    if value is None:
        return ""
    return str(getattr(value, "name", None) or value)


def read_type(section: Dict[str, Any]) -> str:
    """The ``type`` of a message, surviving proto-plus's builtin rename.

    A message field called ``type`` arrives in the dict as ``type_``.
    Reading ``"type"`` returns None silently -- that is how a filter once
    matched nothing on an account that had the thing it was looking for.
    """
    return enum_name(section.get("type_", section.get("type")))


def set_mask(client: Any, operation: Any, paths: List[str]) -> None:
    """Puts a FieldMask on an operation.

    ``FieldMask`` is a protobuf well-known type, not a Google Ads one:
    ``client.get_type("FieldMask")`` fails.
    """
    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )


def apply_campaign_scheme(
    client: Any,
    campaign: Any,
    strategy: str,
    *,
    target_cpa: Optional[float] = None,
    target_roas: Optional[float] = None,
    impression_share_location: Optional[str] = None,
    impression_share_percent: Optional[float] = None,
    cpc_bid_ceiling: Optional[float] = None,
) -> List[str]:
    """Sets a campaign-level bidding scheme and returns its mask paths.

    ``campaign`` is a Campaign message being built for an update (or a
    create). The returned paths are the LEAF fields an update mask has to
    name: masking the strategy message itself is rejected with
    FIELD_HAS_SUBFIELDS. For the strategies that carry no settings we mask
    a leaf deliberately left unset, which both selects the strategy and
    clears any stale value on it.

    Raises ValueError on an unknown strategy, a missing required setting,
    or a ceiling on a strategy that does not take one.
    """
    strategy = str(strategy).upper()

    if strategy == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False
        paths = ["manual_cpc.enhanced_cpc_enabled"]
    elif strategy == "MAXIMIZE_CLICKS":
        client.copy_from(
            campaign.target_spend, client.get_type("TargetSpend")
        )
        if cpc_bid_ceiling is not None:
            campaign.target_spend.cpc_bid_ceiling_micros = to_micros(
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
        # Conversions with a target. The standalone TargetCpa is rejected
        # on Search campaigns with OPERATION_NOT_PERMITTED_FOR_CONTEXT.
        campaign.maximize_conversions.target_cpa_micros = to_micros(
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
        if location not in IMPRESSION_SHARE_LOCATIONS:
            raise ValueError(
                f"impression_share_location must be one of "
                f"{sorted(IMPRESSION_SHARE_LOCATIONS)}, not "
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
        campaign.target_impression_share.cpc_bid_ceiling_micros = to_micros(
            cpc_bid_ceiling
        )
        paths = [
            "target_impression_share.location",
            "target_impression_share.location_fraction_micros",
            "target_impression_share.cpc_bid_ceiling_micros",
        ]
    else:
        raise ValueError(f"Unknown bidding strategy: {strategy}")

    if (
        cpc_bid_ceiling is not None
        and strategy in CAMPAIGN_STRATEGIES_WITHOUT_CEILING
    ):
        raise ValueError(
            f"{strategy} has no cpc_bid_ceiling. Only MAXIMIZE_CLICKS and "
            f"TARGET_IMPRESSION_SHARE accept one."
        )

    return paths


def campaign_scheme_readback_fields() -> List[str]:
    """The campaign-level bidding fields worth selecting after a write."""
    return [
        "campaign.bidding_strategy",
        "campaign.bidding_strategy_type",
        "campaign.bidding_strategy_system_status",
        "campaign.target_spend.cpc_bid_ceiling_micros",
        "campaign.target_impression_share.location",
        "campaign.target_impression_share.location_fraction_micros",
        "campaign.target_impression_share.cpc_bid_ceiling_micros",
        "campaign.maximize_conversions.target_cpa_micros",
        "campaign.maximize_conversion_value.target_roas",
    ]


def describe_system_status(status: str) -> Optional[str]:
    """A sentence for a bidding_strategy_system_status worth reporting."""
    status = enum_name(status)
    if status in SYSTEM_STATUS_OK:
        return None
    return SYSTEM_STATUS_MEANING.get(
        status, "Google reports this status and this server has no note for it"
    )
