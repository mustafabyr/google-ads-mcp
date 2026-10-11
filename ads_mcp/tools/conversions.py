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
from google.ads.googleads.errors import GoogleAdsException
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

# Goal rows the API exposes but will not let you address by resource name.
_UNADDRESSABLE_ORIGINS = {"UNKNOWN", "UNSPECIFIED"}

# Bidding strategies that spend against the "Conversions" column, so
# changing what feeds that column changes live spend.
_CONVERSION_BASED_STRATEGIES = (
    "MAXIMIZE_CONVERSIONS",
    "TARGET_CPA",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_ROAS",
)


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
    op_keys = []
    intended = {}
    skipped = []
    for row in goals:
        goal = row["campaign_conversion_goal"]
        category, origin = goal["category"], goal["origin"]
        should_bid = category in wanted
        if bool(goal.get("biddable")) == should_bid:
            continue

        # A goal row whose origin is UNKNOWN/UNSPECIFIED has no addressable
        # resource name: campaign_conversion_goal_path emits the literal
        # "UNKNOWN" and the API rejects the WHOLE batch with BAD_RESOURCE_ID.
        # Accounts carried over from older conversion setups routinely expose
        # these rows, so skip them and say so instead of failing everything.
        if origin in _UNADDRESSABLE_ORIGINS:
            skipped.append(
                f"{category}/{origin}: this goal has no addressable resource "
                f"name and cannot be changed through the API"
            )
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
        op_keys.append((category, origin))
        intended[(category, origin)] = should_bid

    # CampaignConversionGoalService takes no partial_failure flag, so a batch
    # is all or nothing: one goal the API refuses -- Google-hosted goals
    # frequently are -- would discard every goal it would have accepted. Send
    # them one at a time and keep going. What actually landed is decided by
    # the read-back below, never by the request (see CLAUDE.md).
    refused = {}
    for key, operation in zip(op_keys, operations):
        try:
            service.mutate_campaign_conversion_goals(
                customer_id=customer_id, operations=[operation]
            )
        except GoogleAdsException as exc:
            refused[key] = "; ".join(
                error.message for error in exc.failure.errors
            ) or "refused by the API"

    after = _read_back(
        customer_id,
        f"""
        SELECT campaign_conversion_goal.category,
               campaign_conversion_goal.origin,
               campaign_conversion_goal.biddable
        FROM campaign_conversion_goal
        WHERE campaign.id = {campaign_id}
        """,
    )
    state = {
        (
            r["campaign_conversion_goal"]["category"],
            r["campaign_conversion_goal"]["origin"],
        ): bool(r["campaign_conversion_goal"].get("biddable"))
        for r in after
    }

    applied, rejected = [], []
    for key, should_bid in intended.items():
        category, origin = key
        line = f"{category}/{origin} -> biddable={should_bid}"
        if state.get(key) == should_bid:
            applied.append(line)
        else:
            reason = refused.get(key, "the API accepted the write but the "
                                      "read-back still disagrees")
            rejected.append(f"{line}: {reason}")

    return {
        "campaign_id": campaign_id,
        "biddable_categories": sorted(wanted),
        "applied": applied,
        "rejected_by_api": rejected,
        "skipped": skipped,
        "biddable_now": sorted(
            f"{c}/{o}" for (c, o), biddable in state.items() if biddable
        ),
    }


