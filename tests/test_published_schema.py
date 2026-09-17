"""Regression guard: published tool schemas must not declare defaults.

On 2026-09-16 the Claude desktop app began rejecting omitted arguments whose
published JSON Schema carried a "default" ("expected nonoptional"). Defaults
stay on the Pydantic models, so omitted arguments still resolve server-side.
"""

import unittest

from mcp_dayone.server import GetEntryCountFromDbArgs, ReadRecentEntriesArgs, get_available_tools


class PublishedSchemaTest(unittest.TestCase):
    def test_no_default_keys_published(self) -> None:
        for tool in get_available_tools():
            for name, prop in tool.inputSchema.get("properties", {}).items():
                self.assertNotIn("default", prop, f"{tool.name}.{name} publishes a default")

    def test_models_still_apply_defaults(self) -> None:
        self.assertEqual(GetEntryCountFromDbArgs().journal, "")
        self.assertEqual(ReadRecentEntriesArgs().limit, 10)


if __name__ == "__main__":
    unittest.main()
