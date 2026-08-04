#!/usr/bin/env python3
"""Measure the local Codex process envelope before opening Browser tabs."""

from __future__ import annotations

import ctypes
import re
import subprocess
from dataclasses import asdict, dataclass, replace
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
RENDERER_EQUIVALENT_RSS_KIB = 256 * 1024
HELPER_EQUIVALENT_RSS_KIB = 64 * 1024


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
    node_repl_rss_mb: float = 0.0
    mcp_process_rss_mb: float = 0.0
    renderer_process_count: int = 0
    renderer_rss_mb: float = 0.0
    node_repl_process_count: int = 0
    mcp_raw_process_count: int = 0


@dataclass
class ProcessTotals:
    codex_rss_kib: int = 0
    renderer_process_count: int = 0
    renderer_rss_kib: int = 0
    node_repl_process_count: int = 0
    node_repl_rss_kib: int = 0
    mcp_raw_process_count: int = 0
    mcp_process_rss_kib: int = 0

    def add(self, rss_kib: int, command: str) -> None:
        is_codex = any(marker in command for marker in CODEX_MARKERS)
        if is_codex:
            self.codex_rss_kib += rss_kib
        if is_codex and "(Renderer)" in command:
            self.renderer_process_count += 1
            self.renderer_rss_kib += rss_kib
        if "node_repl" in command:
            self.node_repl_process_count += 1
            self.node_repl_rss_kib += rss_kib
        lowered = command.lower()
        if "mcp" in lowered and any(
            marker in lowered for marker in ("python", "node", "uv")
        ):
            self.mcp_raw_process_count += 1
            self.mcp_process_rss_kib += rss_kib

    def sample(self) -> ResourceSample:
        return ResourceSample(
            codex_rss_mb=round(self.codex_rss_kib / 1024, 1),
            renderer_count=_equivalent_count(
                self.renderer_rss_kib,
                self.renderer_process_count,
                RENDERER_EQUIVALENT_RSS_KIB,
            ),
            node_repl_count=_equivalent_count(
                self.node_repl_rss_kib,
                self.node_repl_process_count,
                HELPER_EQUIVALENT_RSS_KIB,
            ),
            mcp_process_count=_equivalent_count(
                self.mcp_process_rss_kib,
                self.mcp_raw_process_count,
                HELPER_EQUIVALENT_RSS_KIB,
            ),
            free_percent=None,
            node_repl_rss_mb=round(self.node_repl_rss_kib / 1024, 1),
            mcp_process_rss_mb=round(self.mcp_process_rss_kib / 1024, 1),
            renderer_process_count=self.renderer_process_count,
            renderer_rss_mb=round(self.renderer_rss_kib / 1024, 1),
            node_repl_process_count=self.node_repl_process_count,
            mcp_raw_process_count=self.mcp_raw_process_count,
        )


def _equivalent_count(
    rss_kib: int,
    process_count: int,
    equivalent_rss_kib: int,
) -> int:
    if process_count == 0:
        return 0
    return (rss_kib + equivalent_rss_kib - 1) // equivalent_rss_kib


def parse_processes(output: str) -> ResourceSample:
    totals = ProcessTotals()
    for raw_line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.+)$", raw_line)
        if match is None:
            continue
        totals.add(int(match.group(1)), match.group(2))
    return totals.sample()


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


def _load_libproc() -> Any:
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
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    return libproc


def _load_libc() -> Any:
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctl.restype = ctypes.c_int
    return libc


