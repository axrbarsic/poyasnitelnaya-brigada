#!/usr/bin/env python3
"""Lazy Browserbase session plus a filtered Playwright MCP child process."""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping

try:
    from scripts import keychain_bundle
except ModuleNotFoundError:
    import keychain_bundle  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "var" / "cloud-browser"
DEFAULT_API_BASE = "https://api.browserbase.com/v1"
DEFAULT_MCP_PACKAGE = "@playwright/mcp@0.0.75"
ALLOWED_CHILD_TOOLS = frozenset(
    {
        "browser_click",
        "browser_close",
        "browser_console_messages",
        "browser_evaluate",
        "browser_file_upload",
        "browser_fill_form",
        "browser_hover",
        "browser_navigate",
        "browser_navigate_back",
        "browser_press_key",
        "browser_select_option",
        "browser_snapshot",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_type",
        "browser_wait_for",
    }
)


class CloudBrowserError(RuntimeError):
    """Raised when the cloud browser cannot safely continue."""


@dataclass(frozen=True)
class Settings:
    config_path: Path
    api_base: str
    project_id: str
    context_id: str
    keychain_helper: Path
    keychain_service: str
    keychain_account: str
    region: str
    timeout_seconds: int
    mcp_package: str
    runtime_root: Path


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def load_settings(path: Path = DEFAULT_CONFIG) -> Settings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CloudBrowserError(f"cannot read cloud browser config: {path}") from error
    block = raw.get("cloud_browser")
    if not isinstance(block, dict):
        raise CloudBrowserError("cloud_browser config object is required")
    if str(block.get("provider", "browserbase")) != "browserbase":
        raise CloudBrowserError("cloud_browser.provider must be browserbase")
    base = path.resolve().parent
    timeout_seconds = int(block.get("timeout_seconds", 900))
    if not 60 <= timeout_seconds <= 21600:
        raise CloudBrowserError("cloud browser timeout must be between 60 and 21600")
    return Settings(
        config_path=path.resolve(),
        api_base=str(block.get("api_base", DEFAULT_API_BASE)).rstrip("/"),
        project_id=str(block.get("project_id", "")).strip(),
        context_id=str(block.get("context_id", "")).strip(),
        keychain_helper=_resolve(
            base,
            str(raw.get("keychain_helper", "var/keychain-helper")),
        ),
        keychain_service=str(
            block.get(
                "keychain_service",
                "axrbarsic-x-mention-watcher-browserbase",
            )
        ).strip(),
        keychain_account=str(block.get("keychain_account", "api-key")).strip(),
        region=str(block.get("region", "us-east-1")).strip(),
        timeout_seconds=timeout_seconds,
        mcp_package=str(block.get("mcp_package", DEFAULT_MCP_PACKAGE)).strip(),
        runtime_root=_resolve(
            base,
            str(block.get("runtime_root", "var/cloud-browser")),
        ),
    )


def _environment_or_config(name: str, configured: str) -> str:
    return os.environ.get(name, "").strip() or configured.strip()


