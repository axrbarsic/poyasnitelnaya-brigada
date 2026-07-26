#!/usr/bin/env python3
"""Measure the local Codex process envelope before opening Browser tabs."""

from __future__ import annotations

import ctypes
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from scripts import autopilot_dispatch
except ModuleNotFoundError:
    import autopilot_dispatch  # type: ignore[no-redef]


FREE_PERCENT = re.compile(r"System-wide memory free percentage:\s*(\d+)%")
IDLE_NANOSECONDS = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
SWAP_USED = re.compile(r"used\s*=\s*([0-9.]+)([MG])")
CODEX_MARKERS = (
    "/Applications/Codex.app/Contents/",
    "/Applications/ChatGPT.app/Contents/",
)
CODEX_MAIN_EXECUTABLES = (
    "/Applications/Codex.app/Contents/MacOS/Codex",
    "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
)


@dataclass(frozen=True)
class ResourceSample:
    codex_rss_mb: float
    renderer_count: int
    node_repl_count: int
    mcp_process_count: int
    free_percent: int | None
    user_idle_seconds: int | None = None
    on_ac_power: bool | None = None
    swap_used_mb: float | None = None


def parse_processes(output: str) -> ResourceSample:
    codex_rss_kib = 0
    renderer_count = 0
    node_repl_count = 0
    mcp_process_count = 0
    for raw_line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.+)$", raw_line)
        if match is None:
            continue
        rss_kib = int(match.group(1))
        command = match.group(2)
        if any(marker in command for marker in CODEX_MARKERS):
            codex_rss_kib += rss_kib
        if "(Renderer)" in command and any(
            marker in command for marker in CODEX_MARKERS
        ):
            renderer_count += 1
        if "node_repl" in command:
            node_repl_count += 1
        if "mcp" in command.lower() and (
            "python" in command.lower()
            or "node" in command.lower()
            or "uv" in command.lower()
        ):
            mcp_process_count += 1
    return ResourceSample(
        codex_rss_mb=round(codex_rss_kib / 1024, 1),
        renderer_count=renderer_count,
        node_repl_count=node_repl_count,
        mcp_process_count=mcp_process_count,
        free_percent=None,
    )


def parse_runtime_id(output: str) -> str | None:
    matches: list[str] = []
    for raw_line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.+)$", raw_line)
        if match is None:
            continue
        command = match.group(2).split(" ", 1)[0]
        if command in CODEX_MAIN_EXECUTABLES:
            matches.append(f"{command}:{match.group(1)}")
    return "|".join(sorted(matches)) if matches else None


def parse_free_percent(output: str) -> int | None:
    match = FREE_PERCENT.search(output)
    return int(match.group(1)) if match else None


def parse_idle_seconds(output: str) -> int | None:
    match = IDLE_NANOSECONDS.search(output)
    return int(match.group(1)) // 1_000_000_000 if match else None


def parse_on_ac_power(output: str) -> bool | None:
    if "AC Power" in output:
        return True
    if "Battery Power" in output:
        return False
    return None


def parse_swap_used_mb(output: str) -> float | None:
    match = SWAP_USED.search(output)
    if match is None:
        return None
    value = float(match.group(1))
    return round(value * 1024 if match.group(2) == "G" else value, 2)


class ProcTaskInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("csw", ctypes.c_int32),
        ("threadnum", ctypes.c_int32),
        ("numrunning", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]


def _native_command(libc: Any, pid: int, path: str) -> str:
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t(262144)
    buffer = ctypes.create_string_buffer(size.value)
    result = libc.sysctl(
        mib,
        3,
        buffer,
        ctypes.byref(size),
        None,
        0,
    )
    if result != 0 or size.value <= ctypes.sizeof(ctypes.c_int):
        return path
    payload = buffer.raw[: size.value]
    arguments = [
        part.decode("utf-8", errors="replace")
        for part in payload[ctypes.sizeof(ctypes.c_int) :].split(b"\0")
        if part
    ]
    return " ".join(arguments) if arguments else path