def _native_pids(libproc: Any) -> list[int]:
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
    return [
        int(pid)
        for pid in pids[: written // ctypes.sizeof(ctypes.c_int)]
        if pid > 0
    ]


def _native_process_path(libproc: Any, pid: int) -> str | None:
    path_buffer = ctypes.create_string_buffer(4096)
    if libproc.proc_pidpath(pid, path_buffer, len(path_buffer)) <= 0:
        return None
    return path_buffer.value.decode("utf-8", errors="replace")


def _native_process_row(libproc: Any, libc: Any, pid: int) -> str | None:
    path = _native_process_path(libproc, pid)
    if path is None:
        return None
    info = ProcTaskInfo()
    info_size = libproc.proc_pidinfo(
        pid,
        4,
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if info_size != ctypes.sizeof(info):
        return None
    command = _native_command(libc, pid, path)
    return f"{info.resident_size // 1024} {command}"


def collect_native_processes() -> ResourceSample:
    libproc = _load_libproc()
    libc = _load_libc()
    rows: list[str] = []
    for pid in _native_pids(libproc):
        row = _native_process_row(libproc, libc, pid)
        if row is not None:
            rows.append(row)
    return parse_processes("\n".join(rows))


def collect_native_runtime_id() -> str | None:
    libproc = _load_libproc()
    matches: list[str] = []
    for pid in _native_pids(libproc):
        path = _native_process_path(libproc, pid)
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


def _collect_process_sample() -> ResourceSample:
    try:
        process_result = subprocess.run(
            ["ps", "-axo", "rss=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return parse_processes(process_result.stdout)
    except (OSError, subprocess.SubprocessError):
        return collect_native_processes()


def _collect_free_percent() -> int | None:
    try:
        memory_result = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return parse_free_percent(memory_result.stdout)
    except (OSError, subprocess.SubprocessError):
        return None


def _collect_optional_metrics() -> tuple[
    int | None,
    bool | None,
    float | None,
]:
    values: dict[str, Any] = {"idle": None, "power": None, "swap": None}
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
        values[metric] = value
    return values["idle"], values["power"], values["swap"]


def _merge_resource_sample(
    sample: ResourceSample,
    *,
    free_percent: int | None,
    idle_seconds: int | None,
    on_ac_power: bool | None,
    swap_used_mb: float | None,
) -> ResourceSample:
    return replace(
        sample,
        free_percent=free_percent,
        user_idle_seconds=idle_seconds,
        on_ac_power=on_ac_power,
        swap_used_mb=swap_used_mb,
    )


def collect() -> ResourceSample:
    sample = _collect_process_sample()
    free_percent = _collect_free_percent()
    idle_seconds, on_ac_power, swap_used_mb = _collect_optional_metrics()
    return _merge_resource_sample(
        sample,
        free_percent=free_percent,
        idle_seconds=idle_seconds,
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
    swap_blocks_dispatch = bool(
        config.get("memory_guard_swap_blocks_dispatch", False)
    )
    historical_swap = swap_is_historical(sample, config)
    if (
        (
            sample.free_percent is not None
            and sample.free_percent < pressure_free
        )
        or sample.codex_rss_mb > pressure_rss
        or (
            swap_blocks_dispatch
            and sample.swap_used_mb is not None
            and sample.swap_used_mb > pressure_swap
            and not historical_swap
        )
    ):
        return "efficiency"
    idle_threshold = int(config["resource_mode_idle_seconds"])
    if (
        sample.user_idle_seconds is not None
        and sample.user_idle_seconds >= idle_threshold
        and sample.on_ac_power is True
        and (
            sample.free_percent is None
            or sample.free_percent
            >= int(
                config.get(
                    "resource_mode_performance_min_free_percent",
                    35,
                )
            )
        )
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


def swap_is_historical(
    sample: ResourceSample,
    config: dict[str, Any],
) -> bool:
    return (
        sample.free_percent is not None
        and sample.free_percent
        >= int(config.get("memory_guard_swap_recovery_free_percent", 25))
        and sample.codex_rss_mb
        <= float(config.get("memory_guard_swap_recovery_codex_rss_mb", 2000))
    )


def helper_envelope_is_healthy(
    sample: ResourceSample,
    config: dict[str, Any],
) -> bool:
    return (
        sample.free_percent is not None
        and sample.free_percent
        >= int(config.get("memory_guard_helper_recovery_free_percent", 25))
        and sample.codex_rss_mb
        <= float(
            config.get("memory_guard_helper_recovery_codex_rss_mb", 2000)
        )
        and (
            sample.node_repl_rss_mb + sample.mcp_process_rss_mb
            <= float(
                config.get(
                    "memory_guard_helper_recovery_total_rss_mb",
                    512,
                )
            )
        )
    )


def high_free_dispatch_envelope_is_healthy(
    sample: ResourceSample,
    config: dict[str, Any],
) -> bool:
    return (
        sample.free_percent is not None
        and sample.free_percent
        >= int(config.get("memory_guard_high_free_recovery_percent", 35))
        and sample.codex_rss_mb
        <= float(
            config.get(
                "memory_guard_high_free_recovery_codex_rss_mb",
                2700,
            )
        )
        and sample.renderer_count
        <= int(
            config.get(
                "memory_guard_high_free_recovery_renderer_count",
                6,
            )
        )
        and (
            sample.node_repl_rss_mb + sample.mcp_process_rss_mb
            <= float(
                config.get(
                    "memory_guard_high_free_recovery_helper_rss_mb",
                    512,
                )
            )
        )
    )


def assess(
    sample: ResourceSample,
    config: dict[str, Any],
    *,
    mode: str | None = None,
) -> list[str]:
    limits_config = profile_limits(config, mode) if mode else config
    reasons = _assess_limits(sample, limits_config, config)
    hard_caps = config.get("memory_guard_hard_caps")
    if isinstance(hard_caps, dict) and hard_caps is not limits_config:
        for reason in _assess_limits(sample, hard_caps, config):
            hard_reason = "hard_cap: " + reason
            if hard_reason not in reasons:
                reasons.append(hard_reason)
    return reasons


def _assess_limits(
    sample: ResourceSample,
    limits_config: dict[str, Any],
    policy_config: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    historical_swap = swap_is_historical(sample, policy_config)
    helpers_recovered = helper_envelope_is_healthy(
        sample,
        policy_config,
    )
    high_free_recovered = high_free_dispatch_envelope_is_healthy(
        sample,
        policy_config,
    )
    limits = (
        ("codex_rss_mb", sample.codex_rss_mb, float),
        ("renderer_count", sample.renderer_count, int),
        ("node_repl_count", sample.node_repl_count, int),
        ("mcp_process_count", sample.mcp_process_count, int),
    )
    for name, value, converter in limits:
        maximum = converter(limits_config[f"memory_guard_max_{name}"])
        if name == "renderer_count" and high_free_recovered:
            continue
        if (
            name in {"node_repl_count", "mcp_process_count"}
            and (helpers_recovered or high_free_recovered)
        ):
            continue
        if name == "codex_rss_mb" and high_free_recovered:
            continue
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
        if (
            bool(
                policy_config.get(
                    "memory_guard_swap_blocks_dispatch",
                    False,
                )
            )
            and sample.swap_used_mb > maximum_swap
            and not historical_swap
            and not high_free_recovered
        ):
            reasons.append(
                f"swap_used_mb={sample.swap_used_mb} exceeds {maximum_swap}"
            )
    return reasons


def _guard_result(
    sample: ResourceSample,
    config: dict[str, Any],
    *,
    memory_enabled: bool,
) -> dict[str, Any]:
    mode = select_mode(sample, config)
    reasons = assess(sample, config, mode=mode) if memory_enabled else []
    result: dict[str, Any] = {
        "enabled": memory_enabled,
        "available": True,
        "defer": bool(reasons),
        "reasons": reasons,
        "mode": mode,
        "swap_blocks_dispatch": bool(
            config.get("memory_guard_swap_blocks_dispatch", False)
        ),
        "sample": asdict(sample),
        "checked_at": autopilot_dispatch.isoformat(),
    }
    return result


def check(config_path: Path) -> dict[str, Any]:
    config = autopilot_dispatch.read_json(config_path)
    memory_enabled = bool(config.get("memory_guard_enabled", False))
    if not memory_enabled:
        return {
            "enabled": False,
            "defer": False,
            "reasons": [],
        }
    state_value = str(
        config.get("memory_guard_state_file", "var/resource-health.json")
    )
    state_path = autopilot_dispatch.resolve_path(config_path, state_value)
    try:
        sample = collect()
        result = _guard_result(
            sample,
            config,
            memory_enabled=memory_enabled,
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        result = {
            "enabled": True,
            "available": False,
            "defer": False,
            "reasons": [],
            "error": str(error),
            "checked_at": autopilot_dispatch.isoformat(),
        }
    autopilot_dispatch.atomic_write_json(state_path, result)
    return result
