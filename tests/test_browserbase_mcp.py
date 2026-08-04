from __future__ import annotations

import unittest
from unittest import mock

from scripts import browserbase_mcp


class BrowserbaseMcpTests(unittest.TestCase):
    def request(self, name: str, arguments: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    def test_lists_only_four_facade_tools(self) -> None:
        session = mock.Mock()
        result = browserbase_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            session,
        )
        names = [tool["name"] for tool in result["result"]["tools"]]
        self.assertEqual(
            names,
            [
                "cloud_browser_preflight",
                "cloud_browser_start",
                "cloud_browser_call",
                "cloud_browser_end",
            ],
        )

    def test_preflight_does_not_start_session(self) -> None:
        session = mock.Mock()
        session.preflight.return_value = {"status": "configuration_required"}
        result = browserbase_mcp.handle_request(
            self.request("cloud_browser_preflight", {}),
            session,
        )
        self.assertIn("configuration_required", result["result"]["content"][0]["text"])
        session.start.assert_not_called()

    def test_call_forwards_exact_allowlisted_tool(self) -> None:
        session = mock.Mock()
        session.call.return_value = {"result": {"content": []}}
        browserbase_mcp.handle_request(
            self.request(
                "cloud_browser_call",
                {
                    "name": "browser_snapshot",
                    "arguments": {"filename": "snapshot.md"},
                },
            ),
            session,
        )
        session.call.assert_called_once_with(
            "browser_snapshot",
            {"filename": "snapshot.md"},
        )


if __name__ == "__main__":
    unittest.main()
