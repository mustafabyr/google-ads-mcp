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

"""The one place a tool's class is declared.

Every tool this server mounts has exactly one class, and the class decides
everything downstream: the MCP annotations a client sees, whether the
account needs a spending ceiling on file, whether the call writes directly
or produces a plan, and what the audit log records.

  READ         changes nothing in the account.
  PROTECTIVE   can only reduce or protect spend: adding a negative, pausing,
               lowering a budget, a bid or a ceiling.
  SPEND        can raise spend or changes structure: creating, enabling,
               raising a budget or a bid, bidding strategy, targeting,
               conversion goals, ad text.
  DESTRUCTIVE  removes, deletes or detaches something.

A tool that is missing from ``TOOL_CLASSES`` does not mount: the coordinator
raises ``UnclassifiedTool`` at start-up and the test suite fails, so a new
tool cannot be added without deciding what it is allowed to do.

Some SPEND tools are DIRECTIONAL: the same tool lowers a budget one day and
raises it the next. Their static class stays SPEND (that is what the client
sees), but at call time the guard reads the current value and routes a
decrease down the PROTECTIVE path -- no ceiling needed, no plan -- and an
increase down the SPEND path.
"""

from typing import Dict, FrozenSet

from mcp.types import ToolAnnotations

READ = "READ"
PROTECTIVE = "PROTECTIVE"
SPEND = "SPEND"
DESTRUCTIVE = "DESTRUCTIVE"

ALL_CLASSES: FrozenSet[str] = frozenset({READ, PROTECTIVE, SPEND, DESTRUCTIVE})

# Classes whose tools write nothing without a plan and an explicit apply.
PLANNED_CLASSES: FrozenSet[str] = frozenset({SPEND, DESTRUCTIVE})


class UnclassifiedTool(LookupError):
    """A tool was mounted without a row in TOOL_CLASSES."""


class UnknownTool(LookupError):
    """TOOL_CLASSES names a tool that no module defines."""


def qualified_name(namespace: str, tool_name: str) -> str:
    """The registry key: the sub-server's name plus the bare tool name."""
    return f"{namespace}.{tool_name}"