def read_api_key(
    settings: Settings,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[str, str]:
    value = os.environ.get("BROWSERBASE_API_KEY", "").strip()
    if value:
        return value, "environment:BROWSERBASE_API_KEY"
    if not settings.keychain_service or not settings.keychain_account:
        raise CloudBrowserError("Browserbase Keychain coordinates are missing")
    try:
        verification = keychain_bundle.verify_bundle(settings.keychain_helper)
    except Exception as error:
        raise CloudBrowserError("cannot verify the Keychain helper") from error
    if not verification.ok:
        raise CloudBrowserError("refusing an unverified Keychain helper")
    try:
        result = run(
            [
                verification.executable_path,
                "get",
                settings.keychain_service,
                settings.keychain_account,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CloudBrowserError("cannot read Browserbase API key") from error
    value = result.stdout.strip()
    if not value:
        raise CloudBrowserError("Browserbase API key is empty")
    return value, "keychain_helper"


def _request_json(
    method: str,
    url: str,
    api_key: str,
    payload: Mapping[str, Any] | None = None,
    *,
    timeout: int = 30,
) -> Any:
    data = None
    headers = {"X-BB-API-Key": api_key}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise CloudBrowserError("Browserbase API request failed") from error
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CloudBrowserError("Browserbase returned invalid JSON") from error


def create_session(settings: Settings, api_key: str) -> dict[str, str]:
    project_id = _environment_or_config(
        "BROWSERBASE_PROJECT_ID",
        settings.project_id,
    )
    context_id = _environment_or_config(
        "BROWSERBASE_CONTEXT_ID",
        settings.context_id,
    )
    if not project_id:
        raise CloudBrowserError("Browserbase project ID is missing")
    if not context_id:
        raise CloudBrowserError("Browserbase context ID is missing")
    payload = {
        "projectId": project_id,
        "browserSettings": {
            "context": {"id": context_id, "persist": True},
            "viewport": {"width": 1440, "height": 1000},
        },
        "timeout": settings.timeout_seconds,
        "keepAlive": False,
        "region": settings.region,
        "userMetadata": {
            "component": "x-mention-watcher",
            "role": "browser-owner",
        },
    }
    response = _request_json(
        "POST",
        f"{settings.api_base}/sessions",
        api_key,
        payload,
    )
    if not isinstance(response, dict):
        raise CloudBrowserError("Browserbase session response must be an object")
    session_id = str(response.get("id", "")).strip()
    connect_url = str(response.get("connectUrl", "")).strip()
    if not session_id or not connect_url:
        raise CloudBrowserError("Browserbase session response is incomplete")
    return {
        "session_id": session_id,
        "connect_url": connect_url,
        "project_id": project_id,
        "context_id": context_id,
    }


def release_session(
    settings: Settings,
    api_key: str,
    session_id: str,
    project_id: str,
) -> None:
    _request_json(
        "POST",
        f"{settings.api_base}/sessions/{session_id}",
        api_key,
        {"status": "REQUEST_RELEASE", "projectId": project_id},
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class PlaywrightChild:
    """One filtered Playwright MCP transport connected to Browserbase CDP."""

    def __init__(self, settings: Settings, connect_url: str) -> None:
        output_dir = settings.runtime_root / "playwright"
        output_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "PLAYWRIGHT_MCP_CDP_ENDPOINT": connect_url,
                "PLAYWRIGHT_MCP_CDP_TIMEOUT": "60000",
                "PLAYWRIGHT_MCP_CODEGEN": "none",
                "PLAYWRIGHT_MCP_HEADLESS": "true",
                "PLAYWRIGHT_MCP_IMAGE_RESPONSES": "omit",
                "PLAYWRIGHT_MCP_OUTPUT_DIR": str(output_dir),
                "PLAYWRIGHT_MCP_OUTPUT_MODE": "file",
                "PLAYWRIGHT_MCP_SAVE_SESSION": "false",
            }
        )
        self._process = subprocess.Popen(
            ["npx", "-y", settings.mcp_package],
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        self._next_id = 1
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _send(self, payload: Mapping[str, Any]) -> None:
        if self._process.stdin is None:
            raise CloudBrowserError("Playwright MCP stdin is unavailable")
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()

    def _receive(self, identifier: int, *, timeout: int = 120) -> dict[str, Any]:
        if self._process.stdout is None:
            raise CloudBrowserError("Playwright MCP stdout is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CloudBrowserError("Playwright MCP response timed out")
                if not selector.select(remaining):
                    raise CloudBrowserError("Playwright MCP response timed out")
                line = self._process.stdout.readline()
                if not line:
                    raise CloudBrowserError("Playwright MCP stopped unexpectedly")
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("id") == identifier:
                    return payload
        finally:
            selector.close()

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        identifier = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": method,
                "params": dict(params),
            }
        )
        return self._receive(identifier)

    def _initialize(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "x-mention-watcher-cloud-browser",
                    "version": "1.0.0",
                },
            },
        )
        if "error" in result:
            raise CloudBrowserError("Playwright MCP initialization failed")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict[str, Any]]:
        response = self._request("tools/list", {})
        tools = response.get("result", {}).get("tools", [])
        if not isinstance(tools, list):
            raise CloudBrowserError("Playwright MCP returned invalid tools")
        return [
            tool
            for tool in tools
            if isinstance(tool, dict) and tool.get("name") in ALLOWED_CHILD_TOOLS
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in ALLOWED_CHILD_TOOLS:
            raise CloudBrowserError(f"Playwright tool is not allowed: {name}")
        return self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
        )

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)


