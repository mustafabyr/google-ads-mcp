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

"""Plans: a SPEND or DESTRUCTIVE write, described first and applied second.

A plan is what a SPEND or DESTRUCTIVE tool returns instead of writing. It
holds the tool and its arguments, the reads the tool made (the "before"),
the operations it would have sent (the "after"), the money involved, the
limits verdict and how to undo it. It lives on disk under
``GOOGLE_ADS_MCP_PLAN_DIR`` (default ``~/.google-ads-mcp/plans``) for
``GOOGLE_ADS_MCP_PLAN_TTL_MINUTES`` (default 30).

``apply`` runs the same tool again with the same arguments. Before the
first write the session compares every read with the plan's; if the
account changed in between, the write is refused and the plan is marked
stale. A plan applies once: a second apply is refused, and a lock file
keeps two concurrent applies apart. A DESTRUCTIVE plan needs
``confirm_destructive=True``.

With ``GOOGLE_ADS_MCP_REQUIRE_DRY_RUN=true`` the first apply of every plan
runs with ``validate_only`` and only the second one writes.
"""

import contextlib
import dataclasses
import datetime
import json
import logging
import os
import pathlib
import secrets
from typing import Any, Dict, Iterator, List, Optional

from ads_mcp.guardrails import classes
from ads_mcp.guardrails.session import (
    APPLY,
    PlanPreconditionFailed,
    ReadRecord,
)

logger = logging.getLogger(__name__)

PLAN_DIR_ENV = "GOOGLE_ADS_MCP_PLAN_DIR"
DEFAULT_PLAN_DIR = "~/.google-ads-mcp/plans"
# On Cloud Run an instance can be recycled between the plan and the apply
# (min-instances is 0), so plans live in a bucket there, not on the disk.
PLAN_GCS_BUCKET_ENV = "GOOGLE_ADS_MCP_PLAN_GCS_BUCKET"
GCS_PREFIX = "plans"
TTL_ENV = "GOOGLE_ADS_MCP_PLAN_TTL_MINUTES"
DEFAULT_TTL_MINUTES = 30
KEEP_FINISHED_DAYS = 7

PENDING = "pending"
VALIDATED = "validated"
APPLIED = "applied"
STALE = "stale"
FAILED = "failed"
EXPIRED = "expired"
DISCARDED = "discarded"

OPEN_STATES = (PENDING, VALIDATED)


class PlanError(RuntimeError):
    """Base class for plan refusals."""


class PlanNotFound(PlanError):
    pass


class PlanExpired(PlanError):
    pass


class PlanNotOpen(PlanError):
    """Applied, stale, failed or discarded: cannot be applied (again)."""


class DestructiveConfirmationRequired(PlanError):
    pass


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(when: datetime.datetime) -> str:
    return when.isoformat(timespec="seconds")


def _parse(when: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(when)


def ttl_minutes() -> int:
    try:
        return int(os.environ.get(TTL_ENV) or DEFAULT_TTL_MINUTES)
    except ValueError:
        return DEFAULT_TTL_MINUTES


def plan_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(PLAN_DIR_ENV) or DEFAULT_PLAN_DIR
    ).expanduser()


def new_plan_id(now: Optional[datetime.datetime] = None) -> str:
    now = now or utcnow()
    return f"plan-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


@dataclasses.dataclass
class Plan:
    plan_id: str
    created_at: str
    expires_at: str
    status: str
    tool: str
    static_class: str
    effective_class: str
    customer_id: str
    arguments: Dict[str, Any]
    reads: List[Dict[str, Any]]
    writes: List[Dict[str, Any]]
    changes: List[Dict[str, Any]]
    impact: Dict[str, Any]
    limits: Dict[str, Any]
    rollback: List[str]
    actor: str = ""
    validated_at: Optional[str] = None
    applied_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Plan":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def is_expired(self, now: Optional[datetime.datetime] = None) -> bool:
        return (now or utcnow()) >= _parse(self.expires_at)

    def minutes_left(self, now: Optional[datetime.datetime] = None) -> float:
        delta = _parse(self.expires_at) - (now or utcnow())
        return round(max(delta.total_seconds(), 0) / 60, 1)

    def summary(self, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """What the model sees. Everything a person needs to say yes or no."""
        destructive = self.effective_class == classes.DESTRUCTIVE
        if self.status in OPEN_STATES and not self.is_expired(now):
            next_step = (
                f"Show the changes and the impact to the user. If they "
                f"agree, call plans_apply_plan(plan_id=\"{self.plan_id}\""
                + (", confirm_destructive=True" if destructive else "")
                + f") within {self.minutes_left(now)} minutes. Nothing has "
                f"been written yet."
            )
        else:
            next_step = f"This plan is {self.status}; make a new one if needed."
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "class": self.effective_class,
            "tool": self.tool,
            "customer_id": self.customer_id,
            "arguments": self.arguments,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "changes": self.changes,
            "impact": self.impact,
            "limits": self.limits,
            "rollback": self.rollback,
            "reads_snapshotted": len(self.reads),
            "validated_at": self.validated_at,
            "applied_at": self.applied_at,
            "error": self.error,
            "next_step": next_step,
        }