# Keyed by "<namespace>.<tool>", where the namespace is the FastMCP
# sub-server's own name (the same string tools_config.yaml uses).
TOOL_CLASSES: Dict[str, str] = {
    # --- Google's originals -------------------------------------------------
    "customers.list_accessible_customers": READ,
    "search.search": READ,
    "metadata.get_resource_metadata": READ,
    # --- campaigns ----------------------------------------------------------
    "campaigns.create_campaign_budget": SPEND,
    "campaigns.update_campaign_budget": SPEND,
    "campaigns.create_search_campaign": SPEND,
    "campaigns.set_campaign_status": SPEND,
    "campaigns.pause_campaign": PROTECTIVE,
    "campaigns.enable_campaign": SPEND,
    "campaigns.find_location_ids": READ,
    "campaigns.set_campaign_geo_targets": SPEND,
    "campaigns.set_campaign_geo_target_type": SPEND,
    "campaigns.find_language_ids": READ,
    "campaigns.set_campaign_languages": SPEND,
    "campaigns.set_campaign_url_settings": SPEND,
    "campaigns.set_campaign_ad_schedule": SPEND,
    "campaigns.update_campaign": SPEND,
    "campaigns.update_bid_strategy": SPEND,
    "campaigns.set_device_bid_modifiers": SPEND,
    "campaigns.set_campaign_proximity": SPEND,
    "campaigns.set_demographic_bid_modifiers": SPEND,
    "campaigns.set_location_bid_modifiers": SPEND,
    # --- adgroups -----------------------------------------------------------
    "adgroups.create_ad_group": SPEND,
    "adgroups.set_ad_group_status": SPEND,
    "adgroups.pause_ad_group": PROTECTIVE,
    "adgroups.enable_ad_group": SPEND,
    "adgroups.create_responsive_search_ad": SPEND,
    "adgroups.add_keywords": SPEND,
    "adgroups.add_negative_keywords": PROTECTIVE,
    "adgroups.remove_keywords": DESTRUCTIVE,
    "adgroups.update_ad_group_bid": SPEND,
    "adgroups.set_keyword_status": SPEND,
    "adgroups.pause_keywords": PROTECTIVE,
    "adgroups.enable_keywords": SPEND,
    "adgroups.update_keyword": SPEND,
    "adgroups.remove_negative_keywords": DESTRUCTIVE,
    "adgroups.set_ad_status": SPEND,
    "adgroups.pause_ad": PROTECTIVE,
    "adgroups.enable_ad": SPEND,
    "adgroups.update_ad_text": SPEND,
    # --- conversions --------------------------------------------------------
    "conversions.create_conversion_action": SPEND,
    "conversions.list_conversion_actions": READ,
    "conversions.set_campaign_conversion_goals": SPEND,
    "conversions.update_conversion_action": SPEND,
    "conversions.upload_offline_conversions": SPEND,
    "conversions.upload_call_conversions": SPEND,
    # --- extensions ---------------------------------------------------------
    "extensions.add_sitelinks": SPEND,
    "extensions.add_callout_extensions": SPEND,
    "extensions.add_structured_snippets": SPEND,
    "extensions.list_campaign_extensions": READ,
    "extensions.remove_campaign_extension": DESTRUCTIVE,
    "extensions.add_call_extension": SPEND,
    "extensions.update_call_extension": SPEND,
    "extensions.list_location_asset_sets": READ,
    "extensions.attach_location_asset_set": SPEND,
    # --- reports ------------------------------------------------------------
    "reports.list_campaigns": READ,
    "reports.get_campaign_performance": READ,
    "reports.get_campaign_structure": READ,
    "reports.get_search_terms": READ,
    "reports.get_wasted_spend": READ,
    "reports.get_conversion_performance": READ,
    "reports.get_impression_share": READ,
    "reports.get_keyword_performance": READ,
    "reports.get_ad_performance": READ,
    "reports.get_performance_by_segment": READ,
    "reports.get_change_history": READ,
    "reports.list_recommendations": READ,
    # --- keyword research ---------------------------------------------------
    "keyword_research.research_keywords": READ,
    "keyword_research.get_keyword_historical_metrics": READ,
    # --- pmax ---------------------------------------------------------------
    "pmax.upload_image_asset": SPEND,
    "pmax.discover_existing_assets": READ,
    "pmax.create_pmax_campaign": SPEND,
    "pmax.add_pmax_search_themes": SPEND,
    "pmax.get_pmax_search_themes": READ,
    "pmax.get_pmax_audience_signals": READ,
    "pmax.add_pmax_audience_signal": SPEND,
    "pmax.remove_pmax_signal": DESTRUCTIVE,
    # --- campaign types -----------------------------------------------------
    "campaign_types.upload_youtube_video_asset": SPEND,
    "campaign_types.create_youtube_campaign": SPEND,
    "campaign_types.create_demandgen_campaign": SPEND,
    # --- shared sets --------------------------------------------------------
    "shared_sets.list_negative_keyword_lists": READ,
    "shared_sets.get_negative_keyword_list_terms": READ,
    "shared_sets.create_negative_keyword_list": PROTECTIVE,
    "shared_sets.add_to_negative_keyword_list": PROTECTIVE,
    "shared_sets.attach_negative_keyword_list": PROTECTIVE,
    "shared_sets.detach_negative_keyword_list": DESTRUCTIVE,
    "shared_sets.remove_negative_keyword_list": DESTRUCTIVE,
    # --- targeting ----------------------------------------------------------
    "targeting.list_campaign_targeting": READ,
    "targeting.list_audiences": READ,
    "targeting.add_audience_to_campaign": SPEND,
    "targeting.exclude_demographics": PROTECTIVE,
    "targeting.remove_campaign_criteria": DESTRUCTIVE,
    # --- agency -------------------------------------------------------------
    "agency.account_portfolio_overview": READ,
    "agency.get_budget_pacing": READ,
    "agency.get_account_settings": READ,
    "agency.list_labels": READ,
    # Labels change nothing about serving or spend; they cannot raise it.
    "agency.create_label": PROTECTIVE,
    "agency.apply_label_to_campaigns": PROTECTIVE,
    # --- experiments --------------------------------------------------------
    "experiments.list_experiments": READ,
    "experiments.create_experiment": SPEND,
    "experiments.schedule_experiment": SPEND,
    "experiments.end_experiment": DESTRUCTIVE,
    "experiments.graduate_experiment": SPEND,
    "experiments.remove_experiment": DESTRUCTIVE,
    # --- audit --------------------------------------------------------------
    "audit.run_account_audit": READ,
    "audit.run_portfolio_audit": READ,
    "audit.get_change_log": READ,
    # Writes a line to the local decision log, nothing to the account.
    "audit.log_manual_change": PROTECTIVE,
    # --- plans --------------------------------------------------------------
    # apply_plan executes whatever the plan holds, so the client sees the
    # worst case. It gates itself (see EXECUTORS).
    "plans.apply_plan": DESTRUCTIVE,
    "plans.get_plan": READ,
    "plans.list_plans": READ,
    "plans.discard_plan": PROTECTIVE,
}

