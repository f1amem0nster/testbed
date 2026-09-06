"""Dataset-specific compilers for canonical dynamic channel traces."""

from __future__ import annotations

import csv
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .trace import TraceError, sha256_file, validate_trace


def _float(row: dict[str, str], key: str) -> float | None:
    raw = row.get(key, "").strip()
    if not raw or raw.lower() in {"nan", "none", "null"}:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise TraceError(f"invalid numeric value for {key}: {raw!r}") from exc
    return value if math.isfinite(value) else None


def _iso(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TraceError(f"invalid ISO timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _profile(
    *,
    available: bool,
    rate_bps: float,
    delay_ms: float,
    jitter_ms: float,
    loss_pct: float,
    queue_packets: int,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "available": available,
        "rate_bps": max(1, int(round(rate_bps))),
        "one_way_delay_ms": round(max(0.0, delay_ms), 6),
        "jitter_ms": round(max(0.0, jitter_ms), 6),
        "loss_pct": round(min(100.0, max(0.0, loss_pct)), 6),
        "queue_limit_packets": queue_packets,
        "provenance": provenance,
    }


def compile_wetlinks(
    source: str | Path,
    *,
    scenario_id: str,
    dataset_commit: str | None,
    max_states: int = 60,
    start_at: str | None = None,
    fixed_one_way_delay_ms: float = 0.0,
    fixed_loss_pct: float = 0.0,
    queue_packets: int = 10_000,
    gap_tolerance_ms: int = 1_500,
) -> dict[str, Any]:
    """Compile one contiguous second-level WetLinks iperf segment.

    Missing throughput or a timestamp gap ends the segment.  It is never converted
    into an outage because the source only says that no measurement is available.
    """
    source_path = Path(source).expanduser().resolve()
    if max_states <= 0:
        raise TraceError("max_states must be positive")
    start_limit = _iso(start_at) if start_at else None
    states: list[dict[str, Any]] = []
    origin: datetime | None = None
    previous_end: datetime | None = None
    expected_site: str | None = None
    with source_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"site_name", "timestamp_start", "timestamp_end", "download", "upload"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise TraceError(f"WetLinks CSV is missing fields: {sorted(required - set(reader.fieldnames or []))}")
        for row_number, row in enumerate(reader, start=2):
            started = _iso(row["timestamp_start"])
            ended = _iso(row["timestamp_end"])
            if start_limit and started < start_limit:
                continue
            download = _float(row, "download")
            upload = _float(row, "upload")
            if download is None or upload is None or download <= 0 or upload <= 0:
                if states:
                    break
                continue
            if expected_site is None:
                expected_site = row["site_name"]
            if row["site_name"] != expected_site:
                if states:
                    break
                continue
            if previous_end is not None:
                gap_ms = (started - previous_end).total_seconds() * 1000
                if gap_ms < -1 or gap_ms > gap_tolerance_ms:
                    break
            duration_ms = max(1, int(round((ended - started).total_seconds() * 1000)))
            if duration_ms <= 0:
                raise TraceError(f"WetLinks row {row_number} has a non-positive duration")
            if origin is None:
                origin = started
            common = {
                "source_dataset": "WetLinks",
                "source_row": row_number,
                "source_timestamp_start": row["timestamp_start"],
                "rate_semantics": "measured UDP iperf goodput used as an IP service-rate proxy, not RF capacity",
                "delay_loss_semantics": "fixed compiler input; not synchronized WetLinks ping",
            }
            states.append(
                {
                    "state_seq": len(states),
                    "effective_at_offset_ms": sum(state["duration_ms"] for state in states),
                    "duration_ms": duration_ms,
                    "links": {
                        "uplink": _profile(
                            available=True,
                            rate_bps=upload,
                            delay_ms=fixed_one_way_delay_ms,
                            jitter_ms=0,
                            loss_pct=fixed_loss_pct,
                            queue_packets=queue_packets,
                            provenance={**common, "source_column": "upload"},
                        ),
                        "downlink": _profile(
                            available=True,
                            rate_bps=download,
                            delay_ms=fixed_one_way_delay_ms,
                            jitter_ms=0,
                            loss_pct=fixed_loss_pct,
                            queue_packets=queue_packets,
                            provenance={**common, "source_column": "download"},
                        ),
                    },
                }
            )
            previous_end = ended
            if len(states) >= max_states:
                break
    if not states or origin is None:
        raise TraceError("no contiguous WetLinks rows with positive upload and download were found")
    return validate_trace(
        {
            "schema_version": "1.0",
            "scenario_id": scenario_id,
            "time_mode": "realtime",
            "origin_timestamp_utc": origin.astimezone(timezone.utc).isoformat(),
            "source": {
                "dataset": "WetLinks",
                "source_path": str(source_path),
                "source_sha256": sha256_file(source_path),
                "dataset_commit": dataset_commit,
                "license": "CC BY-SA 4.0",
                "site": expected_site,
                "limitations": [
                    "Starlink end-to-end service measurement, not a remote-sensing satellite direct-link trace.",
                    "UDP iperf achieved goodput is used as a service-rate proxy.",
                    "Rows after missing data or a timestamp gap are not converted into outages.",
                ],
            },
            "compiler": {
                "kind": "wetlinks_contiguous_iperf_seconds",
                "max_states": max_states,
                "gap_tolerance_ms": gap_tolerance_ms,
                "fixed_one_way_delay_ms": fixed_one_way_delay_ms,
                "fixed_loss_pct": fixed_loss_pct,
            },
            "states": states,
        }
    )


def _timestamp_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise TraceError(f"invalid LENS timestamp: {raw!r}") from exc
    if value > 1e15:
        return value / 1_000_000_000
    if value > 1e12:
        return value / 1_000
    return value


def _mean(values: Iterable[float], fallback: float) -> float:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else fallback


def _jitter(values: Iterable[float]) -> float:
    materialized = list(values)
    return statistics.pstdev(materialized) if len(materialized) > 1 else 0.0


def compile_lens_irtt(
    source: str | Path,
    *,
    scenario_id: str,
    dataset_commit: str | None,
    tick_ms: int = 1_000,
    max_states: int = 60,
    fixed_rate_bps: int = 110_670_000,
    fallback_one_way_delay_ms: float = 0.0,
    queue_packets: int = 10_000,
    outage_loss_pct: float = 100.0,
) -> dict[str, Any]:
    """Aggregate a processed LENS IRTT CSV into real-time tc-sized ticks."""
    source_path = Path(source).expanduser().resolve()
    if tick_ms <= 0 or max_states <= 0 or fixed_rate_bps <= 0:
        raise TraceError("tick_ms, max_states, and fixed_rate_bps must be positive")
    buckets: dict[int, list[dict[str, str]]] = {}
    origin_seconds: float | None = None
    with source_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp", "rtt", "uplink", "downlink"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise TraceError(f"LENS IRTT CSV is missing fields: {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            timestamp = _timestamp_seconds(row["timestamp"])
            if origin_seconds is None:
                origin_seconds = timestamp
            bucket = int((timestamp - origin_seconds) * 1000 // tick_ms)
            if bucket < 0:
                raise TraceError("LENS timestamps must be monotonically non-decreasing")
            if bucket >= max_states:
                break
            buckets.setdefault(bucket, []).append(row)
    if origin_seconds is None or 0 not in buckets:
        raise TraceError("LENS IRTT CSV contains no usable rows")

    states: list[dict[str, Any]] = []
    previous_delays = {"uplink": fallback_one_way_delay_ms, "downlink": fallback_one_way_delay_ms}
    for bucket in range(max_states):
        rows = buckets.get(bucket)
        if not rows:
            break
        total = len(rows)
        direction_values: dict[str, list[float]] = {"uplink": [], "downlink": []}
        losses = {"uplink": 0, "downlink": 0}
        ambiguous_losses = 0
        for row in rows:
            rtt = _float(row, "rtt")
            up = _float(row, "uplink")
            down = _float(row, "downlink")
            if rtt is not None and rtt < 0:
                losses["uplink"] += 1
                losses["downlink"] += 1
                ambiguous_losses += 1
                continue
            for direction, value in (("uplink", up), ("downlink", down)):
                if value is not None and value < 0:
                    losses[direction] += 1
                elif value is not None and value > 0:
                    direction_values[direction].append(value)
        links: dict[str, Any] = {}
        for direction in ("uplink", "downlink"):
            values = direction_values[direction]
            delay = _mean(values, previous_delays[direction])
            if values:
                previous_delays[direction] = delay
            loss_pct = losses[direction] * 100.0 / total
            links[direction] = _profile(
                available=loss_pct < outage_loss_pct,
                rate_bps=fixed_rate_bps,
                delay_ms=delay,
                jitter_ms=_jitter(values),
                loss_pct=loss_pct,
                queue_packets=queue_packets,
                provenance={
                    "source_dataset": "LENS",
                    "source_bucket": bucket,
                    "source_samples": total,
                    "valid_directional_delay_samples": len(values),
                    "ambiguous_loss_samples_applied_to_both_directions": ambiguous_losses,
                    "delay_semantics": "IRTT end-to-end one-way delay, including terrestrial path and endpoints",
                    "rate_semantics": "fixed control value; LENS IRTT does not measure capacity",
                },
            )
        states.append(
            {
                "state_seq": len(states),
                "effective_at_offset_ms": len(states) * tick_ms,
                "duration_ms": tick_ms,
                "links": links,
            }
        )
    return validate_trace(
        {
            "schema_version": "1.0",
            "scenario_id": scenario_id,
            "time_mode": "realtime",
            "origin_timestamp_utc": datetime.fromtimestamp(origin_seconds, timezone.utc).isoformat(),
            "source": {
                "dataset": "LENS",
                "source_path": str(source_path),
                "source_sha256": sha256_file(source_path),
                "dataset_commit": dataset_commit,
                "license": "CC BY-SA 4.0 for dataset files; repository code GPL-3.0",
                "limitations": [
                    "Starlink end-to-end service measurement, not a remote-sensing satellite direct-link trace.",
                    "IRTT delay includes non-satellite path components.",
                    "Rate is a fixed control value because IRTT does not measure capacity.",
                    "Missing aggregate buckets terminate the trace and are not interpreted as outages.",
                ],
            },
            "compiler": {
                "kind": "lens_processed_irtt",
                "tick_ms": tick_ms,
                "max_states": max_states,
                "fixed_rate_bps": fixed_rate_bps,
                "fallback_one_way_delay_ms": fallback_one_way_delay_ms,
                "outage_loss_pct": outage_loss_pct,
            },
            "states": states,
        }
    )
