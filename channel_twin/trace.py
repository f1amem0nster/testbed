"""Canonical dynamic trace schema, validation, and atomic persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class TraceError(ValueError):
    """A canonical trace is invalid or cannot be compiled."""


PROFILE_KEYS = {
    "available",
    "rate_bps",
    "one_way_delay_ms",
    "jitter_ms",
    "loss_pct",
    "queue_limit_packets",
    "provenance",
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TraceError(f"{name} must be numeric")
    result = float(value)
    if result < 0 or (positive and result <= 0):
        raise TraceError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return result


def validate_trace(trace: Any) -> dict[str, Any]:
    if not isinstance(trace, dict):
        raise TraceError("trace must be a JSON object")
    if trace.get("schema_version") != "1.0":
        raise TraceError("schema_version must be '1.0'")
    if not isinstance(trace.get("scenario_id"), str) or not trace["scenario_id"].strip():
        raise TraceError("scenario_id must be a non-empty string")
    if trace.get("time_mode") != "realtime":
        raise TraceError("time_mode must be realtime")
    if not isinstance(trace.get("source"), dict):
        raise TraceError("source must be an object")
    states = trace.get("states")
    if not isinstance(states, list) or not states:
        raise TraceError("states must be a non-empty array")

    expected_offset = 0
    for index, state in enumerate(states):
        if not isinstance(state, dict):
            raise TraceError(f"states[{index}] must be an object")
        if state.get("state_seq") != index:
            raise TraceError(f"states[{index}].state_seq must equal {index}")
        offset = state.get("effective_at_offset_ms")
        duration = state.get("duration_ms")
        if not isinstance(offset, int) or offset != expected_offset:
            raise TraceError(f"states[{index}].effective_at_offset_ms must equal {expected_offset}")
        if not isinstance(duration, int) or duration <= 0:
            raise TraceError(f"states[{index}].duration_ms must be a positive integer")
        links = state.get("links")
        if not isinstance(links, dict) or set(links) != {"uplink", "downlink"}:
            raise TraceError(f"states[{index}].links must contain uplink and downlink")
        for direction, profile in links.items():
            prefix = f"states[{index}].links.{direction}"
            if not isinstance(profile, dict) or not PROFILE_KEYS.issubset(profile):
                raise TraceError(f"{prefix} is missing required profile fields")
            if not isinstance(profile["available"], bool):
                raise TraceError(f"{prefix}.available must be boolean")
            _number(profile["rate_bps"], f"{prefix}.rate_bps", positive=True)
            _number(profile["one_way_delay_ms"], f"{prefix}.one_way_delay_ms")
            _number(profile["jitter_ms"], f"{prefix}.jitter_ms")
            loss = _number(profile["loss_pct"], f"{prefix}.loss_pct")
            if loss > 100:
                raise TraceError(f"{prefix}.loss_pct cannot exceed 100")
            queue = profile["queue_limit_packets"]
            if not isinstance(queue, int) or isinstance(queue, bool) or queue <= 0:
                raise TraceError(f"{prefix}.queue_limit_packets must be a positive integer")
            if not isinstance(profile["provenance"], dict):
                raise TraceError(f"{prefix}.provenance must be an object")
        expected_offset += duration
    trace["duration_ms"] = expected_offset
    return trace


def load_trace(path: str | Path) -> dict[str, Any]:
    trace_path = Path(path).expanduser().resolve()
    try:
        value = json.loads(trace_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TraceError(f"trace does not exist: {trace_path}") from exc
    except json.JSONDecodeError as exc:
        raise TraceError(f"invalid trace JSON in {trace_path}: {exc}") from exc
    trace = validate_trace(value)
    trace["trace_path"] = str(trace_path)
    return trace


def write_trace(path: str | Path, trace: dict[str, Any]) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    validated = validate_trace(trace)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination
