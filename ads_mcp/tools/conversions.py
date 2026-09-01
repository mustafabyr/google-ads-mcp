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

"""Write tools for conversion tracking.

Conversion tracking is measurement, not spend: defining a conversion action
cannot cost anything, so nothing here needs confirm_spend. The exception is
changing which conversions a SERVING campaign bids toward -- that changes how
money is spent right now, and is gated the same way as changing the bidding
strategy.

Without at least one conversion action the API refuses every conversion-based
bidding strategy with CONVERSION_TRACKING_NOT_ENABLED, which is why these
tools come before any real use of MAXIMIZE_CONVERSIONS, TARGET_CPA or
TARGET_ROAS.
"""

from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from google.protobuf import field_mask_pb2
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.tools.campaigns import SpendConfirmationRequired

conversions_mcp = FastMCP("conversions")

_MICROS = 1_000_000

# The categories worth offering for a lead-generation or ecommerce account.
# The full enum has a long tail that only applies to app or store campaigns.
_CATEGORIES = {
    "DEFAULT", "PAGE_VIEW", "PURCHASE", "SIGNUP", "DOWNLOAD", "ADD_TO_CART",
    "BEGIN_CHECKOUT", "SUBSCRIBE_PAID", "PHONE_CALL_LEAD", "IMPORTED_LEAD",
    "SUBMIT_LEAD_FORM", "BOOK_APPOINTMENT", "REQUEST_QUOTE", "GET_DIRECTIONS",
    "OUTBOUND_CLICK", "CONTACT", "ENGAGEMENT", "QUALIFIED_LEAD",
    "CONVERTED_LEAD",
}

_COUNTING = {"ONE_PER_CLICK", "MANY_PER_CLICK"}


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _to_micros(amount: float) -> int:
    if amount < 0:
        raise ValueError("Amount cannot be negative.")
    return int(round(amount * _MICROS))


def _from_micros(micros: int) -> float:
    return round((micros or 0) / _MICROS, 2)


