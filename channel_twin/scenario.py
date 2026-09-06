"""Validation and role-specific lookup for the v1 static scenario."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import TwinConfigError
from .trace import load_trace


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TwinConfigError(f"{name} must be numeric")
    if value < 0 or (positive and value <= 0):
        raise TwinConfigError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return float(value)


def load_static_scenario(path: str | Path) -> dict[str, Any]:
    scenario_path = Path(path).expanduser().resolve()
    try:
        value = json.loads(scenario_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TwinConfigError(f"Scenario file does not exist: {scenario_path}") from exc
    except json.JSONDecodeError as exc:
        raise TwinConfigError(f"Invalid scenario JSON in {scenario_path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("scenario_id"), str):
        raise TwinConfigError("scenario must contain a string scenario_id")
    if value.get("time_mode") != "realtime":
        raise TwinConfigError("v1 static scenario time_mode must be realtime")
    transport = value.get("transport")
    if not isinstance(transport, dict) or transport.get("protocol") != "tcp":
        raise TwinConfigError("v1 static scenario transport.protocol must be tcp")
    if transport.get("max_active_business_transfers") != 1:
        raise TwinConfigError("v1 static scenario allows exactly one active business transfer")
    topology = value.get("topology")
    if not isinstance(topology, dict) or not isinstance(topology.get("directed_links"), list):
        raise TwinConfigError("scenario.topology.directed_links must be an array")
    links = topology["directed_links"]
    if len(links) != 2:
        raise TwinConfigError("v1 static scenario requires exactly two directed links")
    seen: set[str] = set()
    for index, link in enumerate(links):
        if not isinstance(link, dict):
            raise TwinConfigError(f"directed_links[{index}] must be an object")
        link_id = link.get("id")
        if link_id not in {"uplink", "downlink"} or link_id in seen:
            raise TwinConfigError("directed links must contain one uplink and one downlink")
        seen.add(link_id)
        if link.get("available") is not True:
            raise TwinConfigError("SpaceVerse-compatible static links must be available")
        _number(link.get("rate_bps"), f"{link_id}.rate_bps", positive=True)
        _number(link.get("one_way_delay_ms"), f"{link_id}.one_way_delay_ms")
        _number(link.get("jitter_ms"), f"{link_id}.jitter_ms")
        loss = _number(link.get("loss_pct"), f"{link_id}.loss_pct")
        if loss > 100:
            raise TwinConfigError(f"{link_id}.loss_pct cannot exceed 100")
        queue = link.get("queue_limit_packets")
        if not isinstance(queue, int) or isinstance(queue, bool) or queue <= 0:
            raise TwinConfigError(f"{link_id}.queue_limit_packets must be a positive integer")
    value["scenario_path"] = str(scenario_path)
    return value


def receiver_profile(scenario: dict[str, Any], role: str) -> dict[str, Any]:
    wanted = "downlink" if role == "ground" else "uplink" if role == "satellite" else None
    if wanted is None:
        raise TwinConfigError("receiver role must be ground or satellite")
    return next(link for link in scenario["topology"]["directed_links"] if link["id"] == wanted)


def load_gateway_scenario(path: str | Path) -> dict[str, Any]:
    """Load either the fixed two-link scenario or a canonical dynamic trace."""
    scenario_path = Path(path).expanduser().resolve()
    try:
        value = json.loads(scenario_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TwinConfigError(f"Scenario file does not exist: {scenario_path}") from exc
    except json.JSONDecodeError as exc:
        raise TwinConfigError(f"Invalid scenario JSON in {scenario_path}: {exc}") from exc
    if isinstance(value, dict) and "states" in value:
        try:
            return load_trace(scenario_path)
        except ValueError as exc:
            raise TwinConfigError(str(exc)) from exc
    return load_static_scenario(scenario_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a static receive profile for the Linux setup script")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--receiver-role", choices=("ground", "satellite"), required=True)
    args = parser.parse_args(argv)
    profile = receiver_profile(load_static_scenario(args.scenario), args.receiver_role)
    for key in ("rate_bps", "one_way_delay_ms", "jitter_ms", "loss_pct", "queue_limit_packets"):
        print(profile[key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
