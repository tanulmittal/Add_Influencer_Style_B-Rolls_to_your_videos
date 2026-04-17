#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from create import (
    Segment,
    discover_single_file,
    has_audio_stream,
    load_segments_from_edit_plan,
    parse_srt,
    probe_video,
    render_video,
)


def validate_required_broll_files(broll_dir: Path, segments: list[Segment]) -> None:
    missing_files = [
        segment.broll_file
        for segment in segments
        if segment.broll_file and not (broll_dir / segment.broll_file).is_file()
    ]
    if missing_files:
        joined = "\n".join(f"- {name}" for name in missing_files)
        raise SystemExit(
            "Missing required B-roll files referenced by output/edit_plan.json:\n"
            f"{joined}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rerender a project using the existing output/edit_plan.json and current B-roll files."
    )
    parser.add_argument("folder", help="Project folder to rerender.")
    parser.add_argument(
        "--burn-subs",
        dest="burn_subtitles",
        action="store_true",
        help="Backward-compatible alias for enabling burned-in subtitles.",
    )
    parser.add_argument(
        "--no-subs",
        dest="burn_subtitles",
        action="store_false",
        help="Render without burned-in subtitles.",
    )
    parser.set_defaults(burn_subtitles=True)
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        raise SystemExit(f"{folder} is not a folder.")

    output_dir = folder / "output"
    edit_plan_path = output_dir / "edit_plan.json"
    if not edit_plan_path.is_file():
        raise SystemExit(
            f"Missing edit plan: {edit_plan_path}. Run create.py {folder.name} first."
        )

    video_path = discover_single_file(folder, (".mp4", ".mov", ".m4v"))
    srt_path = discover_single_file(folder, (".srt",))
    cues = parse_srt(srt_path)
    _, fps = probe_video(video_path)
    segments = load_segments_from_edit_plan(edit_plan_path)
    broll_dir = folder / "B_roll"
    validate_required_broll_files(broll_dir, segments)

    output_path = render_video(
        folder=folder,
        video_path=video_path,
        srt_path=srt_path,
        output_dir=output_dir,
        broll_dir=broll_dir,
        segments=segments,
        cues=cues,
        fps=fps,
        include_audio=has_audio_stream(video_path),
        burn_subtitles=args.burn_subtitles,
    )
    print(f"Rendered {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
