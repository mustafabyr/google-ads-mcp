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

``wrap`` returns a callable with the same signature as the tool; what it
does around the call depends on the tool's class. In this revision it
records the class on the function and passes the call through; the
plan/apply and audit behaviour is layered on in the session module.
"""

import functools
from typing import Any, Callable, Dict

from ads_mcp.guardrails import classes

# Every guarded function carries these so tests and the plan executor can
# find their way back to the bare tool.
ORIGINALS: Dict[str, Callable[..., Any]] = {}


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
        return fn(*args, **kwargs)

    guarded.__guardrail_class__ = cls  # type: ignore[attr-defined]
    guarded.__guardrail_name__ = key  # type: ignore[attr-defined]
    return guarded