class PlanStore:
    """Plans as JSON files, one per plan, in one directory."""

    def __init__(self, directory: Optional[pathlib.Path] = None) -> None:
        self.directory = directory or plan_dir()

    def path(self, plan_id: str) -> pathlib.Path:
        safe = "".join(ch for ch in plan_id if ch.isalnum() or ch in "-_")
        if safe != plan_id or not plan_id:
            raise PlanNotFound(f"Not a plan id: {plan_id!r}.")
        return self.directory / f"{plan_id}.json"

    def save(self, plan: Plan) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(plan.plan_id)
        tmp = target.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(plan.to_json(), handle, indent=1, default=str)
        os.replace(tmp, target)

    def load(self, plan_id: str) -> Plan:
        target = self.path(plan_id)
        if not target.exists():
            raise PlanNotFound(
                f"No plan {plan_id} in {self.directory}. Plans expire after "
                f"{ttl_minutes()} minutes; make a new one."
            )
        with open(target, "r", encoding="utf-8") as handle:
            return Plan.from_json(json.load(handle))

    def all(self) -> List[Plan]:
        if not self.directory.exists():
            return []
        plans = []
        for file in sorted(self.directory.glob("plan-*.json")):
            try:
                with open(file, "r", encoding="utf-8") as handle:
                    plans.append(Plan.from_json(json.load(handle)))
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("Unreadable plan file %s: %s", file, exc)
        return plans

    @contextlib.contextmanager
    def lock(self, plan_id: str) -> Iterator[None]:
        """A file lock so two applies of one plan cannot overlap."""
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.path(plan_id).with_suffix(".lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise PlanNotOpen(
                f"Plan {plan_id} is being applied right now by another "
                f"call. Wait for it and read the plan's status."
            ) from None
        try:
            os.close(fd)
            yield
        finally:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass

    def purge(self, now: Optional[datetime.datetime] = None) -> int:
        """Expires open plans past their time; deletes old finished ones."""
        now = now or utcnow()
        removed = 0
        for plan in self.all():
            if plan.status in OPEN_STATES and plan.is_expired(now):
                plan.status = EXPIRED
                self.save(plan)
            created = _parse(plan.created_at)
            if plan.status not in OPEN_STATES and (
                now - created
            ) > datetime.timedelta(days=KEEP_FINISHED_DAYS):
                try:
                    self.path(plan.plan_id).unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed


class GcsPlanStore(PlanStore):
    """The same store, as objects in a bucket, for Cloud Run."""

    def __init__(self, bucket_name: str) -> None:
        super().__init__(pathlib.Path(f"gs://{bucket_name}/{GCS_PREFIX}"))
        from google.cloud import storage  # optional dependency
        from google.api_core import exceptions as gexc

        self._exc = gexc
        self._bucket = storage.Client().bucket(bucket_name)
        self._bucket_name = bucket_name

    def _blob(self, plan_id: str, suffix: str = ".json"):
        self.path(plan_id)  # validates the id
        return self._bucket.blob(f"{GCS_PREFIX}/{plan_id}{suffix}")

    def save(self, plan: Plan) -> None:
        self._blob(plan.plan_id).upload_from_string(
            json.dumps(plan.to_json(), default=str),
            content_type="application/json",
        )

    def load(self, plan_id: str) -> Plan:
        try:
            text = self._blob(plan_id).download_as_text()
        except self._exc.NotFound:
            raise PlanNotFound(
                f"No plan {plan_id} in gs://{self._bucket_name}/{GCS_PREFIX}. "
                f"Plans expire after {ttl_minutes()} minutes; make a new one."
            ) from None
        return Plan.from_json(json.loads(text))

    def all(self) -> List[Plan]:
        plans = []
        for blob in self._bucket.list_blobs(prefix=f"{GCS_PREFIX}/plan-"):
            if not blob.name.endswith(".json"):
                continue
            try:
                plans.append(Plan.from_json(json.loads(blob.download_as_text())))
            except (ValueError, TypeError) as exc:
                logger.warning("Unreadable plan object %s: %s", blob.name, exc)
        return sorted(plans, key=lambda p: p.plan_id)

    @contextlib.contextmanager
    def lock(self, plan_id: str) -> Iterator[None]:
        blob = self._blob(plan_id, ".lock")
        try:
            blob.upload_from_string("locked", if_generation_match=0)
        except self._exc.PreconditionFailed:
            raise PlanNotOpen(
                f"Plan {plan_id} is being applied right now by another "
                f"call. Wait for it and read the plan's status."
            ) from None
        try:
            yield
        finally:
            try:
                blob.delete()
            except Exception:  # noqa: BLE001
                logger.warning("Could not remove lock for plan %s", plan_id)

    def purge(self, now: Optional[datetime.datetime] = None) -> int:
        now = now or utcnow()
        removed = 0
        for plan in self.all():
            if plan.status in OPEN_STATES and plan.is_expired(now):
                plan.status = EXPIRED
                self.save(plan)
            created = _parse(plan.created_at)
            if plan.status not in OPEN_STATES and (
                now - created
            ) > datetime.timedelta(days=KEEP_FINISHED_DAYS):
                try:
                    self._blob(plan.plan_id).delete()
                    removed += 1
                except Exception:  # noqa: BLE001
                    pass
        return removed


