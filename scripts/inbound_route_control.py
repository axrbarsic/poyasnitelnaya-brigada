#!/usr/bin/env python3
"""Durable temporary routing policy for queued inbound X events."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    from scripts import json_contract
except ModuleNotFoundError:
    import json_contract  # type: ignore[no-redef]


STATE_VERSION = 2
NORMAL_MODE = "normal"
SIMPLE_WAVE_MODE = "simple-wave"
SUPPORTED_MODES = frozenset({NORMAL_MODE, SIMPLE_WAVE_MODE})
SUPPORTED_ROUTES = frozenset(
    {
        "short",
        "local-max",
        "requested-media",
        "satirical-media",
        "already-answered",
    }
)


def resolve_path(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (config_path.resolve().parent / candidate).resolve()


def state_path(config_path: Path, config: dict[str, Any]) -> Path:
    return resolve_path(
        config_path,
        str(
            config.get(
                "inbound_route_control_file",
                "var/inbound-route-control.json",
            )
        ),
    )


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "mode": NORMAL_MODE,
        "routes": {},
    }


def _event_id(value: Any) -> str:
    event_id = str(value).strip()
    if not event_id.isdigit() or len(event_id) > 19:
        raise ValueError("event id must be numeric")
    return event_id


def _route(value: Any) -> str:
    route = str(value).strip()
    if route not in SUPPORTED_ROUTES:
        raise ValueError("unsupported inbound response route")
    return route


def _mode(value: Any) -> str:
    mode = str(value).strip()
    if mode not in SUPPORTED_MODES:
        raise ValueError("unsupported inbound route mode")
    return mode


def validate_state(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("version") != STATE_VERSION:
        raise ValueError("unsupported inbound route control version")
    mode = _mode(payload.get("mode"))
    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, dict):
        raise ValueError("inbound route control routes must be an object")
    routes: dict[str, dict[str, Any]] = {}
    for raw_event_id, raw_record in raw_routes.items():
        event_id = _event_id(raw_event_id)
        if not isinstance(raw_record, dict):
            raise ValueError("inbound route record must be an object")
        route = _route(raw_record.get("route"))
        classified_at = str(raw_record.get("classified_at", "")).strip()
        if not classified_at:
            raise ValueError("inbound route record misses classified_at")
        routes[event_id] = {
            "route": route,
            "classified_at": classified_at,
        }
    result = {
        "version": STATE_VERSION,
        "mode": mode,
        "routes": routes,
    }
    raw_wave_ids = payload.get("simple_wave_event_ids")
    if mode == SIMPLE_WAVE_MODE:
        if not isinstance(raw_wave_ids, list):
            raise ValueError("simple wave event IDs must be an array")
        wave_ids = [_event_id(value) for value in raw_wave_ids]
        if len(wave_ids) != len(set(wave_ids)):
            raise ValueError("simple wave event IDs must be unique")
        result["simple_wave_event_ids"] = wave_ids
    elif raw_wave_ids not in (None, []):
        raise ValueError("normal mode must not retain simple wave event IDs")
    for key in ("updated_at", "updated_by"):
        value = payload.get(key)
        if value is not None:
            result[key] = str(value)
    return result


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    return validate_state(json_contract.read_object(path))


def write(path: Path, state: dict[str, Any]) -> None:
    json_contract.atomic_write(path, validate_state(state))


@contextmanager
def locked_state(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def selection_sets(
    state: dict[str, Any],
    pending_event_ids: Iterable[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Return one-wave ordinary priority IDs and temporarily excluded IDs."""

    pending = {_event_id(value) for value in pending_event_ids}
    if state["mode"] == NORMAL_MODE:
        return frozenset(), frozenset()
    routes = state["routes"]
    wave = set(state["simple_wave_event_ids"])
    priority = {
        event_id
        for event_id, record in routes.items()
        if event_id in pending
        and event_id in wave
        and record["route"] != "local-max"
    }
    excluded = pending - wave
    excluded.update(
        event_id
        for event_id, record in routes.items()
        if event_id in pending
        and event_id in wave
        and record["route"] == "local-max"
    )
    return frozenset(priority), frozenset(excluded)


def set_mode(
    path: Path,
    *,
    mode: str,
    updated_at: str,
    updated_by: str,
    pending_event_ids: Iterable[str] = (),
) -> dict[str, Any]:
    with locked_state(path):
        state = load(path)
        selected_mode = _mode(mode)
        state["mode"] = selected_mode
        if selected_mode == SIMPLE_WAVE_MODE:
            state["simple_wave_event_ids"] = sorted(
                {_event_id(value) for value in pending_event_ids},
                key=int,
            )
        else:
            state.pop("simple_wave_event_ids", None)
        state["updated_at"] = updated_at
        state["updated_by"] = updated_by
        write(path, state)
        return state


def reconcile_wave(
    path: Path,
    *,
    pending_event_ids: Iterable[str],
    updated_at: str,
) -> dict[str, Any]:
    """Prune finished/scanned wave events and resume normal FIFO once empty."""

    with locked_state(path):
        state = load(path)
        pending = {_event_id(value) for value in pending_event_ids}
        live_routes = {
            event_id: record
            for event_id, record in state["routes"].items()
            if event_id in pending
        }
        routes_changed = live_routes != state["routes"]
        state["routes"] = live_routes
        if state["mode"] != SIMPLE_WAVE_MODE:
            if routes_changed:
                state["updated_at"] = updated_at
                write(path, state)
            return state
        routes = state["routes"]
        remaining = [
            event_id
            for event_id in state["simple_wave_event_ids"]
            if event_id in pending
            and routes.get(event_id, {}).get("route") != "local-max"
        ]
        if remaining:
            if remaining != state["simple_wave_event_ids"] or routes_changed:
                state["simple_wave_event_ids"] = remaining
                state["updated_at"] = updated_at
                write(path, state)
            return state
        state["mode"] = NORMAL_MODE
        state.pop("simple_wave_event_ids", None)
        state["updated_at"] = updated_at
        state["updated_by"] = "automatic-simple-wave-completion"
        write(path, state)
        return state


def record_route(
    path: Path,
    *,
    event_id: str,
    route: str,
    classified_at: str,
) -> dict[str, Any]:
    with locked_state(path):
        state = load(path)
        event_id = _event_id(event_id)
        route = _route(route)
        previous = state["routes"].get(event_id)
        if previous is not None and previous["route"] != route:
            raise ValueError("event already has a different live route")
        state["routes"][event_id] = {
            "route": route,
            "classified_at": classified_at,
        }
        state["updated_at"] = classified_at
        write(path, state)
        return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    mode = commands.add_parser("set-mode")
    mode.add_argument("mode", choices=sorted(SUPPORTED_MODES))
    mode.add_argument("--updated-at", required=True)
    mode.add_argument("--updated-by", default="Alex")
    mode.add_argument("--event-id", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "status":
        result = load(arguments.state)
    else:
        result = set_mode(
            arguments.state,
            mode=arguments.mode,
            updated_at=arguments.updated_at,
            updated_by=arguments.updated_by,
            pending_event_ids=arguments.event_id,
        )
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