# Tools that execute plans rather than produce them. They carry a class for
# the annotations but the guard does not wrap them in a plan session --
# plans.apply does its own gating (expiry, once-only, confirm_destructive).
EXECUTORS: FrozenSet[str] = frozenset({"plans.apply_plan"})

# SPEND tools whose effective class is decided per call by the direction of
# the change: a decrease takes the PROTECTIVE path, an increase the SPEND
# path. See ads_mcp/guardrails/direction.py for the rules.
DIRECTIONAL: FrozenSet[str] = frozenset(
    {
        "campaigns.update_campaign_budget",
        "campaigns.update_campaign",
        "campaigns.set_device_bid_modifiers",
        "campaigns.set_demographic_bid_modifiers",
        "campaigns.set_location_bid_modifiers",
        "campaigns.set_campaign_geo_target_type",
        "campaigns.set_campaign_geo_targets",
        "campaigns.set_campaign_languages",
        "adgroups.update_ad_group_bid",
        "adgroups.update_keyword",
    }
)

# Legacy status tools accept REMOVED as a status, and update_keyword removes
# and re-creates the keyword when its match type changes. A remove operation
# inside one of these escalates the call to DESTRUCTIVE.
REMOVE_ESCALATES: FrozenSet[str] = frozenset(
    {
        "campaigns.set_campaign_status",
        "adgroups.set_ad_group_status",
        "adgroups.set_keyword_status",
        "adgroups.set_ad_status",
        "adgroups.update_keyword",
        "conversions.update_conversion_action",
    }
)

# Deprecated: kept for one release in the SPEND class. The pause_* and
# enable_* tools replace them, so a client can allow pausing without also
# allowing enabling.
DEPRECATED: FrozenSet[str] = frozenset(
    {
        "campaigns.set_campaign_status",
        "adgroups.set_ad_group_status",
        "adgroups.set_keyword_status",
        "adgroups.set_ad_status",
    }
)

_ANNOTATIONS = {
    READ: {"read_only_hint": True, "destructive_hint": False},
    PROTECTIVE: {"read_only_hint": False, "destructive_hint": False},
    SPEND: {"read_only_hint": False, "destructive_hint": False},
    DESTRUCTIVE: {"read_only_hint": False, "destructive_hint": True},
}


def tool_class(namespace: str, tool_name: str) -> str:
    """Returns the class of a tool, or raises UnclassifiedTool."""
    key = qualified_name(namespace, tool_name)
    try:
        return TOOL_CLASSES[key]
    except KeyError:
        raise UnclassifiedTool(
            f"Tool '{key}' has no class. Add it to TOOL_CLASSES in "
            f"ads_mcp/guardrails/classes.py as READ, PROTECTIVE, SPEND or "
            f"DESTRUCTIVE before mounting it."
        ) from None


def annotations_for(cls: str) -> ToolAnnotations:
    """The MCP annotations a client sees for a class."""
    if cls not in _ANNOTATIONS:
        raise ValueError(f"Unknown tool class {cls!r}.")
    return ToolAnnotations(**_ANNOTATIONS[cls])


def is_directional(namespace: str, tool_name: str) -> bool:
    return qualified_name(namespace, tool_name) in DIRECTIONAL
