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

"""The second step of every write that can spend or remove.

A SPEND or DESTRUCTIVE tool does not write. It returns a PLAN: the objects
it would touch, their state before and after, the money involved, whether
the account's ceilings allow it, and how to undo it. Nothing has happened
at that point. apply_plan is what happens.

Applying re-reads the objects first and refuses if they no longer match
the plan -- somebody changed the budget in the Google Ads interface in the
meantime, say. It writes once: a second apply of the same plan is refused.
Plans expire after thirty minutes, because the account they describe
drifts.
"""

from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ads_mcp.guardrails import plans as plan_store

plans_mcp = FastMCP("plans")


@plans_mcp.tool
def apply_plan(
    plan_id: str,
    confirm_destructive: bool = False,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Applies a plan produced by a SPEND or DESTRUCTIVE tool.

    Before writing, every object the plan read is read again and compared
    with the plan's "before". If anything differs, the write is refused and
    the plan is marked stale: make a new one. After writing, the tool's own
    read-back is returned as `actual`, next to what the plan `requested`.

    A plan applies exactly once. A DESTRUCTIVE plan (something is removed
    or detached) also needs confirm_destructive=True, given only after the
    user has been told what is lost.

    With GOOGLE_ADS_MCP_REQUIRE_DRY_RUN=true the first apply of a plan is
    a validation only and the second one writes.

    Args:
        plan_id: From the plan returned by the tool.
        confirm_destructive: Required for a DESTRUCTIVE plan.
        validate_only: True to have Google validate the request without
            writing anything. Repeatable; does not consume the plan.

    Returns:
        The plan's status, what was requested, and what the API reads back.
    """
    return plan_store.apply(
        plan_id,
        confirm_destructive=confirm_destructive,
        validate_only=validate_only,
    )


@plans_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_plan(plan_id: str) -> Dict[str, Any]:
    """Shows a plan: its changes, impact, limits verdict, rollback and status.

    Args:
        plan_id: The plan to show.

    Returns:
        The plan summary, including how many minutes it has left.
    """
    return plan_store.get(plan_id)


@plans_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_plans(
    customer_id: Optional[str] = None,
    include_finished: bool = False,
) -> List[Dict[str, Any]]:
    """Lists open plans, newest first.

    Args:
        customer_id: Limit to one account. Omit for every account.
        include_finished: True to also list applied, stale, failed,
            expired and discarded plans (kept for seven days).

    Returns:
        One summary per plan, without the full change list.
    """
    return plan_store.list_plans(
        customer_id=customer_id, include_finished=include_finished
    )


@plans_mcp.tool
def discard_plan(plan_id: str) -> Dict[str, Any]:
    """Discards an open plan so it can never be applied.

    Nothing in the account changes. An applied plan cannot be discarded;
    its rollback recipe is the way back.

    Args:
        plan_id: The plan to discard.

    Returns:
        The plan's id and its new status.
    """
    return plan_store.discard(plan_id)
