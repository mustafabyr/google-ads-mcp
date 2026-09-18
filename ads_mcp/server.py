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

"""Entry point for the MCP server."""

from ads_mcp.coordinator import mcp

# The following imports are necessary to register the resources with the `mcp`
# object, even though they are not directly used in this file.
# Tools are loaded dynamically via reflection in coordinator.py.
# The `# noqa: F401` comment tells the linter to ignore the "unused import"
# warning.
from ads_mcp.resources import (
    discovery,
    metrics,
    release_notes,
    segments,
)  # noqa: F401


import os

from ads_mcp.guardrails.access import ALLOWED_EMAILS_ENV, allowed_emails

TRANSPORT_ENV = "GOOGLE_ADS_MCP_TRANSPORT"  # stdio | http | auto (default)
ACCESS_LOG_ENV = "GOOGLE_ADS_MCP_ACCESS_LOG"  # true to log every request


class NetworkAuthMissing(RuntimeError):
    """The server was asked to listen on the network without authentication."""


def require_network_auth(env=None) -> None:
    """Refuses to serve HTTP without OAuth AND an email allow list.

    This server can spend an advertiser's money. An HTTP endpoint that
    anyone can reach, or that any Google account can sign in to, is never
    published -- not even by a typo in the deploy command.
    """
    env = os.environ if env is None else env
    client_id = env.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
    client_secret = env.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise NetworkAuthMissing(
            "HTTP transport needs OAuth: set GOOGLE_ADS_MCP_OAUTH_CLIENT_ID "
            "and GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET, or run over stdio."
        )
    raw = env.get(ALLOWED_EMAILS_ENV, "")
    emails = {e.strip() for e in raw.replace(";", ",").split(",") if e.strip()}
    if not emails:
        raise NetworkAuthMissing(
            f"HTTP transport needs an allow list: set {ALLOWED_EMAILS_ENV} "
            f"to the Google accounts that may use this server. Without it "
            f"any Google account could sign in."
        )


def chosen_transport(env=None) -> str:
    env = os.environ if env is None else env
    wanted = (env.get(TRANSPORT_ENV) or "auto").strip().lower()
    if wanted not in ("stdio", "http", "auto"):
        raise ValueError(f"{TRANSPORT_ENV} must be stdio, http or auto.")
    if wanted == "auto":
        has_oauth = bool(
            env.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
            and env.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
        )
        return "http" if has_oauth else "stdio"
    return wanted


def run_server() -> None:
    transport = chosen_transport()
    port = int(os.environ.get("PORT", "8080"))

    if transport == "http":
        require_network_auth()
        access_log = os.environ.get(ACCESS_LOG_ENV, "").lower() in (
            "1", "true", "yes", "on",
        )
        mcp.run(
            transport="streamable-http",
            port=port,
            host="0.0.0.0",
            uvicorn_config={"access_log": access_log},
        )
    else:
        # stdio: the caller owns the process; nothing to authenticate.
        mcp.run()


if __name__ == "__main__":
    run_server()
