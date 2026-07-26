#!/usr/bin/env python3
"""Measure the local Codex process envelope before opening Browser tabs."""

from __future__ import annotations

import ctypes
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
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
    voice_active: bool = False
    voice_input_pids: tuple[int, ...] = ()
    node_repl_rss_mb: float = 0.0
    mcp_process_rss_mb: float = 0.0


def parse_processes(output: str) -> ResourceSample:
    codex_rss_kib = 0
    renderer_count = 0
    node_repl_count = 0
    mcp_process_count = 0
    node_repl_rss_kib = 0
    mcp_process_rss_kib = 0
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
            node_repl_rss_kib += rss_kib
        if "mcp" in command.lower() and (
            "python" in command.lower()
            or "node" in command.lower()
            or "uv" in command.lower()
        ):
            mcp_process_count += 1
            mcp_process_rss_kib += rss_kib
    return ResourceSample(
        codex_rss_mb=round(codex_rss_kib / 1024, 1),
        renderer_count=renderer_count,
        node_repl_count=node_repl_count,
        mcp_process_count=mcp_process_count,
        free_percent=None,
        node_repl_rss_mb=round(node_repl_rss_kib / 1024, 1),
        mcp_process_rss_mb=round(mcp_process_rss_kib / 1024, 1),
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


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


def fourcc(value: str) -> int:
    if len(value) != 4:
        raise ValueError("fourcc value must contain exactly four characters")
    return int.from_bytes(value.encode("ascii"), byteorder="big")


def active_audio_input_pids() -> tuple[int, ...]:
    core_audio = ctypes.CDLL(
        "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
    )
    core_audio.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    core_audio.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    core_audio.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    core_audio.AudioObjectGetPropertyData.restype = ctypes.c_int32
    process_list = AudioObjectPropertyAddress(
        fourcc("prs#"),
        fourcc("glob"),
        0,
    )
    byte_count = ctypes.c_uint32()
    status = core_audio.AudioObjectGetPropertyDataSize(
        1,
        ctypes.byref(process_list),
        0,
        None,
        ctypes.byref(byte_count),
    )
    if status != 0 or byte_count.value == 0:
        return ()
    object_count = byte_count.value // ctypes.sizeof(ctypes.c_uint32)
    objects = (ctypes.c_uint32 * object_count)()
    status = core_audio.AudioObjectGetPropertyData(
        1,
        ctypes.byref(process_list),
        0,
        None,
        ctypes.byref(byte_count),
        objects,
    )
    if status != 0:
        return ()

    active: list[int] = []
    for object_id in objects:
        running_address = AudioObjectPropertyAddress(
            fourcc("piri"),
            fourcc("glob"),
            0,
        )
        running = ctypes.c_uint32()
        running_size = ctypes.c_uint32(ctypes.sizeof(running))
        if (
            core_audio.AudioObjectGetPropertyData(
                object_id,
                ctypes.byref(running_address),
                0,
                None,
                ctypes.byref(running_size),
                ctypes.byref(running),
            )
            != 0
            or running.value == 0
        ):
            continue
        pid_address = AudioObjectPropertyAddress(
            fourcc("ppid"),
            fourcc("glob"),
            0,
        )
        pid = ctypes.c_int32()
        pid_size = ctypes.c_uint32(ctypes.sizeof(pid))
        if (
            core_audio.AudioObjectGetPropertyData(
                object_id,
                ctypes.byref(pid_address),
                0,
                None,
                ctypes.byref(pid_size),
                ctypes.byref(pid),
            )
            == 0
            and pid.value > 0
        ):
            active.append(pid.value)
    return tuple(sorted(set(active)))


def native_process_command(pid: int) -> str:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    path_buffer = ctypes.create_string_buffer(4096)
    if libproc.proc_pidpath(pid, path_buffer, len(path_buffer)) <= 0:
        return ""
    path = path_buffer.value.decode("utf-8", errors="replace")
    return _native_command(libc, pid, path)


def is_codex_audio_command(command: str) -> bool:
    return (
        "audio.mojom.AudioService" in command
        and any(marker in command for marker in CODEX_MARKERS)
    )


def detect_realtime_voice() -> tuple[bool, tuple[int, ...]]:
    try:
        matching = tuple(
            pid
            for pid in active_audio_input_pids()
            if is_codex_audio_command(native_process_command(pid))
        )
    except (OSError, ValueError):
        matching = ()
    return bool(matching), matching


def apply_voice_hold(
    sample: ResourceSample,
    previous: dict[str, Any],
    *,
    now: datetime,
    hold_seconds: int,
) -> tuple[ResourceSample, str | None, bool]:
    observed = sample.voice_active
    if observed:
        until = now + timedelta(seconds=hold_seconds)
        return sample, autopilot_dispatch.isoformat(until), True
    previous_until = autopilot_dispatch.parse_time(
        previous.get("voice_priority_until")
    )
    if previous_until is not None and previous_until > now:
        return (
            replace(sample, voice_active=True),
            autopilot_dispatch.isoformat(previous_until),
            False,
        )
    return sample, None, False


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
    voice_active, voice_input_pids = detect_realtime_voice()
    return ResourceSample(
        codex_rss_mb=sample.codex_rss_mb,
        renderer_count=sample.renderer_count,
        node_repl_count=sample.node_repl_count,
        mcp_process_count=sample.mcp_process_count,
        free_percent=free_percent,
        user_idle_seconds=idle_seconds,
        on_ac_power=on_ac_power,
        swap_used_mb=swap_used_mb,
        voice_active=voice_active,
        voice_input_pids=voice_input_pids,
        node_repl_rss_mb=sample.node_repl_rss_mb,
        mcp_process_rss_mb=sample.mcp_process_rss_mb,
    )


def select_mode(sample: ResourceSample, config: dict[str, Any]) -> str:
    if not isinstance(config.get("memory_guard_profiles"), dict):
        return "balanced"
    configured = str(config.get("resource_mode", "auto"))
    if configured in {"efficiency", "balanced", "performance"}:
        return configured
    if configured != "auto":
        raise ValueError(f"Unsupported resource_mode: {configured}")
    if sample.voice_active:
        return "efficiency"
    pressure_free = int(config["resource_mode_pressure_free_percent"])
    pressure_rss = float(config["resource_mode_pressure_codex_rss_mb"])
    pressure_swap = float(config["resource_mode_pressure_swap_used_mb"])
    historical_swap = swap_is_historical(sample, config)
    if (
        (
            sample.free_percent is not None
            and sample.free_percent < pressure_free
        )
        or sample.codex_rss_mb > pressure_rss
        or (
            sample.swap_used_mb is not None
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
            config.get("memory_guard_helper_recovery_codex_rss_mb", 1800)
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


def assess(
    sample: ResourceSample,
    config: dict[str, Any],
    *,
    mode: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    limits_config = profile_limits(config, mode) if mode else config
    historical_swap = swap_is_historical(sample, config)
    helpers_recovered = helper_envelope_is_healthy(sample, config)
    limits = (
        ("codex_rss_mb", sample.codex_rss_mb, float),
        ("renderer_count", sample.renderer_count, int),
        ("node_repl_count", sample.node_repl_count, int),
        ("mcp_process_count", sample.mcp_process_count, int),
    )
    for name, value, converter in limits:
        maximum = converter(limits_config[f"memory_guard_max_{name}"])
        if (
            name in {"node_repl_count", "mcp_process_count"}
            and helpers_recovered
        ):
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
        if sample.swap_used_mb > maximum_swap and not historical_swap:
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
    memory_enabled = bool(config.get("memory_guard_enabled", False))
    voice_priority_enabled = bool(
        config.get("voice_priority_enabled", False)
    )
    if not memory_enabled and not voice_priority_enabled:
        return {
            "enabled": False,
            "voice_priority_enabled": False,
            "defer": False,
            "reasons": [],
        }
    state_value = str(
        config.get("memory_guard_state_file", "var/resource-health.json")
    )
    state_path = autopilot_dispatch.resolve_path(config_path, state_value)
    previous: dict[str, Any] = {}
    if state_path.exists():
        try:
            previous = autopilot_dispatch.read_json(state_path)
        except (OSError, ValueError):
            previous = {}
    try:
        sample = collect()
        voice_priority_until: str | None = None
        voice_observed = sample.voice_active
        if voice_priority_enabled:
            hold_seconds = int(
                config.get("voice_priority_hold_seconds", 300)
            )
            if hold_seconds < 0:
                raise ValueError(
                    "voice_priority_hold_seconds must not be negative"
                )
            sample, voice_priority_until, voice_observed = apply_voice_hold(
                sample,
                previous,
                now=datetime.now(timezone.utc),
                hold_seconds=hold_seconds,
            )
        mode = select_mode(sample, config)
        reasons = assess(sample, config, mode=mode) if memory_enabled else []
        if voice_priority_enabled and sample.voice_active:
            reasons.insert(
                0,
                "voice_active=true reserves resources for realtime conversation",
            )
        result: dict[str, Any] = {
            "enabled": memory_enabled,
            "voice_priority_enabled": voice_priority_enabled,
            "available": True,
            "defer": bool(reasons),
            "reasons": reasons,
            "mode": mode,
            "sample": asdict(sample),
            "checked_at": autopilot_dispatch.isoformat(),
        }
        if voice_priority_enabled:
            result["voice_observed"] = voice_observed
            result["voice_priority_until"] = voice_priority_until
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
