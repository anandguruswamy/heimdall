from __future__ import annotations

import argparse
from pathlib import Path

from .capture import decode_capture
from .model import Geometry, GridSpec, QualityConfig
from .processing import backproject, build_link_profiles
from .server import serve
from .storage import export_volume


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and serve Heimdall bistatic radar volumes")
    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build", help="backproject a .husb capture")
    build.add_argument("capture", type=Path)
    build.add_argument("geometry", type=Path)
    build.add_argument("output", type=Path)
    build.add_argument(
        "--bounds",
        type=float,
        nargs=6,
        metavar=("X_MIN", "X_MAX", "Y_MIN", "Y_MAX", "Z_MIN", "Z_MAX"),
        required=True,
    )
    build.add_argument("--spacing", type=float, default=0.10, help="voxel spacing in metres")
    build.add_argument("--clutter-frames", type=int, default=16)
    build.add_argument("--direct-path-guard-taps", type=float, default=2.0)
    build.add_argument("--zarr", action="store_true", help="also export volume.zarr")
    build.add_argument(
        "--require-complete-stream",
        action="store_true",
        help="reject parser loss, post-HELLO decode rejects, or incomplete pooled reports",
    )
    build.add_argument("--max-first-path-jump", type=float, default=8.0)
    build.add_argument("--max-start-offset-jump", type=float, default=8.0)

    server = subcommands.add_parser("serve", help="serve an exported volume")
    server.add_argument("volume", type=Path)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "serve":
        serve(args.volume, args.host, args.port)
        return
    geometry = Geometry.load(args.geometry)
    observations, capture_stats = decode_capture(args.capture)
    if capture_stats["hello"] is None:
        raise SystemExit("capture contains no decodable HELLO")
    if capture_stats["trailing_bytes"]:
        raise SystemExit("capture ends with a partial USB record")
    if args.require_complete_stream:
        parser_stats = capture_stats["parser"]
        incomplete = {
            "crc_failures": parser_stats["crc_failures"],
            "framing_errors": parser_stats["framing_errors"],
            "sequence_gaps": parser_stats["sequence_gaps"],
            "post_hello_decode_rejections": capture_stats["post_hello_decode_rejections"],
            "incomplete_reports": capture_stats["incomplete_reports"],
            "inconsistent_fragments": capture_stats["inconsistent_fragments"],
        }
        failures = {name: value for name, value in incomplete.items() if value}
        if failures:
            raise SystemExit(f"capture completeness check failed: {failures}")
    quality = QualityConfig(
        max_first_path_jump_samples=args.max_first_path_jump,
        max_start_offset_jump_samples=args.max_start_offset_jump,
    )
    profiles, quality_stats = build_link_profiles(
        observations,
        geometry,
        quality=quality,
        clutter_frames=args.clutter_frames,
        direct_path_guard_taps=args.direct_path_guard_taps,
    )
    if not profiles:
        raise SystemExit("capture produced no usable directed-link profiles")
    bounds = args.bounds
    grid = GridSpec(
        (bounds[0], bounds[2], bounds[4]),
        (bounds[1], bounds[3], bounds[5]),
        args.spacing,
    )
    result = backproject(profiles, geometry, grid, product="motion")
    static_result = backproject(profiles, geometry, grid, product="static")
    processing = {
        "tool_version": "0.1.0",
        "capture": str(args.capture),
        "capture_stats": capture_stats,
        "quality_config": vars(quality),
        "quality": quality_stats,
    }
    export_volume(
        result,
        args.output,
        processing,
        zarr=args.zarr,
        additional_products={"static": static_result},
    )
    print(
        f"Wrote {result.volume.shape} volume from {len(profiles)} directed links to {args.output}"
    )


if __name__ == "__main__":
    main()
