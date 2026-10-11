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

"""Who may talk to this server over the network.

Over stdio the caller is whoever runs the process, and nothing here
applies. Over HTTP the caller is whoever holds a Google OAuth token that
FastMCP's GoogleProvider accepted -- which is any Google account, unless
the server checks the email. ``GOOGLE_ADS_MCP_ALLOWED_EMAILS`` is that
check: a comma-separated list of the Google accounts allowed in. A
request from anyone else is refused before it reaches a tool.

An HTTP server without an allow list does not start (see
``require_network_auth`` in server.py): this server can spend an
advertiser's money, so an open endpoint is never published by accident.
"""

import logging
import os
from typing import Any, FrozenSet, Optional

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

ALLOWED_EMAILS_ENV = "GOOGLE_ADS_MCP_ALLOWED_EMAILS"


def allowed_emails() -> FrozenSet[str]:
    raw = os.environ.get(ALLOWED_EMAILS_ENV, "")
    return frozenset(
        e.strip().lower() for e in raw.replace(";", ",").split(",") if e.strip()
    )


def caller_email() -> Optional[str]:
    """The email on the current request's OAuth token, if any."""
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:  # noqa: BLE001 - outside a request there is no token
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    email = claims.get("email")
    return str(email).lower() if email else None


class AccessDenied(ToolError):
    """The caller's Google account is not on the allow list."""


class AllowedEmailsMiddleware(Middleware):
    """Refuses every request whose token email is not on the allow list.

    Applies only when an allow list is configured; with none, the
    middleware is inert (and the server refuses to run over HTTP anyway).
    Requests with no token at all (stdio) pass, because there is nothing
    to check and nothing to protect against: the caller owns the process.
    """

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        allowed = allowed_emails()
        if allowed:
            email = caller_email()
            if email is not None and email not in allowed:
                logger.warning("Refused request from %s (not on the allow list)", email)
                raise AccessDenied(
                    f"{email} is not allowed to use this server. Ask the "
                    f"operator to add the address to {ALLOWED_EMAILS_ENV}."
                )
        return await call_next(context)
