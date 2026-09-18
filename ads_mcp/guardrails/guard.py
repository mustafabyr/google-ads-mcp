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

"""The function that stands in front of every tool.

``wrap`` returns a callable with the same signature as the tool. What it
does around the call depends on the tool's class:

  READ         passes straight through.
  PROTECTIVE   runs the tool in a direct session, so the reads before its
               first write and the writes themselves are recorded, and
               tells the write listeners (the audit log) afterwards.
  SPEND,
  DESTRUCTIVE  refuses if the account has no row in the limits file. Then
               runs the tool in PLAN mode, where nothing is sent, and
               looks at what it tried to write:
                 - a DIRECTIONAL tool whose changes are all decreases is
                   written straight away, the PROTECTIVE way;
                 - otherwise the captured changes are checked against the
                   account's ceilings, and a plan is stored and returned.
               The plan is applied by plans.apply, which runs the tool
               again in APPLY mode with the plan's reads as preconditions.

The confirm_* flags the tools carry (confirm_spend, confirm_remove, ...)
are the pre-plan way of saying "the user agreed". Under the plan flow the
plan itself is that step, so the guard passes them as True when it runs a
SPEND or DESTRUCTIVE tool in PLAN or APPLY mode. A DESTRUCTIVE plan still
needs confirm_destructive=True at apply time.
"""

import datetime
import functools
import getpass
import inspect
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from ads_mcp.guardrails import changes as changes_mod
from ads_mcp.guardrails import classes, direction, impact, limits, quota
from ads_mcp.guardrails.session import (
    APPLY,
    DIRECT,
    PLAN,
    ReadRecord,
    WriteSession,
)

logger = logging.getLogger(__name__)

REQUIRE_DRY_RUN_ENV = "GOOGLE_ADS_MCP_REQUIRE_DRY_RUN"
ACTOR_ENV = "GOOGLE_ADS_MCP_ACTOR"

CONFIRM_FLAGS = (
    "confirm_spend",
    "confirm_remove",
    "confirm",
    "confirm_widens_reach",
    "confirm_reach_cut",
)

# Every guarded function carries these so tests and the plan executor can
# find their way back to the bare tool.
ORIGINALS: Dict[str, Callable[..., Any]] = {}

# Called with an event dict after every write attempt. The audit log
# registers itself here.
WRITE_LISTENERS: List[Callable[[Dict[str, Any]], None]] = []

# Set by the audit log: customer_id -> {budget resource name: last change}.
# Feeds the "min days between budget changes" rule.
BUDGET_HISTORY: Optional[Callable[[str], Dict[str, datetime.datetime]]] = None


