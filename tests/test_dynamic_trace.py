from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from channel_twin.compilers import compile_lens_irtt, compile_wetlinks
from channel_twin.scenario import load_gateway_scenario
from channel_twin.trace import TraceError, load_trace, write_trace
from channel_twin.trace_player import play_trace


class RecordingBackend:
    def __init__(self) -> None:
        self.profiles: list[tuple[int, dict[str, object]]] = []

    def apply(self, profile: dict[str, object], state_seq: int) -> list[list[str]]:
        self.profiles.append((state_seq, profile))
        return []


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class DynamicCompilerTests(unittest.TestCase):
    def test_checked_in_outage_trace_is_valid(self) -> None:
        root = Path(__file__).resolve().parents[1]
        trace = load_trace(root / "configs" / "channel" / "scenarios" / "dynamic-outage-demo.json")
        self.assertEqual(3, len(trace["states"]))
        self.assertFalse(trace["states"][1]["links"]["uplink"]["available"])
        self.assertEqual("dynamic-outage-demo", load_gateway_scenario(trace["trace_path"])["scenario_id"])

    def test_wetlinks_compiles_contiguous_bidirectional_goodput(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wet.csv"
            source.write_text(
                "site_name,timestamp_start,timestamp_end,download,upload\n"
                "utwente,2023-10-12 15:21:13,2023-10-12 15:21:14,120000000,12000000\n"
                "utwente,2023-10-12 15:21:14,2023-10-12 15:21:15,130000000,13000000\n"
                "utwente,2023-10-12 15:30:00,2023-10-12 15:30:01,1,1\n",
                encoding="utf-8",
            )
            trace = compile_wetlinks(source, scenario_id="wet-test", dataset_commit="abc", max_states=10)
            self.assertEqual(2, len(trace["states"]))
            self.assertEqual(120_000_000, trace["states"][0]["links"]["downlink"]["rate_bps"])
            self.assertEqual(12_000_000, trace["states"][0]["links"]["uplink"]["rate_bps"])
            self.assertEqual(2_000, trace["duration_ms"])

    def test_lens_aggregates_directional_delay_loss_and_jitter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "irtt.csv"
            source.write_text(
                "timestamp,rtt,uplink,downlink\n"
                "1705744800000000000,100,60,40\n"
                "1705744800100000000,110,70,40\n"
                "1705744800200000000,0,-1,0\n"
                "1705744801000000000,-1,0,0\n",
                encoding="utf-8",
            )
            trace = compile_lens_irtt(source, scenario_id="lens-test", dataset_commit="def", tick_ms=1000)
            first = trace["states"][0]["links"]
            self.assertAlmostEqual(65.0, first["uplink"]["one_way_delay_ms"])
            self.assertAlmostEqual(33.333333, first["uplink"]["loss_pct"], places=5)
            self.assertEqual(0, first["downlink"]["loss_pct"])
            second = trace["states"][1]["links"]
            self.assertFalse(second["uplink"]["available"])
            self.assertFalse(second["downlink"]["available"])

    def test_trace_round_trip_and_player_selects_receive_direction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wet.csv"
            source.write_text(
                "site_name,timestamp_start,timestamp_end,download,upload\n"
                "x,2023-01-01T00:00:00Z,2023-01-01T00:00:01Z,100,10\n",
                encoding="utf-8",
            )
            trace = compile_wetlinks(source, scenario_id="round-trip", dataset_commit=None, max_states=1)
            output = write_trace(Path(directory) / "trace.json", trace)
            loaded = load_trace(output)
            ground = RecordingBackend()
            satellite = RecordingBackend()
            play_trace(loaded, role="ground", backend=ground, wait=False)
            play_trace(loaded, role="satellite", backend=satellite, wait=False)
            self.assertEqual(100, ground.profiles[0][1]["rate_bps"])
            self.assertEqual(10, satellite.profiles[0][1]["rate_bps"])

    def test_missing_state_gap_is_rejected(self) -> None:
        trace = {
            "schema_version": "1.0",
            "scenario_id": "bad",
            "time_mode": "realtime",
            "source": {},
            "states": [
                {
                    "state_seq": 0,
                    "effective_at_offset_ms": 1,
                    "duration_ms": 1000,
                    "links": {},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TraceError):
                write_trace(Path(directory) / "bad.json", trace)

    def test_player_honors_realtime_offsets_with_monotonic_clock(self) -> None:
        root = Path(__file__).resolve().parents[1]
        trace = load_trace(root / "configs" / "channel" / "scenarios" / "dynamic-outage-demo.json")
        clock = FakeClock()
        backend = RecordingBackend()
        play_trace(
            trace,
            role="ground",
            backend=backend,
            wait=True,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )
        self.assertEqual([2.0, 3.0], clock.sleeps)
        self.assertEqual([0, 1, 2], [state_seq for state_seq, _ in backend.profiles])


if __name__ == "__main__":
    unittest.main()