def _read_back(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    rows = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            rows.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return rows


@conversions_mcp.tool
def create_conversion_action(
    customer_id: str,
    name: str,
    category: str = "SUBMIT_LEAD_FORM",
    default_value: Optional[float] = None,
    always_use_default_value: bool = False,
    counting_type: str = "ONE_PER_CLICK",
    click_lookback_days: int = 30,
) -> Dict[str, Any]:
    """Creates a website conversion action and returns the tag to install.

    Creating this costs nothing and changes no bidding on its own. It does
    two things: it starts measurement, and it satisfies the precondition for
    conversion-based bidding, which the API otherwise refuses with
    CONVERSION_TRACKING_NOT_ENABLED.

    The returned `tag_snippets` are the actual gtag code. Nothing is measured
    until that code is on the site, so pass it to the user -- a conversion
    action with no tag installed will report zero conversions forever and
    quietly starve any smart bidding strategy pointed at it.

    Args:
        customer_id: The Google Ads account id.
        name: Conversion name, e.g. "Kayit Formu". Unique within the account.
        category: What the conversion means. Common ones: SUBMIT_LEAD_FORM,
            PURCHASE, SIGNUP, CONTACT, BOOK_APPOINTMENT, REQUEST_QUOTE,
            PHONE_CALL_LEAD, QUALIFIED_LEAD, PAGE_VIEW.
        default_value: Value of one conversion, in the ACCOUNT'S OWN currency.
            Omit for conversions with no monetary value. Required in practice
            for TARGET_ROAS, which has nothing to optimize without it.
        always_use_default_value: True to ignore any value sent by the tag and
            always use default_value.
        counting_type: ONE_PER_CLICK counts one conversion per ad click and is
            right for leads. MANY_PER_CLICK counts every one and is right for
            sales, where one visitor can legitimately buy twice.
        click_lookback_days: How many days after a click a conversion still
            counts, 1-90. Longer windows suit slow decisions.

    Google decides `include_in_conversions_metric` -- whether this action
    feeds the "Conversions" column that smart bidding optimizes -- from the
    category, and rejects any attempt to set it on create. The read-back
    reports what it chose.

    Returns:
        The action's id, its read-back state, and the tag snippets to install.
    """
    customer_id = _clean_id(customer_id)
    category = category.upper()
    if category not in _CATEGORIES:
        raise ValueError(
            f"category must be one of {sorted(_CATEGORIES)}, got {category!r}."
        )
    counting_type = counting_type.upper()
    if counting_type not in _COUNTING:
        raise ValueError(f"counting_type must be one of {sorted(_COUNTING)}.")
    if not 1 <= click_lookback_days <= 90:
        raise ValueError("click_lookback_days must be between 1 and 90.")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("ConversionActionService")

    operation = client.get_type("ConversionActionOperation")
    action = operation.create
    action.name = name
    action.type_ = client.enums.ConversionActionTypeEnum.WEBPAGE
    action.category = getattr(
        client.enums.ConversionActionCategoryEnum, category
    )
    action.status = client.enums.ConversionActionStatusEnum.ENABLED
    action.counting_type = getattr(
        client.enums.ConversionActionCountingTypeEnum, counting_type
    )
    action.click_through_lookback_window_days = click_lookback_days
    if default_value is not None:
        action.value_settings.default_value = default_value
        action.value_settings.always_use_default_value = (
            always_use_default_value
        )

    response = service.mutate_conversion_actions(
        customer_id=customer_id, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    action_id = resource_name.rsplit("/", 1)[-1]

    verified = _read_back(
        customer_id,
        f"""
        SELECT conversion_action.id, conversion_action.name,
               conversion_action.status, conversion_action.type,
               conversion_action.category, conversion_action.counting_type,
               conversion_action.click_through_lookback_window_days,
               conversion_action.include_in_conversions_metric,
               conversion_action.value_settings.default_value,
               conversion_action.tag_snippets
        FROM conversion_action
        WHERE conversion_action.id = {action_id}
        """,
    )

    # Google returns the tag in four variants (HTML and AMP, each in an
    # onclick and a page-load flavour). Return only the HTML ones, and strip
    # them out of the read-back so the same blob is not repeated: this whole
    # response lands in a model's context window.
    snippets = []
    if verified:
        action_row = verified[0]["conversion_action"]
        for snippet in action_row.pop("tag_snippets", []):
            if snippet.get("page_format") != "HTML":
                continue
            snippets.append(
                {
                    "type": snippet.get("type_"),
                    "global_site_tag": snippet.get("global_site_tag"),
                    "event_snippet": snippet.get("event_snippet"),
                }
            )

    return {
        "conversion_action_id": action_id,
        "resource_name": resource_name,
        "name": name,
        "category": category,
        "note": (
            "Nothing is measured until the tag below is installed on the "
            "site. Give it to whoever owns the website. Conversion-based "
            "bidding on an action with no tag will have nothing to optimize."
        ),
        "tag_snippets": snippets,
        "verified": verified,
    }


@conversions_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_conversion_actions(
    customer_id: str,
    include_removed: bool = False,
) -> List[Dict[str, Any]]:
    """Lists the account's conversion actions.

    Use this before pointing a campaign at conversion-based bidding: an
    account with no ENABLED action, or one whose actions are all excluded
    from the conversions metric, has nothing for smart bidding to optimize.

    Args:
        customer_id: The Google Ads account id.
        include_removed: True to include REMOVED and HIDDEN actions.

    Returns:
        One row per conversion action.
    """
    customer_id = _clean_id(customer_id)
    where = (
        "" if include_removed
        else "WHERE conversion_action.status = 'ENABLED'"
    )
    return _read_back(
        customer_id,
        f"""
        SELECT conversion_action.id, conversion_action.name,
               conversion_action.status, conversion_action.type,
               conversion_action.category, conversion_action.counting_type,
               conversion_action.include_in_conversions_metric,
               conversion_action.primary_for_goal,
               conversion_action.click_through_lookback_window_days,
               conversion_action.value_settings.default_value
        FROM conversion_action
        {where}
        """,
    )


@conversions_mcp.tool
def set_campaign_conversion_goals(
    customer_id: str,
    campaign_id: str,
    biddable_categories: List[str],
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Chooses which conversion categories a campaign bids toward.

    By default a campaign inherits the account's goals. Setting them here
    overrides that for this campaign: the categories you list become biddable
    and every other one stops being bid toward, so pass the complete list you
    want, not just additions.

    On a PAUSED campaign this is free. On an ENABLED campaign it changes what
    the bidding is chasing right now, so it requires confirm_spend=True.

    Args:
        customer_id: The Google Ads account id.
        campaign_id: The campaign to set goals for.
        biddable_categories: Categories to bid toward, e.g.
            ["SUBMIT_LEAD_FORM", "PHONE_CALL_LEAD"]. Every other category
            present on the campaign is set to non-biddable.
        confirm_spend: Required when the campaign is ENABLED.

    Returns:
        The campaign's conversion goals, read back after the change.
    """
    customer_id = _clean_id(customer_id)
    campaign_id = _clean_id(campaign_id)

    wanted = {c.upper() for c in biddable_categories}
    unknown = wanted - _CATEGORIES
    if unknown:
        raise ValueError(
            f"Unknown categories: {sorted(unknown)}. "
            f"Valid: {sorted(_CATEGORIES)}"
        )

    campaign = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign_budget.amount_micros
        FROM campaign WHERE campaign.id = {campaign_id}
        """,
    )
    if not campaign:
        raise ValueError(f"No campaign {campaign_id} in account {customer_id}.")

    if campaign[0]["campaign"]["status"] == "ENABLED" and not confirm_spend:
        daily = _from_micros(
            int(campaign[0]["campaign_budget"]["amount_micros"])
        )
        raise SpendConfirmationRequired(
            f"'{campaign[0]['campaign']['name']}' is ENABLED and spending "
            f"{daily}/day, about {round(daily * 30.4, 2)}/month in the "
            f"account's currency. Changing what it bids toward changes how "
            f"that money is spent immediately. Show this to the user and call "
            f"again with confirm_spend=True only after they agree."
        )

    goals = _read_back(
        customer_id,
        f"""
        SELECT campaign_conversion_goal.category,
               campaign_conversion_goal.origin,
               campaign_conversion_goal.biddable
        FROM campaign_conversion_goal
        WHERE campaign.id = {campaign_id}
        """,
    )
    if not goals:
        raise ValueError(
            f"Campaign {campaign_id} exposes no conversion goals. The account "
            f"needs at least one conversion action first -- create one with "
            f"create_conversion_action."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("CampaignConversionGoalService")
    campaign_service = utils.get_googleads_service("CampaignService")
    campaign_path = campaign_service.campaign_path(customer_id, campaign_id)

    operations = []
    changed = []
    for row in goals:
        goal = row["campaign_conversion_goal"]
        category, origin = goal["category"], goal["origin"]
        should_bid = category in wanted
        if bool(goal.get("biddable")) == should_bid:
            continue

        operation = client.get_type("CampaignConversionGoalOperation")
        operation.update.resource_name = (
            service.campaign_conversion_goal_path(
                customer_id, campaign_id, category, origin
            )
        )
        operation.update.campaign = campaign_path
        operation.update.biddable = should_bid
        client.copy_from(
            operation.update_mask,
            field_mask_pb2.FieldMask(paths=["biddable"]),
        )
        operations.append(operation)
        changed.append(f"{category}/{origin} -> biddable={should_bid}")

    if operations:
        service.mutate_campaign_conversion_goals(
            customer_id=customer_id, operations=operations
        )

    return {
        "campaign_id": campaign_id,
        "biddable_categories": sorted(wanted),
        "changed": changed,
        "verified": _read_back(
            customer_id,
            f"""
            SELECT campaign_conversion_goal.category,
                   campaign_conversion_goal.origin,
                   campaign_conversion_goal.biddable
            FROM campaign_conversion_goal
            WHERE campaign.id = {campaign_id}
              AND campaign_conversion_goal.biddable = true
            """,
        ),
    }
