"""Real-time replay of one canonical receive-direction trace on Linux OVS/tc."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .trace import TraceError, load_trace, sha256_file


class Backend(Protocol):
    def apply(self, profile: dict[str, Any], state_seq: int) -> list[list[str]]: ...


class PrintBackend:
    def apply(self, profile: dict[str, Any], state_seq: int) -> list[list[str]]:
        print(json.dumps({"state_seq": state_seq, "profile": profile}, ensure_ascii=False))
        return []


class LinuxBackend:
    DROP_COOKIE = "0x53475401"

    def __init__(self, *, ifb_device: str, bridge: str, vxlan_port: str) -> None:
        if os.name != "posix":
            raise TraceError("the Linux backend requires a POSIX/Linux host")
        if os.geteuid() != 0:
            raise TraceError("the Linux backend must run as root")
        self.ifb_device = ifb_device
        self.bridge = bridge
        self.vxlan_port = vxlan_port
        self.ofport = self._output(["ovs-vsctl", "get", "Interface", vxlan_port, "ofport"]).strip()
        if not self.ofport.isdigit() or int(self.ofport) <= 0:
            raise TraceError(f"OVS port {vxlan_port} has invalid ofport {self.ofport!r}")
        self._last_available: bool | None = None

    @staticmethod
    def _run(command: list[str]) -> None:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)

    @staticmethod
    def _output(command: list[str]) -> str:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=10).stdout

    def apply(self, profile: dict[str, Any], state_seq: int) -> list[list[str]]:
        rate = int(profile["rate_bps"])
        delay = float(profile["one_way_delay_ms"])
        jitter = float(profile["jitter_ms"])
        loss = float(profile["loss_pct"])
        queue = int(profile["queue_limit_packets"])
        commands = [
            [
                "tc", "class", "change", "dev", self.ifb_device, "parent", "1:", "classid", "1:10",
                "htb", "rate", f"{rate}bit", "ceil", f"{rate}bit", "burst", "256k", "cburst", "256k",
            ],
            [
                "tc", "qdisc", "change", "dev", self.ifb_device, "parent", "1:10", "handle", "10:",
                "netem", "limit", str(queue), "delay", f"{delay:.6f}ms", f"{jitter:.6f}ms",
                "loss", f"{loss:.6f}%",
            ],
        ]
        for command in commands:
            self._run(command)
        available = bool(profile["available"])
        if available != self._last_available:
            selector = f"cookie={self.DROP_COOKIE}/0xffffffffffffffff,priority=200,in_port={self.ofport}"
            delete_command = ["ovs-ofctl", "--strict", "del-flows", self.bridge, selector]
            self._run(delete_command)
            commands.append(delete_command)
            if not available:
                add_command = [
                    "ovs-ofctl", "add-flow", self.bridge,
                    f"cookie={self.DROP_COOKIE},priority=200,in_port={self.ofport},actions=drop",
                ]
                self._run(add_command)
                commands.append(add_command)
            self._last_available = available
        return commands


def receiver_direction(role: str) -> str:
    if role == "ground":
        return "downlink"
    if role == "satellite":
        return "uplink"
    raise TraceError("role must be ground or satellite")


def play_trace(
    trace: dict[str, Any],
    *,
    role: str,
    backend: Backend,
    start_delay_seconds: float = 0.0,
    wait: bool = True,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    audit: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    direction = receiver_direction(role)
    start = monotonic() + max(0.0, start_delay_seconds)
    for state in trace["states"]:
        due = start + state["effective_at_offset_ms"] / 1000
        if wait:
            remaining = due - monotonic()
            if remaining > 0:
                sleeper(remaining)
        applied_at = monotonic()
        profile = state["links"][direction]
        commands = backend.apply(profile, state["state_seq"])
        if audit:
            audit(
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "scenario_id": trace["scenario_id"],
                    "state_seq": state["state_seq"],
                    "role": role,
                    "direction": direction,
                    "scheduled_offset_ms": state["effective_at_offset_ms"],
                    "apply_lag_ms": round((applied_at - due) * 1000, 6),
                    "profile": profile,
                    "commands": commands,
                }
            )


def _start_delay(start_at: str | None, delay_seconds: float) -> float:
    if start_at is None:
        return delay_seconds
    try:
        parsed = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceError(f"invalid --start-at timestamp: {start_at}") from exc
    if parsed.tzinfo is None:
        raise TraceError("--start-at must include a timezone")
    remaining = parsed.timestamp() - time.time()
    if remaining < -0.1:
        raise TraceError("--start-at is in the past")
    return max(0.0, remaining)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--role", choices=("ground", "satellite"), required=True)
    parser.add_argument("--backend", choices=("print", "linux"), default="print")
    parser.add_argument("--start-delay-seconds", type=float, default=0.0)
    parser.add_argument("--start-at", help="Common timezone-aware UTC/ISO start timestamp for both hosts")
    parser.add_argument("--no-wait", action="store_true", help="Apply/print all states immediately for validation")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--ifb-device", default="ifb-sgt")
    parser.add_argument("--bridge", default="br-sgt")
    parser.add_argument("--vxlan-port", default="sgt-vx")
    args = parser.parse_args(argv)
    try:
        trace = load_trace(args.trace)
        delay = _start_delay(args.start_at, args.start_delay_seconds)
        backend: Backend = (
            PrintBackend()
            if args.backend == "print"
            else LinuxBackend(ifb_device=args.ifb_device, bridge=args.bridge, vxlan_port=args.vxlan_port)
        )
        audit_stream = None
        audit_callback = None
        if args.audit:
            args.audit.parent.mkdir(parents=True, exist_ok=True)
            audit_stream = args.audit.open("a", encoding="utf-8", buffering=1)

            def write_audit(record: dict[str, Any]) -> None:
                assert audit_stream is not None
                audit_stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

            audit_callback = write_audit
        print(
            f"trace={trace['scenario_id']} sha256={sha256_file(args.trace)} states={len(trace['states'])} "
            f"role={args.role} backend={args.backend}",
            flush=True,
        )
        try:
            play_trace(
                trace,
                role=args.role,
                backend=backend,
                start_delay_seconds=delay,
                wait=not args.no_wait,
                audit=audit_callback,
            )
        finally:
            if audit_stream:
                audit_stream.close()
        return 0
    except (OSError, subprocess.SubprocessError, TraceError) as exc:
        print(f"trace player error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
