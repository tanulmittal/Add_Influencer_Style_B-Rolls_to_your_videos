#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from create import (
    GROQ_WORD_TRANSCRIPT_MODEL,
    Segment,
    SubtitleCard,
    WORD_TRANSCRIPT_LANGUAGE,
    build_subtitle_cards,
    discover_single_file,
    has_audio_stream,
    prepare_project_render_inputs,
    prepare_broll_dir,
    render_video,
    resolve_subtitle_model,
    sync_broll_files_by_segment_index,
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
            "Missing required B-roll files referenced by the rebuilt output/edit_plan.json:\n"
            f"{joined}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rerender a project using the current transcript artifacts and B-roll files."
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
    parser.add_argument(
        "--subtitle-mode",
        choices=("word", "cue"),
        default="word",
        help="Burn subtitles using word-level timing or the original cue-level SRT timing.",
    )
    parser.add_argument(
        "--subtitle-model",
        default=None,
        help=f"Groq transcription model to use for word-level subtitles (default: {GROQ_WORD_TRANSCRIPT_MODEL}).",
    )
    parser.add_argument(
        "--subtitle-language",
        default=WORD_TRANSCRIPT_LANGUAGE,
        help=f"Language code for word-level transcription (default: {WORD_TRANSCRIPT_LANGUAGE}).",
    )
    parser.set_defaults(burn_subtitles=True)
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        raise SystemExit(f"{folder} is not a folder.")

    output_dir = folder / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    video_path = discover_single_file(folder, (".mp4", ".mov", ".m4v"))
    subtitle_model = resolve_subtitle_model(args.subtitle_model)
    cues, segments, fps = prepare_project_render_inputs(
        video_path=video_path,
        output_dir=output_dir,
        model_name=subtitle_model,
        language=args.subtitle_language,
    )
    broll_dir = prepare_broll_dir(folder, create=False)
    sync_broll_files_by_segment_index(broll_dir, segments)
    validate_required_broll_files(broll_dir, segments)
    with tempfile.TemporaryDirectory(prefix=".subtitle_cards_", dir=output_dir) as subtitle_dir:
        subtitle_cards: list[SubtitleCard] = []
        if args.burn_subtitles:
            subtitle_cards = build_subtitle_cards(
                output_dir=output_dir,
                subtitle_dir=Path(subtitle_dir),
                video_path=video_path,
                cues=cues,
                subtitle_mode=args.subtitle_mode,
                subtitle_model=subtitle_model,
                subtitle_language=args.subtitle_language,
            )

        output_path = render_video(
            video_path=video_path,
            output_dir=output_dir,
            broll_dir=broll_dir,
            segments=segments,
            subtitle_cards=subtitle_cards,
            fps=fps,
            include_audio=has_audio_stream(video_path),
        )
    print(f"Rendered {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