def store() -> PlanStore:
    bucket = os.environ.get(PLAN_GCS_BUCKET_ENV)
    if bucket:
        return GcsPlanStore(bucket)
    return PlanStore()


# --------------------------------------------------------------------------
# Creating, applying, discarding
# --------------------------------------------------------------------------


def create(
    *,
    tool: str,
    static_class: str,
    effective_class: str,
    customer_id: str,
    arguments: Dict[str, Any],
    reads: List[ReadRecord],
    writes: List[Any],
    changes: List[Dict[str, Any]],
    impact: Dict[str, Any],
    limits: Dict[str, Any],
    rollback: List[str],
    actor: str = "",
    now: Optional[datetime.datetime] = None,
) -> Plan:
    now = now or utcnow()
    plan = Plan(
        plan_id=new_plan_id(now),
        created_at=_iso(now),
        expires_at=_iso(now + datetime.timedelta(minutes=ttl_minutes())),
        status=PENDING,
        tool=tool,
        static_class=static_class,
        effective_class=effective_class,
        customer_id=customer_id,
        arguments=json.loads(json.dumps(arguments, default=str)),
        reads=[r.to_json() for r in reads],
        writes=[w.to_json() for w in writes],
        changes=changes,
        impact=impact,
        limits=limits,
        rollback=rollback,
        actor=actor,
    )
    st = store()
    st.save(plan)
    try:
        st.purge(now)
    except OSError as exc:  # housekeeping must never block a plan
        logger.warning("Plan purge failed: %s", exc)
    return plan


def _summarize_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return json.loads(json.dumps(result, default=str))
    return {"value": str(result)}


