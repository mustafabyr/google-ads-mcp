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

"""The daily operation budget, counted across everyone using the server.

Basic Access allows 15,000 operations per day (docs/kaynaklar.md, row 2).
Several analysts share that through one server, so the count has to be
one count: a file per UTC day under ``GOOGLE_ADS_MCP_QUOTA_DIR``
(default ``~/.google-ads-mcp/quota``), and on Cloud Run an object per
day in ``GOOGLE_ADS_MCP_QUOTA_GCS_BUCKET`` updated with a generation
check, so two instances do not overwrite each other.

The count is an approximation of Google's: one read request is counted
as one operation and a mutate as the number of operations it carries.
Google's own count is the authority; this one exists so the 70% mark is
noticed before the day's limit is hit. Every tool result carries a
``quota_warning`` from that mark on.
"""

import datetime
import json
import logging
import os
import pathlib
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

QUOTA_DIR_ENV = "GOOGLE_ADS_MCP_QUOTA_DIR"
DEFAULT_QUOTA_DIR = "~/.google-ads-mcp/quota"
QUOTA_GCS_BUCKET_ENV = "GOOGLE_ADS_MCP_QUOTA_GCS_BUCKET"
DAILY_LIMIT_ENV = "GOOGLE_ADS_MCP_DAILY_OPERATIONS"
DEFAULT_DAILY_LIMIT = 15_000
WARN_AT = 0.70

_lock = threading.Lock()


def daily_limit() -> int:
    try:
        return int(os.environ.get(DAILY_LIMIT_ENV) or DEFAULT_DAILY_LIMIT)
    except ValueError:
        return DEFAULT_DAILY_LIMIT


def quota_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(QUOTA_DIR_ENV) or DEFAULT_QUOTA_DIR
    ).expanduser()


def today(now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d")


def _local_path(day: str) -> pathlib.Path:
    return quota_dir() / f"{day}.json"


def _read_local(day: str) -> Dict[str, Any]:
    path = _local_path(day)
    if not path.exists():
        return {"day": day, "operations": 0}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"day": day, "operations": 0}


def _write_local(day: str, data: Dict[str, Any]) -> None:
    path = _local_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _add_gcs(day: str, n: int) -> Optional[int]:
    """Increments the day's object with a generation check; None if unset."""
    bucket_name = os.environ.get(QUOTA_GCS_BUCKET_ENV)
    if not bucket_name:
        return None
    try:
        from google.cloud import storage
        from google.api_core import exceptions as gexc
    except ImportError:
        logger.warning(
            "%s is set but google-cloud-storage is not installed; counting "
            "locally only.", QUOTA_GCS_BUCKET_ENV,
        )
        return None
    try:
        blob = storage.Client().bucket(bucket_name).blob(f"quota/{day}.json")
        for _ in range(5):
            try:
                current = json.loads(blob.download_as_text())
                generation = blob.generation
            except gexc.NotFound:
                current, generation = {"day": day, "operations": 0}, 0
            current["operations"] = int(current.get("operations") or 0) + n
            try:
                blob.upload_from_string(
                    json.dumps(current),
                    content_type="application/json",
                    if_generation_match=generation,
                )
                return int(current["operations"])
            except gexc.PreconditionFailed:
                continue
        logger.warning("Quota object in gs://%s kept changing; gave up", bucket_name)
    except Exception:  # noqa: BLE001 - counting must never break a call
        logger.exception("Quota update in gs://%s failed", bucket_name)
    return None


def add(n: int = 1, now: Optional[datetime.datetime] = None) -> int:
    """Counts ``n`` operations against today and returns today's total."""
    day = today(now)
    with _lock:
        data = _read_local(day)
        data["operations"] = int(data.get("operations") or 0) + int(n)
        try:
            _write_local(day, data)
        except OSError:
            logger.exception("Could not write the local quota file")
    shared = _add_gcs(day, int(n))
    return shared if shared is not None else int(data["operations"])


def used(now: Optional[datetime.datetime] = None) -> int:
    return int(_read_local(today(now)).get("operations") or 0)


def status(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    limit = daily_limit()
    count = used(now)
    return {
        "day_utc": today(now),
        "operations_counted": count,
        "daily_limit": limit,
        "used_pct": round(count / limit * 100, 1) if limit else None,
        "warn_at_pct": int(WARN_AT * 100),
        "basis": (
            "one read request = 1, one mutate = its operation count; an "
            "approximation of Google's own count"
        ),
    }


def warning(total: Optional[int] = None) -> Optional[str]:
    """A sentence when the day's count has passed the warning mark."""
    limit = daily_limit()
    count = used() if total is None else total
    if limit and count >= limit * WARN_AT:
        return (
            f"API quota: about {count} of {limit} daily operations used "
            f"({round(count / limit * 100)}%). Spread analyses out, avoid "
            f"repeated audits, and keep writes for what matters until the "
            f"day (UTC) rolls over."
        )
    return None