def collect_native_processes() -> ResourceSample:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libproc.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    libproc.proc_pidpath.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctl.restype = ctypes.c_int

    required = libproc.proc_listpids(1, 0, None, 0)
    if required <= 0:
        raise OSError(ctypes.get_errno(), "proc_listpids size failed")
    capacity = max(required // ctypes.sizeof(ctypes.c_int) + 64, 128)
    pids = (ctypes.c_int * capacity)()
    written = libproc.proc_listpids(
        1,
        0,
        pids,
        ctypes.sizeof(pids),
    )
    if written <= 0:
        raise OSError(ctypes.get_errno(), "proc_listpids failed")

    rows: list[str] = []
    for pid in pids[: written // ctypes.sizeof(ctypes.c_int)]:
        if pid <= 0:
            continue
        path_buffer = ctypes.create_string_buffer(4096)
        if libproc.proc_pidpath(pid, path_buffer, len(path_buffer)) <= 0:
            continue
        path = path_buffer.value.decode("utf-8", errors="replace")
        info = ProcTaskInfo()
        info_size = libproc.proc_pidinfo(
            pid,
            4,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if info_size != ctypes.sizeof(info):
            continue
        command = _native_command(libc, pid, path)
        rows.append(f"{info.resident_size // 1024} {command}")
    return parse_processes("\n".join(rows))


def collect_native_runtime_id() -> str | None:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    libproc.proc_pidpath.restype = ctypes.c_int
    required = libproc.proc_listpids(1, 0, None, 0)
    if required <= 0:
        raise OSError(ctypes.get_errno(), "proc_listpids size failed")
    capacity = max(required // ctypes.sizeof(ctypes.c_int) + 64, 128)
    pids = (ctypes.c_int * capacity)()
    written = libproc.proc_listpids(
        1,
        0,
        pids,
        ctypes.sizeof(pids),
    )
    if written <= 0:
        raise OSError(ctypes.get_errno(), "proc_listpids failed")
    matches: list[str] = []
    for pid in pids[: written // ctypes.sizeof(ctypes.c_int)]:
        if pid <= 0:
            continue
        path_buffer = ctypes.create_string_buffer(4096)
        if libproc.proc_pidpath(pid, path_buffer, len(path_buffer)) <= 0:
            continue
        path = path_buffer.value.decode("utf-8", errors="replace")
        if path in CODEX_MAIN_EXECUTABLES:
            matches.append(f"{path}:{pid}")
    return "|".join(sorted(matches)) if matches else None


def codex_runtime_id() -> str | None:
    try:
        process_result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        runtime_id = parse_runtime_id(process_result.stdout)
    except (OSError, subprocess.SubprocessError):
        runtime_id = collect_native_runtime_id()
    return runtime_id


def collect() -> ResourceSample:
    try:
        process_result = subprocess.run(
            ["ps", "-axo", "rss=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        sample = parse_processes(process_result.stdout)
    except (OSError, subprocess.SubprocessError):
        sample = collect_native_processes()
    try:
        memory_result = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        free_percent = parse_free_percent(memory_result.stdout)
    except (OSError, subprocess.SubprocessError):
        free_percent = None
    idle_seconds: int | None = None
    on_ac_power: bool | None = None
    swap_used_mb: float | None = None
    commands = (
        (
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
            lambda output: parse_idle_seconds(output),
            "idle",
        ),
        (
            ["/usr/bin/pmset", "-g", "batt"],
            lambda output: parse_on_ac_power(output),
            "power",
        ),
        (
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            lambda output: parse_swap_used_mb(output),
            "swap",
        ),
    )
    for command, parser, metric in commands:
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            value = parser(result.stdout)
        except (OSError, subprocess.SubprocessError):
            value = None
        if metric == "idle":
            idle_seconds = value
        elif metric == "power":
            on_ac_power = value
        else:
            swap_used_mb = value
    return ResourceSample(
        codex_rss_mb=sample.codex_rss_mb,
        renderer_count=sample.renderer_count,
        node_repl_count=sample.node_repl_count,
        mcp_process_count=sample.mcp_process_count,
        free_percent=free_percent,
        user_idle_seconds=idle_seconds,
        on_ac_power=on_ac_power,
        swap_used_mb=swap_used_mb,
    )


def select_mode(sample: ResourceSample, config: dict[str, Any]) -> str:
    if not isinstance(config.get("memory_guard_profiles"), dict):
        return "balanced"
    configured = str(config.get("resource_mode", "auto"))
    if configured in {"efficiency", "balanced", "performance"}:
        return configured
    if configured != "auto":
        raise ValueError(f"Unsupported resource_mode: {configured}")
    pressure_free = int(config["resource_mode_pressure_free_percent"])
    pressure_rss = float(config["resource_mode_pressure_codex_rss_mb"])
    pressure_swap = float(config["resource_mode_pressure_swap_used_mb"])
    if (
        (
            sample.free_percent is not None
            and sample.free_percent < pressure_free
        )
        or sample.codex_rss_mb > pressure_rss
        or (
            sample.swap_used_mb is not None
            and sample.swap_used_mb > pressure_swap
        )
    ):
        return "efficiency"
    idle_threshold = int(config["resource_mode_idle_seconds"])
    if (
        sample.user_idle_seconds is not None
        and sample.user_idle_seconds >= idle_threshold
        and sample.on_ac_power is True
    ):
        return "performance"
    return "balanced"


def profile_limits(config: dict[str, Any], mode: str) -> dict[str, Any]:
    profiles = config.get("memory_guard_profiles")
    if not isinstance(profiles, dict):
        return config
    limits = profiles.get(mode)
    if not isinstance(limits, dict):
        raise ValueError(f"Missing memory guard profile: {mode}")
    return limits


def assess(
    sample: ResourceSample,
    config: dict[str, Any],
    *,
    mode: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    limits_config = profile_limits(config, mode) if mode else config
    limits = (
        ("codex_rss_mb", sample.codex_rss_mb, float),
        ("renderer_count", sample.renderer_count, int),
        ("node_repl_count", sample.node_repl_count, int),
        ("mcp_process_count", sample.mcp_process_count, int),
    )
    for name, value, converter in limits:
        maximum = converter(limits_config[f"memory_guard_max_{name}"])
        if value > maximum:
            reasons.append(f"{name}={value} exceeds {maximum}")
    minimum_free = int(limits_config["memory_guard_min_free_percent"])
    if sample.free_percent is not None and sample.free_percent < minimum_free:
        reasons.append(
            f"free_percent={sample.free_percent} below {minimum_free}"
        )
    if sample.swap_used_mb is not None:
        maximum_swap = float(
            limits_config.get("memory_guard_max_swap_used_mb", float("inf"))
        )
        if sample.swap_used_mb > maximum_swap:
            reasons.append(
                f"swap_used_mb={sample.swap_used_mb} exceeds {maximum_swap}"
            )
    hard_caps = config.get("memory_guard_hard_caps")
    if isinstance(hard_caps, dict) and hard_caps is not limits_config:
        for reason in assess(sample, hard_caps):
            hard_reason = "hard_cap: " + reason
            if hard_reason not in reasons:
                reasons.append(hard_reason)
    return reasons


def check(config_path: Path) -> dict[str, Any]:
    config = autopilot_dispatch.read_json(config_path)
    if not bool(config.get("memory_guard_enabled", False)):
        return {"enabled": False, "defer": False, "reasons": []}
    try:
        sample = collect()
        mode = select_mode(sample, config)
        reasons = assess(sample, config, mode=mode)
        result: dict[str, Any] = {
            "enabled": True,
            "available": True,
            "defer": bool(reasons),
            "reasons": reasons,
            "mode": mode,
            "sample": asdict(sample),
            "checked_at": autopilot_dispatch.isoformat(),
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        result = {
            "enabled": True,
            "available": False,
            "defer": False,
            "reasons": [],
            "error": str(error),
            "checked_at": autopilot_dispatch.isoformat(),
        }
    state_value = str(
        config.get("memory_guard_state_file", "var/resource-health.json")
    )
    state_path = autopilot_dispatch.resolve_path(config_path, state_value)
    autopilot_dispatch.atomic_write_json(state_path, result)
    return result