def require_dry_run() -> bool:
    return os.environ.get(REQUIRE_DRY_RUN_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def current_actor() -> str:
    """Who is calling: the OAuth user when there is one, else the env/OS user."""
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token is not None:
            claims = getattr(token, "claims", None) or {}
            email = claims.get("email")
            if email:
                return str(email)
    except Exception:  # noqa: BLE001 - no request context, no token
        pass
    actor = os.environ.get(ACTOR_ENV)
    if actor:
        return actor
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def bound_kwargs(
    fn: Callable[..., Any], args: tuple, kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """The call's arguments as one dict, however they were passed."""
    bound = inspect.signature(fn).bind_partial(*args, **kwargs)
    return dict(bound.arguments)


def with_confirmations(fn: Callable[..., Any], call: Dict[str, Any]) -> Dict[str, Any]:
    params = inspect.signature(fn).parameters
    out = dict(call)
    for flag in CONFIRM_FLAGS:
        if flag in params:
            out[flag] = True
    return out


def run_in_session(
    fn: Callable[..., Any],
    call: Dict[str, Any],
    mode: str,
    *,
    tool: str,
    customer_id: str,
    expected_reads: Optional[List[ReadRecord]] = None,
    validate_only: bool = False,
    planned: bool = False,
) -> Tuple[Any, WriteSession]:
    """Runs ``fn(**call)`` inside a write session and returns both."""
    call = with_confirmations(fn, call) if planned else dict(call)
    session = WriteSession(
        mode,
        tool=tool,
        customer_id=customer_id,
        validate_only=validate_only,
        expected_reads=expected_reads,
    )
    with session:
        result = fn(**call)
    return result, session


def event(
    *,
    tool: str,
    cls: str,
    customer_id: str,
    plan_id: Optional[str],
    reads: List[ReadRecord],
    writes: List[Any],
    result: str,
    arguments: Dict[str, Any],
    error: Optional[str] = None,
    output: Any = None,
    validate_only: bool = False,
) -> Dict[str, Any]:
    return {
        "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "customer_id": limits.clean_id(customer_id),
        "tool": tool,
        "tool_class": cls,
        "plan_id": plan_id,
        "actor": current_actor(),
        "arguments": arguments,
        "before": [r.to_json() for r in reads],
        "after": [w.to_json() if hasattr(w, "to_json") else w for w in writes],
        "result": result,
        "error": error,
        "output": output,
        "validate_only": validate_only,
    }


def notify(payload: Dict[str, Any]) -> None:
    for listener in list(WRITE_LISTENERS):
        try:
            listener(payload)
        except Exception:  # noqa: BLE001 - a logging failure must not undo a write
            logger.exception("Write listener %r failed", listener)


def _budget_history(customer_id: str) -> Dict[str, datetime.datetime]:
    if BUDGET_HISTORY is None:
        return {}
    try:
        return BUDGET_HISTORY(customer_id) or {}
    except Exception:  # noqa: BLE001
        logger.exception("Budget history lookup failed")
        return {}


def run_protective(
    key: str,
    fn: Callable[..., Any],
    call: Dict[str, Any],
    customer_id: str,
    *,
    expected_reads: Optional[List[ReadRecord]] = None,
    planned: bool = False,
    effective_class: str = classes.PROTECTIVE,
) -> Any:
    """Writes now. With expected reads, checks them first (APPLY mode)."""
    mode = APPLY if expected_reads is not None else DIRECT
    if require_dry_run():
        # The first call to Google is a validation; only then the write.
        run_in_session(
            fn, call, APPLY, tool=key, customer_id=customer_id,
            expected_reads=expected_reads, validate_only=True, planned=planned,
        )
    try:
        result, session = run_in_session(
            fn, call, mode, tool=key, customer_id=customer_id,
            expected_reads=expected_reads, planned=planned,
        )
    except Exception as exc:
        notify(
            event(
                tool=key, cls=effective_class, customer_id=customer_id,
                plan_id=None, reads=[], writes=[], result="error",
                error=str(exc)[:1000], arguments=call,
            )
        )
        raise
    if session.writes:
        notify(
            event(
                tool=key, cls=effective_class, customer_id=customer_id,
                plan_id=None, reads=session.reads, writes=session.writes,
                result="applied", arguments=call, output=result,
            )
        )
    return result


def plan_or_write(
    key: str, cls: str, fn: Callable[..., Any], call: Dict[str, Any]
) -> Any:
    from ads_mcp.guardrails import plans  # lazy: plans imports this module

    customer_id = limits.clean_id(call.get("customer_id"))
    limits.require_account(customer_id, tool=key)

    # Dry pass: nothing is sent, everything is captured.
    result, session = run_in_session(
        fn, call, PLAN, tool=key, customer_id=customer_id, planned=True
    )
    if not session.writes:
        # The tool decided there was nothing to write (an extension that
        # already exists, a label already applied ...). Its reads were
        # real, so its answer is real; there is nothing to plan.
        return result
    change_list = changes_mod.from_session(session)
    rows = session.rows
    effective = direction.effective_class(key, cls, change_list, rows)

    if effective == classes.PROTECTIVE:
        return run_protective(
            key, fn, call, customer_id,
            expected_reads=session.reads, planned=True,
            effective_class=classes.PROTECTIVE,
        )

    verdict = limits.check_changes(
        customer_id, change_list, last_budget_change=_budget_history(customer_id)
    )
    plan = plans.create(
        tool=key,
        static_class=cls,
        effective_class=effective,
        customer_id=customer_id,
        arguments=call,
        reads=session.reads,
        writes=session.writes,
        changes=[changes_mod.to_json(c) for c in change_list],
        impact=impact.estimate(change_list, rows),
        limits=verdict,
        rollback=impact.rollback(change_list, key),
        actor=current_actor(),
    )
    return plan.summary()


def with_quota_warning(result: Any) -> Any:
    """Adds the day's quota warning to a dict result once past the mark."""
    try:
        message = quota.warning()
    except Exception:  # noqa: BLE001 - never fail a tool over bookkeeping
        return result
    if message and isinstance(result, dict):
        result.setdefault("quota_warning", message)
    return result


def wrap(
    fn: Callable[..., Any], *, namespace: str, tool_name: str, cls: str
) -> Callable[..., Any]:
    """Returns the guarded version of ``fn``."""
    if cls not in classes.ALL_CLASSES:
        raise ValueError(f"Unknown tool class {cls!r} for {tool_name}.")
    key = classes.qualified_name(namespace, tool_name)
    ORIGINALS[key] = fn

    @functools.wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        return with_quota_warning(_dispatch(*args, **kwargs))

    def _dispatch(*args: Any, **kwargs: Any) -> Any:
        if cls == classes.READ or key in classes.EXECUTORS:
            return fn(*args, **kwargs)
        call = bound_kwargs(fn, args, kwargs)
        customer_id = call.get("customer_id")
        if cls == classes.PROTECTIVE:
            return run_protective(
                key, fn, call, limits.clean_id(customer_id or "")
            )
        if not customer_id:
            raise ValueError(f"{key} is a {cls} tool and needs customer_id.")
        return plan_or_write(key, cls, fn, call)

    guarded.__guardrail_class__ = cls  # type: ignore[attr-defined]
    guarded.__guardrail_name__ = key  # type: ignore[attr-defined]
    return guarded