def apply(
    plan_id: str,
    confirm_destructive: bool = False,
    validate_only: bool = False,
) -> Dict[str, Any]:
    from ads_mcp.guardrails import guard  # lazy: guard imports this module

    st = store()
    plan = st.load(plan_id)
    now = utcnow()

    if plan.status == APPLIED:
        raise PlanNotOpen(
            f"Plan {plan_id} was already applied at {plan.applied_at}. A "
            f"plan applies once; make a new one for a further change."
        )
    if plan.status not in OPEN_STATES:
        raise PlanNotOpen(
            f"Plan {plan_id} is {plan.status}"
            + (f": {plan.error}" if plan.error else "")
            + ". Make a new plan."
        )
    if plan.is_expired(now):
        plan.status = EXPIRED
        st.save(plan)
        raise PlanExpired(
            f"Plan {plan_id} expired at {plan.expires_at} (plans live "
            f"{ttl_minutes()} minutes). Make a new one -- the account may "
            f"have changed since."
        )
    if plan.effective_class == classes.DESTRUCTIVE and not confirm_destructive:
        raise DestructiveConfirmationRequired(
            f"Plan {plan_id} removes or detaches something and cannot be "
            f"undone: {'; '.join(plan.rollback)[:500]}. Tell the user what "
            f"is lost, then call again with confirm_destructive=True."
        )

    dry = bool(validate_only) or (
        guard.require_dry_run() and plan.validated_at is None
    )

    with st.lock(plan_id):
        plan = st.load(plan_id)
        if plan.status == APPLIED:
            raise PlanNotOpen(f"Plan {plan_id} was applied meanwhile.")
        fn = guard.ORIGINALS.get(plan.tool)
        if fn is None:
            raise PlanError(
                f"The tool {plan.tool} behind plan {plan_id} is not mounted "
                f"on this server."
            )
        expected = [ReadRecord.from_json(r) for r in plan.reads]
        try:
            result, session = guard.run_in_session(
                fn,
                plan.arguments,
                APPLY,
                tool=plan.tool,
                customer_id=plan.customer_id,
                expected_reads=expected,
                validate_only=dry,
                planned=True,
            )
        except PlanPreconditionFailed as exc:
            plan.status = STALE
            plan.error = str(exc)
            st.save(plan)
            guard.notify(
                guard.event(
                    tool=plan.tool, cls=plan.effective_class,
                    customer_id=plan.customer_id, plan_id=plan.plan_id,
                    reads=[], writes=[], result="precondition_failed",
                    error=str(exc), arguments=plan.arguments,
                )
            )
            raise
        except Exception as exc:
            if not dry:
                plan.status = FAILED
                plan.error = str(exc)[:1000]
                st.save(plan)
            guard.notify(
                guard.event(
                    tool=plan.tool, cls=plan.effective_class,
                    customer_id=plan.customer_id, plan_id=plan.plan_id,
                    reads=[], writes=[], result="error", error=str(exc)[:1000],
                    arguments=plan.arguments, validate_only=dry,
                )
            )
            raise

        if dry:
            plan.validated_at = _iso(utcnow())
            plan.status = VALIDATED
            st.save(plan)
            guard.notify(
                guard.event(
                    tool=plan.tool, cls=plan.effective_class,
                    customer_id=plan.customer_id, plan_id=plan.plan_id,
                    reads=session.reads, writes=session.writes,
                    result="validated", arguments=plan.arguments,
                    validate_only=True,
                )
            )
            return {
                "plan_id": plan.plan_id,
                "status": VALIDATED,
                "validated_at": plan.validated_at,
                "precondition": f"{len(session.reads)} read(s) still match the plan",
                "validated_writes": [w.to_json() for w in session.writes],
                "note": (
                    "Google validated the request and nothing was written. "
                    "Call plans_apply_plan again with the same plan_id to "
                    "write it."
                    if not validate_only
                    else "Google validated the request; nothing was written."
                ),
            }

        plan.status = APPLIED
        plan.applied_at = _iso(utcnow())
        plan.result = _summarize_result(result)
        st.save(plan)
        guard.notify(
            guard.event(
                tool=plan.tool, cls=plan.effective_class,
                customer_id=plan.customer_id, plan_id=plan.plan_id,
                reads=session.reads, writes=session.writes, result="applied",
                arguments=plan.arguments, output=plan.result,
            )
        )
        return {
            "plan_id": plan.plan_id,
            "status": APPLIED,
            "applied_at": plan.applied_at,
            "precondition": f"{len(session.reads)} read(s) matched the plan",
            "requested": plan.changes,
            "actual": plan.result,
            "note": (
                "'actual' is the tool's own read-back after the write, not "
                "the request. Compare it with 'requested'."
            ),
        }


def discard(plan_id: str) -> Dict[str, Any]:
    st = store()
    plan = st.load(plan_id)
    if plan.status == APPLIED:
        raise PlanNotOpen(
            f"Plan {plan_id} was applied at {plan.applied_at}; it cannot be "
            f"discarded, only rolled back: {'; '.join(plan.rollback)[:400]}"
        )
    previous = plan.status
    plan.status = DISCARDED
    st.save(plan)
    return {"plan_id": plan_id, "status": DISCARDED, "was": previous}


def get(plan_id: str) -> Dict[str, Any]:
    plan = store().load(plan_id)
    if plan.status in OPEN_STATES and plan.is_expired():
        plan.status = EXPIRED
        store().save(plan)
    return plan.summary()


def list_plans(
    customer_id: Optional[str] = None, include_finished: bool = False
) -> List[Dict[str, Any]]:
    st = store()
    st.purge()
    wanted = "".join(ch for ch in str(customer_id or "") if ch.isdigit())
    out = []
    for plan in st.all():
        if wanted and plan.customer_id != wanted:
            continue
        if not include_finished and plan.status not in OPEN_STATES:
            continue
        summary = plan.summary()
        summary.pop("changes", None)
        summary.pop("rollback", None)
        summary.pop("arguments", None)
        out.append(summary)
    out.sort(key=lambda p: p["created_at"], reverse=True)
    return out