@conversions_mcp.tool
def update_conversion_action(
    customer_id: str,
    conversion_action_id: str,
    include_in_conversions_metric: Optional[bool] = None,
    primary_for_goal: Optional[bool] = None,
    default_value: Optional[float] = None,
    always_use_default_value: Optional[bool] = None,
    click_lookback_days: Optional[int] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
    confirm_spend: bool = False,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Changes an existing conversion action, including whether it counts.

    This fixes the commonest broken account: an action that records perfectly
    but sits OUTSIDE the "Conversions" column, so every conversion-based
    bidding strategy in the account is optimizing toward nothing while the
    real signal accumulates unused. `include_in_conversions_metric` is that
    switch, and it cannot be set at creation time -- only here.

    Because that switch decides what smart bidding chases, changing it (or
    `primary_for_goal`, or the value) while a conversion-based strategy is
    serving requires confirm_spend=True. Renaming is always free.

    STATUS CANNOT BE CHANGED HERE. Google's enum has no PAUSED for a
    conversion action, and the API refuses HIDDEN and REMOVED on an update.
    A conversion action is retired only in the Google Ads interface
    (Goals > Conversions > the action > Remove). Passing `status` returns
    that explanation instead of an opaque API error.

    This does NOT work on GOOGLE_HOSTED actions -- the local actions that
    come from the Business Profile, such as "click to call" and "directions".
    Google owns those and refuses every mutate with MUTATE_NOT_ALLOWED and
    IMMUTABLE_FIELD. To make one of them bid-worthy, make its CATEGORY
    biddable on the campaign with set_campaign_conversion_goals instead.

    Args:
        customer_id: The Google Ads account id.
        conversion_action_id: The action to change, from
            list_conversion_actions.
        include_in_conversions_metric: True to feed the "Conversions" column
            that smart bidding optimizes; False to keep it for reporting only.
        primary_for_goal: True to make this action primary for its goal
            category, False for secondary.
        default_value: Value of one conversion, in the ACCOUNT'S OWN currency.
        always_use_default_value: True to ignore any value sent by the tag.
        click_lookback_days: How many days after a click a conversion still
            counts, 1-90.
        name: New name for the action.
        status: Not supported -- see above. Any value is refused with the
            reason; the parameter stays so older callers get a clear
            message rather than a schema error.
        confirm_spend: Required when a change would alter what a serving
            conversion-based campaign bids toward.
        confirm_remove: Kept for compatibility; no longer used.

    Returns:
        The action's real post-write state, read back from the API.
    """
    customer_id = _clean_id(customer_id)
    conversion_action_id = _clean_id(conversion_action_id)

    changes_bidding = any(
        value is not None
        for value in (
            include_in_conversions_metric,
            primary_for_goal,
            default_value,
            always_use_default_value,
        )
    )
    if (
        not changes_bidding
        and name is None
        and status is None
        and click_lookback_days is None
    ):
        raise ValueError(
            "Nothing to change: pass at least one field. (Status cannot "
            "be changed through the API; see the tool description.)"
        )

    if click_lookback_days is not None and not 1 <= click_lookback_days <= 90:
        raise ValueError("click_lookback_days must be between 1 and 90.")

    if status is not None:
        raise ValueError(
            f"A conversion action's status cannot be changed through the "
            f"API: Google's enum has no PAUSED, and HIDDEN and REMOVED are "
            f"refused on an update (requested: {str(status).upper()!r}). "
            f"Retire it in the Google Ads interface instead: Goals > "
            f"Conversions > the action > Remove. To stop it feeding bidding "
            f"without retiring it, set include_in_conversions_metric=False "
            f"or primary_for_goal=False here."
        )

    before = _read_back(
        customer_id,
        f"""
        SELECT conversion_action.id, conversion_action.name,
               conversion_action.status, conversion_action.type,
               conversion_action.category,
               conversion_action.include_in_conversions_metric,
               conversion_action.primary_for_goal,
               conversion_action.click_through_lookback_window_days,
               conversion_action.value_settings.default_value
        FROM conversion_action
        WHERE conversion_action.id = {conversion_action_id}
        """,
    )
    if not before:
        raise ValueError(
            f"No conversion action {conversion_action_id} in account "
            f"{customer_id}."
        )

    # Feeding, or starving, the "Conversions" column changes how a serving
    # smart-bidding campaign spends from the next auction onward.
    if changes_bidding and not confirm_spend:
        strategies = "', '".join(_CONVERSION_BASED_STRATEGIES)
        serving = _read_back(
            customer_id,
            f"""
            SELECT campaign.name, campaign.bidding_strategy_type,
                   campaign_budget.amount_micros
            FROM campaign
            WHERE campaign.status = 'ENABLED'
              AND campaign.bidding_strategy_type IN ('{strategies}')
            """,
        )
        if serving:
            daily = sum(
                _from_micros(int(row["campaign_budget"]["amount_micros"]))
                for row in serving
            )
            names = ", ".join(
                f"{row['campaign']['name']} "
                f"({row['campaign']['bidding_strategy_type']})"
                for row in serving
            )
            raise SpendConfirmationRequired(
                f"Changing what counts as a conversion changes what these "
                f"serving campaigns bid toward, from the next auction: "
                f"{names}. Together they spend up to {round(daily, 2)}/day, "
                f"about {round(daily * 30.4, 2)}/month in the account's "
                f"currency, and they re-enter the learning phase. Show this "
                f"to the user and call again with confirm_spend=True only "
                f"after they agree."
            )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("ConversionActionService")

    operation = client.get_type("ConversionActionOperation")
    action = operation.update
    action.resource_name = service.conversion_action_path(
        customer_id, conversion_action_id
    )

    paths = []
    if include_in_conversions_metric is not None:
        action.include_in_conversions_metric = include_in_conversions_metric
        paths.append("include_in_conversions_metric")
    if primary_for_goal is not None:
        action.primary_for_goal = primary_for_goal
        paths.append("primary_for_goal")
    if default_value is not None:
        action.value_settings.default_value = default_value
        paths.append("value_settings.default_value")
    if always_use_default_value is not None:
        action.value_settings.always_use_default_value = (
            always_use_default_value
        )
        paths.append("value_settings.always_use_default_value")
    if click_lookback_days is not None:
        action.click_through_lookback_window_days = click_lookback_days
        paths.append("click_through_lookback_window_days")
    if name is not None:
        action.name = name
        paths.append("name")

    client.copy_from(
        operation.update_mask, field_mask_pb2.FieldMask(paths=paths)
    )
    service.mutate_conversion_actions(
        customer_id=customer_id, operations=[operation]
    )

    after = _read_back(
        customer_id,
        f"""
        SELECT conversion_action.id, conversion_action.name,
               conversion_action.status, conversion_action.type,
               conversion_action.category,
               conversion_action.include_in_conversions_metric,
               conversion_action.primary_for_goal,
               conversion_action.click_through_lookback_window_days,
               conversion_action.value_settings.default_value
        FROM conversion_action
        WHERE conversion_action.id = {conversion_action_id}
        """,
    )

    return {
        "conversion_action_id": conversion_action_id,
        "requested_fields": paths,
        "before": before[0]["conversion_action"],
        "after": after[0]["conversion_action"] if after else None,
        "note": (
            "'after' is the API's own read-back, not the request. Compare it "
            "with 'before': a field the API silently refused shows up as "
            "unchanged here."
        ),
    }


# --------------------------------------------------------------------------
# Offline conversion import
#
# The point of this is closing the loop. A Search ad produces a click; the
# click produces a phone call or a form; days later that becomes a booking
# somebody actually paid for. Google only ever saw the click. Uploading the
# outcome back, keyed on the click id, is what lets bidding optimise towards
# revenue instead of towards form submissions.
# --------------------------------------------------------------------------


@conversions_mcp.tool
def upload_offline_conversions(
    customer_id: str,
    conversion_action_id: str,
    conversions: List[Dict[str, Any]],
    partial_failure: bool = True,
) -> Dict[str, Any]:
    """Uploads conversions that happened offline, keyed on the Google click id.

    CHECK THIS FIRST: Google has closed UploadClickConversions to new
    integrations. An account that was not already using it gets
    "Usage of ConversionUploadService.UploadClickConversions is limited to
    existing users" and nothing is recorded -- the call succeeds, the
    conversions do not arrive. New integrations have to go through the
    separate Data Manager API, which this server does not speak. The tool
    still works for accounts already grandfathered in, and surfaces that
    error verbatim in `api_errors` for the rest.

    Each entry needs the GCLID that Google put on the landing page URL, the
    time the conversion happened, and optionally what it was worth. The
    account must have auto-tagging ON or there is no GCLID to key on --
    get_account_settings reports that.

    Two rules Google enforces and this checks first, because the API's own
    error for them is opaque:
      * The conversion time must be AFTER the click. Uploading a booking
        dated before its own click is rejected.
      * conversion_date_time needs an explicit timezone offset, in the form
        "2026-09-01 14:30:00+03:00". A bare timestamp is rejected.

    Args:
        customer_id: The Google Ads account id.
        conversion_action_id: The conversion action to attribute these to. It
            must be of an upload type; create_conversion_action makes one.
        conversions: One dict per conversion, with keys:
            gclid (required, or gbraid / wbraid for app and iOS traffic),
            conversion_date_time (required, with offset),
            conversion_value (optional number),
            currency_code (optional, defaults to the account's),
            order_id (optional, dedupes re-uploads).
        partial_failure: True to accept the valid rows and report the bad
            ones, instead of rejecting the whole batch. Usually what you
            want for a spreadsheet export.

    Returns:
        How many were accepted, and the per-row errors for the rest.
    """
    customer_id = _clean_id(customer_id)
    conversion_action_id = _clean_id(conversion_action_id)
    if not conversions:
        raise ValueError("Give at least one conversion.")

    client = utils.get_googleads_client()
    action_path = utils.get_googleads_service(
        "ConversionActionService"
    ).conversion_action_path(customer_id, conversion_action_id)

    uploads = []
    rejected_locally = []
    for index, entry in enumerate(conversions):
        when = str(entry.get("conversion_date_time") or "").strip()
        click_ids = {
            key: entry.get(key)
            for key in ("gclid", "gbraid", "wbraid")
            if entry.get(key)
        }
        if not click_ids:
            rejected_locally.append(
                {"row": index, "reason": "no gclid, gbraid or wbraid"}
            )
            continue
        if not when:
            rejected_locally.append(
                {"row": index, "reason": "no conversion_date_time"}
            )
            continue
        if "+" not in when and "-" not in when[10:]:
            rejected_locally.append(
                {
                    "row": index,
                    "reason": (
                        "conversion_date_time has no timezone offset; use "
                        "'YYYY-MM-DD HH:MM:SS+03:00'"
                    ),
                }
            )
            continue

        conversion = client.get_type("ClickConversion")
        conversion.conversion_action = action_path
        conversion.conversion_date_time = when
        for key, value in click_ids.items():
            setattr(conversion, key, value)
        if entry.get("conversion_value") is not None:
            conversion.conversion_value = float(entry["conversion_value"])
            conversion.currency_code = entry.get("currency_code") or "TRY"
        if entry.get("order_id"):
            conversion.order_id = str(entry["order_id"])
        uploads.append(conversion)

    if not uploads:
        return {
            "uploaded": 0,
            "rejected_before_sending": rejected_locally,
            "note": "Nothing was sent: every row failed local validation.",
        }

    service = utils.get_googleads_service("ConversionUploadService")
    response = service.upload_click_conversions(
        customer_id=customer_id,
        conversions=uploads,
        partial_failure=partial_failure,
    )

    api_errors = []
    failure = getattr(response, "partial_failure_error", None)
    if failure is not None and getattr(failure, "message", ""):
        api_errors.append(failure.message)

    accepted = sum(
        1 for result in response.results if result.conversion_date_time
    )
    return {
        "sent": len(uploads),
        "accepted": accepted,
        "rejected_before_sending": rejected_locally,
        "api_errors": api_errors,
        "note": (
            "Uploaded conversions can take up to 3 hours to appear in "
            "reports, and only count if the click is inside the conversion "
            "action's lookback window."
        ),
    }


@conversions_mcp.tool
def upload_call_conversions(
    customer_id: str,
    conversion_action_id: str,
    calls: List[Dict[str, Any]],
    partial_failure: bool = True,
) -> Dict[str, Any]:
    """Uploads the outcome of phone calls that came from ads.

    The same restriction as upload_offline_conversions applies: Google
    limits ConversionUploadService to accounts already using it, and points
    new integrations at the Data Manager API. Check `api_errors` in the
    result before believing anything was recorded.

    This is how "the call turned into a booking" gets back to Google. It is
    keyed on the caller's number and the time the call STARTED, which is what
    a call-tracking export gives you, not on a click id.

    Args:
        customer_id: The Google Ads account id.
        conversion_action_id: An upload-type conversion action for calls.
        calls: One dict per call, with keys:
            caller_id (required, in E.164, e.g. "+905301234567"),
            call_start_date_time (required, with timezone offset),
            conversion_date_time (required, with offset),
            conversion_value (optional), currency_code (optional).
        partial_failure: True to accept the valid rows and report the rest.

    Returns:
        How many were accepted, and the per-row errors for the rest.
    """
    customer_id = _clean_id(customer_id)
    conversion_action_id = _clean_id(conversion_action_id)
    if not calls:
        raise ValueError("Give at least one call.")

    client = utils.get_googleads_client()
    action_path = utils.get_googleads_service(
        "ConversionActionService"
    ).conversion_action_path(customer_id, conversion_action_id)

    uploads = []
    rejected_locally = []
    for index, entry in enumerate(calls):
        caller = str(entry.get("caller_id") or "").strip()
        started = str(entry.get("call_start_date_time") or "").strip()
        when = str(entry.get("conversion_date_time") or "").strip()
        if not caller.startswith("+"):
            rejected_locally.append(
                {
                    "row": index,
                    "reason": "caller_id must be E.164, starting with '+'",
                }
            )
            continue
        if not started or not when:
            rejected_locally.append(
                {
                    "row": index,
                    "reason": (
                        "both call_start_date_time and conversion_date_time "
                        "are required, each with a timezone offset"
                    ),
                }
            )
            continue

        conversion = client.get_type("CallConversion")
        conversion.conversion_action = action_path
        conversion.caller_id = caller
        conversion.call_start_date_time = started
        conversion.conversion_date_time = when
        if entry.get("conversion_value") is not None:
            conversion.conversion_value = float(entry["conversion_value"])
            conversion.currency_code = entry.get("currency_code") or "TRY"
        uploads.append(conversion)

    if not uploads:
        return {
            "uploaded": 0,
            "rejected_before_sending": rejected_locally,
            "note": "Nothing was sent: every row failed local validation.",
        }

    service = utils.get_googleads_service("ConversionUploadService")
    response = service.upload_call_conversions(
        customer_id=customer_id,
        conversions=uploads,
        partial_failure=partial_failure,
    )

    api_errors = []
    failure = getattr(response, "partial_failure_error", None)
    if failure is not None and getattr(failure, "message", ""):
        api_errors.append(failure.message)

    accepted = sum(
        1 for result in response.results if result.conversion_date_time
    )
    return {
        "sent": len(uploads),
        "accepted": accepted,
        "rejected_before_sending": rejected_locally,
        "api_errors": api_errors,
    }
