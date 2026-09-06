"""Compile WetLinks or LENS measurements into the canonical dynamic trace."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from channel_twin.compilers import compile_lens_irtt, compile_wetlinks
from channel_twin.trace import TraceError, write_trace


def git_commit(dataset_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={dataset_root}", "-C", str(dataset_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="dataset", required=True)

    wetlinks = subparsers.add_parser("wetlinks", help="Compile one contiguous second-level iperf segment")
    wetlinks.add_argument("--dataset-root", required=True, type=Path)
    wetlinks.add_argument("--source", type=Path, help="CSV override; defaults to Preprocessed_Data for --site")
    wetlinks.add_argument("--site", default="Enschede")
    wetlinks.add_argument("--start-at", help="Optional ISO timestamp at or after which the segment starts")
    wetlinks.add_argument("--max-states", type=int, default=60)
    wetlinks.add_argument("--fixed-one-way-delay-ms", type=float, default=0.0)
    wetlinks.add_argument("--fixed-loss-pct", type=float, default=0.0)
    wetlinks.add_argument("--gap-tolerance-ms", type=int, default=1500)
    wetlinks.add_argument("--queue-packets", type=int, default=10000)
    wetlinks.add_argument("--scenario-id", default="wetlinks-dynamic-auxiliary")
    wetlinks.add_argument("--output", required=True, type=Path)

    lens = subparsers.add_parser("lens", help="Compile an extracted processed LENS IRTT CSV")
    lens.add_argument("--dataset-root", required=True, type=Path)
    lens.add_argument("--source", type=Path, help="Extracted processed IRTT CSV; the Git clone does not contain snapshots")
    lens.add_argument("--tick-ms", type=int, default=1000)
    lens.add_argument("--max-states", type=int, default=60)
    lens.add_argument("--fixed-rate-bps", type=int, default=110670000)
    lens.add_argument("--fallback-one-way-delay-ms", type=float, default=0.0)
    lens.add_argument("--outage-loss-pct", type=float, default=100.0)
    lens.add_argument("--queue-packets", type=int, default=10000)
    lens.add_argument("--scenario-id", default="lens-irtt-dynamic-auxiliary")
    lens.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_root = args.dataset_root.expanduser().resolve()
    try:
        if not dataset_root.is_dir():
            raise TraceError(f"dataset root does not exist: {dataset_root}")
        commit = git_commit(dataset_root)
        if args.dataset == "wetlinks":
            source = args.source or dataset_root / "Preprocessed_Data" / f"iperf_cleaned_seconds_{args.site}.csv"
            trace = compile_wetlinks(
                source,
                scenario_id=args.scenario_id,
                dataset_commit=commit,
                max_states=args.max_states,
                start_at=args.start_at,
                fixed_one_way_delay_ms=args.fixed_one_way_delay_ms,
                fixed_loss_pct=args.fixed_loss_pct,
                queue_packets=args.queue_packets,
                gap_tolerance_ms=args.gap_tolerance_ms,
            )
        else:
            if args.source is None:
                candidates = list(dataset_root.rglob("*.csv"))
                if not candidates:
                    raise TraceError(
                        "the LENS Git clone contains download manifests but no processed snapshot CSV; "
                        "download/extract a LENS CSV archive and pass an IRTT CSV with --source"
                    )
                raise TraceError("multiple/unknown LENS CSVs found; select a processed IRTT CSV with --source")
            trace = compile_lens_irtt(
                args.source,
                scenario_id=args.scenario_id,
                dataset_commit=commit,
                tick_ms=args.tick_ms,
                max_states=args.max_states,
                fixed_rate_bps=args.fixed_rate_bps,
                fallback_one_way_delay_ms=args.fallback_one_way_delay_ms,
                queue_packets=args.queue_packets,
                outage_loss_pct=args.outage_loss_pct,
            )
        output = write_trace(args.output, trace)
        print(f"wrote {len(trace['states'])} states ({trace['duration_ms']} ms) to {output}")
        print(f"source sha256: {trace['source']['source_sha256']}")
        print(f"dataset commit: {trace['source']['dataset_commit'] or 'unavailable'}")
        return 0
    except (OSError, TraceError) as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
