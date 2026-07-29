from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from scripts import lightpanda_mcp


class LightpandaMcpTests(unittest.TestCase):
    def test_tool_list_exposes_only_read_only_facade(self) -> None:
        result = lightpanda_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

        names = [tool["name"] for tool in result["result"]["tools"]]
        self.assertEqual(
            names,
            ["lightpanda_preflight", "lightpanda_public_fetch"],
        )
        forbidden = {
            "click",
            "evaluate",
            "fill",
            "getCookies",
            "press",
            "selectOption",
        }
        self.assertTrue(forbidden.isdisjoint(names))
        for tool in result["result"]["tools"]:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_unknown_native_style_tool_is_rejected(self) -> None:
        result = lightpanda_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "click", "arguments": {}},
            }
        )

        self.assertEqual(result["error"]["code"], -32602)
        self.assertEqual(result["error"]["message"], "unknown tool")

    def test_fetch_response_preserves_untrusted_label(self) -> None:
        payload = {
            "status": "ok",
            "records": [
                {
                    "content": "page",
                    "content_trust": "untrusted_web_content",
                }
            ],
        }
        with mock.patch.object(
            lightpanda_mcp.lightpanda_worker,
            "fetch_public",
            return_value=payload,
        ):
            result = lightpanda_mcp.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "lightpanda_public_fetch",
                        "arguments": {
                            "urls": ["https://example.com"],
                            "format": "markdown",
                            "profile": "thread",
                        },
                    },
                }
            )

        encoded = result["result"]["content"][0]["text"]
        self.assertEqual(json.loads(encoded), payload)

    def test_notification_has_no_response(self) -> None:
        result = lightpanda_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
        )

        self.assertIsNone(result)

    def test_initialize_rejects_non_object_params(self) -> None:
        result = lightpanda_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "initialize",
                "params": [],
            }
        )

        self.assertEqual(result["error"]["code"], -32602)
        self.assertEqual(result["error"]["message"], "invalid params")

    def test_initialize_rejects_non_string_protocol_version(self) -> None:
        result = lightpanda_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "initialize",
                "params": {"protocolVersion": []},
            }
        )

        self.assertEqual(result["error"]["code"], -32602)

    def test_tool_call_rejects_non_object_falsey_arguments(self) -> None:
        result = lightpanda_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "lightpanda_preflight",
                    "arguments": [],
                },
            }
        )

        self.assertEqual(result["error"]["code"], -32602)
        self.assertEqual(
            result["error"]["message"],
            "invalid tool arguments",
        )

    def test_main_rejects_real_command_line_arguments(self) -> None:
        with (
            mock.patch.object(
                lightpanda_mcp.sys,
                "argv",
                ["lightpanda_mcp.py", "--unsafe"],
            ),
            self.assertRaisesRegex(SystemExit, "accepts no command"),
        ):
            lightpanda_mcp.main()

    def test_stdio_server_handles_initialize_and_tool_list(self) -> None:
        source = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                }
            )
            + "\n"
        )
        destination = io.StringIO()

        exit_code = lightpanda_mcp.serve(source, destination)

        rows = [
            json.loads(line)
            for line in destination.getvalue().splitlines()
        ]
        self.assertEqual(exit_code, 0)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[1]["id"], 2)


if __name__ == "__main__":
    unittest.main()