class CloudBrowserSession:
    """Own the lazy cloud session, context lock, child MCP, and release."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._api_key = ""
        self._session: dict[str, str] | None = None
        self._child: PlaywrightChild | None = None
        self._lock: BinaryIO | None = None
        atexit.register(self.close)

    @property
    def active(self) -> bool:
        return self._session is not None and self._child is not None

    def preflight(self) -> dict[str, Any]:
        try:
            _, source = read_api_key(self.settings)
            api_key_available = True
        except CloudBrowserError:
            source = None
            api_key_available = False
        return {
            "status": "ready" if (
                api_key_available
                and bool(_environment_or_config(
                    "BROWSERBASE_PROJECT_ID",
                    self.settings.project_id,
                ))
                and bool(_environment_or_config(
                    "BROWSERBASE_CONTEXT_ID",
                    self.settings.context_id,
                ))
            ) else "configuration_required",
            "provider": "browserbase",
            "api_key_available": api_key_available,
            "api_key_source": source,
            "project_id_configured": bool(_environment_or_config(
                "BROWSERBASE_PROJECT_ID",
                self.settings.project_id,
            )),
            "context_id_configured": bool(_environment_or_config(
                "BROWSERBASE_CONTEXT_ID",
                self.settings.context_id,
            )),
            "mcp_package": self.settings.mcp_package,
            "active": self.active,
        }

    def start(self) -> dict[str, Any]:
        if self.active:
            raise CloudBrowserError("cloud browser session is already active")
        self.settings.runtime_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.settings.runtime_root / "owner.lock"
        self._lock = lock_path.open("a+b")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock.close()
            self._lock = None
            raise CloudBrowserError("another cloud Browser owner is active") from error
        self._api_key, source = read_api_key(self.settings)
        try:
            self._session = create_session(self.settings, self._api_key)
            self._child = PlaywrightChild(
                self.settings,
                self._session["connect_url"],
            )
            tools = self._child.list_tools()
            _atomic_json(
                self.settings.runtime_root / "session.json",
                {
                    "status": "active",
                    "provider": "browserbase",
                    "session_id": self._session["session_id"],
                    "project_id": self._session["project_id"],
                    "context_id": self._session["context_id"],
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "credential_source": source,
                    "allowed_tools": [tool["name"] for tool in tools],
                },
            )
            return {
                "status": "started",
                "provider": "browserbase",
                "session_id": self._session["session_id"],
                "allowed_tools": tools,
            }
        except Exception:
            self.close()
            raise

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not self.active or self._child is None:
            raise CloudBrowserError("cloud browser session is not active")
        return self._child.call(name, arguments)

    def close(self) -> dict[str, Any]:
        session = self._session
        child = self._child
        lock = self._lock
        if session is None and child is None and lock is None:
            return {"status": "idle", "provider": "browserbase"}
        self._session = None
        self._child = None
        errors: list[str] = []
        if child is not None:
            try:
                child.close()
            except Exception as error:
                errors.append(type(error).__name__)
        if session is not None and self._api_key:
            try:
                release_session(
                    self.settings,
                    self._api_key,
                    session["session_id"],
                    session["project_id"],
                )
            except Exception as error:
                errors.append(type(error).__name__)
        self._api_key = ""
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()
                self._lock = None
        state = {
            "status": "closed_with_warning" if errors else "closed",
            "provider": "browserbase",
            "session_id": session["session_id"] if session else None,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "errors": errors,
        }
        if self.settings.runtime_root.exists():
            _atomic_json(self.settings.runtime_root / "session.json", state)
        return state


def install_signal_cleanup(session: CloudBrowserSession) -> None:
    def stop(_signum: int, _frame: Any) -> None:
        session.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
