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

"""Every tool has a class, and the class decides what the client sees."""

import importlib
import inspect
import pkgutil
import unittest
import warnings

from fastmcp import FastMCP
from fastmcp.tools import Tool

from ads_mcp.guardrails import classes
from ads_mcp.guardrails.install import apply_guardrails, registered_tools

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _discover():
    """Maps qualified tool name -> (sub-server, Tool) across ads_mcp.tools."""
    import ads_mcp.tools as tools_pkg

    found = {}
    for _, module_name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"ads_mcp.tools.{module_name}")
        for attr in dir(module):
            value = getattr(module, attr)
            if isinstance(value, FastMCP):
                for tool in registered_tools(value):
                    key = classes.qualified_name(value.name, tool.name)
                    found[key] = (value, tool)
    return found


class TestRegistry(unittest.TestCase):
    def setUp(self):
        # Importing the coordinator mounts everything, which applies the
        # guardrails to each sub-server exactly once.
        import ads_mcp.coordinator  # noqa: F401

        self.tools = _discover()

    def test_every_tool_has_a_class(self):
        missing = sorted(
            key for key in self.tools if key not in classes.TOOL_CLASSES
        )
        self.assertEqual(
            missing,
            [],
            "Tools without a class in ads_mcp/guardrails/classes.py: "
            f"{missing}",
        )

    def test_registry_names_only_real_tools(self):
        unknown = sorted(
            key for key in classes.TOOL_CLASSES if key not in self.tools
        )
        self.assertEqual(
            unknown, [], f"TOOL_CLASSES rows with no tool behind them: {unknown}"
        )

    def test_every_class_is_valid(self):
        for key, cls in classes.TOOL_CLASSES.items():
            self.assertIn(cls, classes.ALL_CLASSES, key)

    def test_annotations_follow_the_class(self):
        for key, (_, tool) in self.tools.items():
            cls = classes.TOOL_CLASSES[key]
            expected = classes.annotations_for(cls)
            self.assertIsNotNone(tool.annotations, key)
            self.assertEqual(
                tool.annotations.read_only_hint,
                expected.read_only_hint,
                f"{key} ({cls}) readOnlyHint",
            )
            self.assertEqual(
                tool.annotations.destructive_hint,
                expected.destructive_hint,
                f"{key} ({cls}) destructiveHint",
            )

    def test_read_tools_are_the_only_read_only_ones(self):
        for key, (_, tool) in self.tools.items():
            cls = classes.TOOL_CLASSES[key]
            self.assertEqual(
                tool.annotations.read_only_hint is True, cls == classes.READ, key
            )

    def test_guarded_function_remembers_its_class(self):
        for key, (_, tool) in self.tools.items():
            self.assertEqual(
                getattr(tool.fn, "__guardrail_class__", None),
                classes.TOOL_CLASSES[key],
                key,
            )

    def test_wrapping_keeps_the_schema(self):
        """The guard must not change what the client sees as parameters."""
        from ads_mcp.guardrails import guard

        for key, (_, tool) in self.tools.items():
            original = guard.ORIGINALS[key]
            bare = Tool.from_function(original, name=tool.name)
            self.assertEqual(bare.parameters, tool.parameters, key)

    def test_planned_tools_take_a_customer_id(self):
        """SPEND and DESTRUCTIVE tools are gated per account, so they need one."""
        from ads_mcp.guardrails import guard

        for key, cls in classes.TOOL_CLASSES.items():
            if cls not in classes.PLANNED_CLASSES or key in classes.EXECUTORS:
                continue
            params = inspect.signature(guard.ORIGINALS[key]).parameters
            self.assertIn("customer_id", params, key)

    def test_directional_tools_are_spend_tools(self):
        for key in classes.DIRECTIONAL:
            self.assertEqual(classes.TOOL_CLASSES.get(key), classes.SPEND, key)

    def test_deprecated_status_tools_have_split_replacements(self):
        for key in classes.DEPRECATED:
            self.assertEqual(classes.TOOL_CLASSES[key], classes.SPEND, key)
        for key in (
            "campaigns.pause_campaign",
            "adgroups.pause_ad_group",
            "adgroups.pause_keywords",
            "adgroups.pause_ad",
        ):
            self.assertEqual(classes.TOOL_CLASSES[key], classes.PROTECTIVE)
        for key in (
            "campaigns.enable_campaign",
            "adgroups.enable_ad_group",
            "adgroups.enable_keywords",
            "adgroups.enable_ad",
        ):
            self.assertEqual(classes.TOOL_CLASSES[key], classes.SPEND)


class TestInstall(unittest.TestCase):
    def test_unclassified_tool_refuses_to_mount(self):
        server = FastMCP("throwaway")

        @server.tool
        def brand_new_tool(customer_id: str) -> dict:
            """A tool nobody classified."""
            return {}

        with self.assertRaises(classes.UnclassifiedTool) as ctx:
            apply_guardrails(server, "throwaway")
        self.assertIn("throwaway.brand_new_tool", str(ctx.exception))

    def test_tool_class_lookup(self):
        self.assertEqual(
            classes.tool_class("campaigns", "pause_campaign"),
            classes.PROTECTIVE,
        )
        with self.assertRaises(classes.UnclassifiedTool):
            classes.tool_class("campaigns", "no_such_tool")

    def test_annotations_for_each_class(self):
        self.assertTrue(classes.annotations_for(classes.READ).read_only_hint)
        self.assertFalse(
            classes.annotations_for(classes.PROTECTIVE).destructive_hint
        )
        self.assertFalse(classes.annotations_for(classes.SPEND).destructive_hint)
        self.assertTrue(
            classes.annotations_for(classes.DESTRUCTIVE).destructive_hint
        )
        with self.assertRaises(ValueError):
            classes.annotations_for("MAYBE")
