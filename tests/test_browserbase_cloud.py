from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import browserbase_cloud


class BrowserbaseCloudTests(unittest.TestCase):
    def write_config(self, root: Path) -> Path:
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "keychain_helper": "helper",
                    "cloud_browser": {
                        "provider": "browserbase",
                        "project_id": "project",
                        "context_id": "context",
                        "keychain_service": "service",
                        "keychain_account": "account",
                        "runtime_root": "runtime",
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_load_settings_is_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = browserbase_cloud.load_settings(self.write_config(root))
            self.assertEqual(settings.project_id, "project")
            self.assertEqual(settings.context_id, "context")
            self.assertEqual(settings.runtime_root, (root / "runtime").resolve())

    def test_read_api_key_prefers_environment(self) -> None:
        settings = browserbase_cloud.Settings(
            config_path=Path("config.json"),
            api_base=browserbase_cloud.DEFAULT_API_BASE,
            project_id="project",
            context_id="context",
            keychain_helper=Path("helper"),
            keychain_service="service",
            keychain_account="account",
            region="us-east-1",
            timeout_seconds=900,
            mcp_package=browserbase_cloud.DEFAULT_MCP_PACKAGE,
            runtime_root=Path("runtime"),
        )
        with mock.patch.dict(
            "os.environ",
            {"BROWSERBASE_API_KEY": "secret"},
            clear=False,
        ):
            value, source = browserbase_cloud.read_api_key(settings)
        self.assertEqual(value, "secret")
        self.assertEqual(source, "environment:BROWSERBASE_API_KEY")

    @mock.patch("scripts.browserbase_cloud.keychain_bundle.verify_bundle")
    def test_read_api_key_uses_verified_helper(self, verify: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = browserbase_cloud.load_settings(
                self.write_config(Path(directory))
            )
            verify.return_value = SimpleNamespace(
                ok=True,
                executable_path="/verified/helper",
            )
            run = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout="secret\n",
                    stderr="",
                )
            )
            with mock.patch.dict("os.environ", {}, clear=True):
                value, source = browserbase_cloud.read_api_key(
                    settings,
                    run=run,
                )
            self.assertEqual(value, "secret")
            self.assertEqual(source, "keychain_helper")
            self.assertEqual(run.call_args.args[0][0], "/verified/helper")

    @mock.patch("scripts.browserbase_cloud._request_json")
    def test_create_session_uses_persistent_context(self, request: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = browserbase_cloud.load_settings(
                self.write_config(Path(directory))
            )
            request.return_value = {"id": "session", "connectUrl": "wss://cdp"}
            result = browserbase_cloud.create_session(settings, "secret")
            payload = request.call_args.args[3]
            self.assertEqual(result["session_id"], "session")
            self.assertEqual(
                payload["browserSettings"]["context"],
                {"id": "context", "persist": True},
            )
            self.assertFalse(payload["keepAlive"])

    def test_child_tool_allowlist_excludes_unsafe_code(self) -> None:
        self.assertNotIn(
            "browser_run_code_unsafe",
            browserbase_cloud.ALLOWED_CHILD_TOOLS,
        )

    def test_idle_close_does_not_create_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = browserbase_cloud.load_settings(self.write_config(root))
            session = browserbase_cloud.CloudBrowserSession(settings)
            self.assertEqual(session.close()["status"], "idle")
            self.assertFalse((root / "runtime" / "session.json").exists())


if __name__ == "__main__":
    unittest.main()
