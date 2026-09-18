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

"""Wires the guardrails onto a tool sub-server at mount time.

The coordinator calls ``apply_guardrails`` once per sub-server, after the
per-tool enable/disable filter and before mounting. For every tool it:

  1. looks the tool up in the class registry and refuses to mount an
     unclassified one;
  2. replaces the tool's MCP annotations with the ones its class dictates,
     so a client can allow READ and PROTECTIVE tools blanket and gate the
     rest;
  3. re-registers the tool with the guarded function in place of the bare
     one (see ``guard``).

Only public FastMCP calls are used to swap the tool: ``Tool.from_function``
on the same callable keeps the parameter schema byte-for-byte identical,
which the tests assert.
"""

from typing import Dict, List

from fastmcp import FastMCP
from fastmcp.tools import Tool

from ads_mcp.guardrails import classes


def registered_tools(sub_mcp: FastMCP) -> List[Tool]:
    """The tools currently registered on a sub-server, in registration order."""
    out: List[Tool] = []
    for key, value in sub_mcp.local_provider._components.items():
        if key.startswith("tool:"):
            out.append(value)
    return out


def apply_guardrails(sub_mcp: FastMCP, namespace: str) -> Dict[str, str]:
    """Classifies and re-registers every tool on ``sub_mcp``.

    Returns a map of qualified tool name to class, for logging and tests.
    Raises ``UnclassifiedTool`` if any tool is missing from the registry.
    """
    # Local import: guard pulls in the session machinery, which imports
    # utils; keeping it here avoids an import cycle at package import time.
    from ads_mcp.guardrails import guard

    applied: Dict[str, str] = {}
    for tool in registered_tools(sub_mcp):
        cls = classes.tool_class(namespace, tool.name)
        if getattr(tool.fn, "__guardrail_class__", None) == cls:
            # Already guarded: the sub-servers are module globals, and a
            # test that mounts them into a second parent must not stack a
            # second guard on top of the first.
            applied[classes.qualified_name(namespace, tool.name)] = cls
            continue
        guarded = guard.wrap(
            tool.fn, namespace=namespace, tool_name=tool.name, cls=cls
        )
        replacement = Tool.from_function(
            guarded,
            name=tool.name,
            description=tool.description,
            annotations=classes.annotations_for(cls),
            tags=tool.tags or None,
            meta=tool.meta,
        )
        sub_mcp.local_provider.remove_tool(tool.name)
        sub_mcp.add_tool(replacement)
        applied[classes.qualified_name(namespace, tool.name)] = cls
    return applied
