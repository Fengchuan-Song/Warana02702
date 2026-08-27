"""Command-line entry point for standalone AIS/Radar inference."""

import argparse
import json
from pathlib import Path

from .predictor import AISRadarMatcher, InferenceConfig


APP_DIR = Path(__file__).resolve().parent.parent


def parse_args():
    parser = argparse.ArgumentParser(description="Run causal JPDA AIS/Radar trajectory matching.")
    parser.add_argument("--ais-file", required=True)
    parser.add_argument("--radar-file", required=True)
    parser.add_argument(
        "--weights",
        default=str(APP_DIR / "weights" / "mainline_seed42_epoch40.pth"),
    )
    parser.add_argument("--device", default="auto")
    parser.add_argument("--all-windows", action="store_true")
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--max-windows", type=int, default=None)
    parser.add_argument("--match-threshold", type=float, default=0.0)
    parser.add_argument(
        "--max-ais-time-gap-seconds",
        type=float,
        default=None,
        help="Maximum causal AIS age; default is three observed AIS sampling periods.",
    )
    parser.add_argument(
        "--debug-jpda",
        action="store_true",
        help="Enable per-frame JPDA association diagnostics through logging.",
    )
    parser.add_argument("--output", default=None, help="Optional JSON output path.")
    return parser.parse_args()


def main():
    args = parse_args()
    matcher = AISRadarMatcher(
        InferenceConfig(
            checkpoint_path=Path(args.weights),
            device=args.device,
            match_threshold=args.match_threshold,
            max_ais_time_gap_seconds=args.max_ais_time_gap_seconds,
            debug_jpda=args.debug_jpda,
        )
    )
    result = matcher.predict_files(
        args.ais_file,
        args.radar_file,
        latest_only=not args.all_windows,
        stride=args.stride,
        max_windows=args.max_windows,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
