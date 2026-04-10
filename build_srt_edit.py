#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

WIDTH = 1080
HEIGHT = 1920
TEMPLATE2_BROLL_HEIGHT = int(HEIGHT * 0.4)
TEMPLATE2_AROLL_Y = int(HEIGHT * 0.3)
BROLL_START_ZOOM = 1.0
BROLL_END_ZOOM = 1.2
MIN_EDGE_DURATION = 1.0
MAX_EDGE_DURATION = 3.0
MIDDLE_BROLL_DURATION = 2.0
PRODUCT_TERMS = {
    "openai",
    "chatgpt",
    "codex",
    "plus",
    "pro",
    "instant",
    "thinking",
    "model",
    "models",
    "tier",
    "usage",
}
EXPLANATORY_PHRASES = (
    "that gives you",
    "built for",
    "includes",
    "still includes",
    "access to",
)
MONTH_TERMS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
TEMPLATE_SEQUENCE = [
    "template_3",
    "template_1",
    "template_2",
    "template_3",
    "template_1",
    "template_2",
    "template_3",
    "template_2",
    "template_1",
    "template_3",
    "template_1",
    "template_2",
    "template_3",
    "template_1",
    "template_2",
    "template_3",
    "template_2",
]


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str


@dataclass
class Segment:
    index: int
    start: float
    end: float
    duration: float
    text: str
    template: str
    broll_file: str | None


def run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def probe_video(video_path: Path) -> tuple[float, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    duration = float(payload["format"]["duration"])
    frame_rate_raw = payload["streams"][0]["r_frame_rate"]
    numerator, denominator = frame_rate_raw.split("/")
    fps = round(float(numerator) / float(denominator))
    return float(duration), fps


def has_audio_stream(video_path: Path) -> bool:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return bool(result.stdout.strip())


def discover_single_file(folder: Path, suffixes: tuple[str, ...]) -> Path:
    matches = [path for path in folder.iterdir() if path.suffix.lower() in suffixes]
    if len(matches) != 1:
        joined = ", ".join(suffixes)
        raise SystemExit(
            f"Expected exactly one file matching {joined} in {folder}, found {len(matches)}."
        )
    return matches[0]


def timestamp_to_seconds(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    whole_seconds, milliseconds = seconds.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(whole_seconds)
        + int(milliseconds) / 1000
    )


def parse_srt(srt_path: Path) -> list[Cue]:
    raw = srt_path.read_text(encoding="utf-8").strip()
    blocks = re.split(r"\n\s*\n", raw)
    cues: list[Cue] = []
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        cue_id = int(lines[0])
        start_raw, end_raw = lines[1].split(" --> ")
        text = " ".join(lines[2:]).strip()
        cues.append(
            Cue(
                index=cue_id,
                start=timestamp_to_seconds(start_raw),
                end=timestamp_to_seconds(end_raw),
                text=text,
            )
        )
    if not cues:
        raise SystemExit(f"No cues found in {srt_path}")
    return cues


def clean_caption_text(text: str) -> str:
    return re.sub(r"\[[^\]]+\]", "", text).strip()


def slugify(text: str, fallback: str) -> str:
    cleaned = clean_caption_text(text).lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = "_".join(cleaned.split("_")[:8])
    return cleaned or fallback


def build_segments(cues: list[Cue], video_duration: float) -> list[Segment]:
    boundaries = build_segment_boundaries(video_duration)

    segments: list[Segment] = []
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end <= start:
            end = min(video_duration, start + 0.05)
        segment_text = " ".join(
            clean_caption_text(cue.text) for cue in cues if cue.start < end and cue.end > start
        ).strip()
        if not segment_text:
            segment_text = f"segment {index + 1}"
        segments.append(
            Segment(
                index=index + 1,
                start=round(start, 3),
                end=round(end, 3),
                duration=round(end - start, 3),
                text=segment_text,
                template="template_1",
                broll_file=None,
            )
        )
    return segments


def build_segment_boundaries(video_duration: float) -> list[float]:
    middle_options = []
    upper_bound = int(video_duration // MIDDLE_BROLL_DURATION) + 2
    for middle_count in range(0, upper_bound):
        edge_duration = (video_duration - middle_count * MIDDLE_BROLL_DURATION) / 2
        if MIN_EDGE_DURATION <= edge_duration <= MAX_EDGE_DURATION:
            middle_options.append(middle_count)
    if middle_options:
        middle_count = max(middle_options)
        edge_duration = (video_duration - middle_count * MIDDLE_BROLL_DURATION) / 2
        boundaries = [0.0, round(edge_duration, 3)]
        cursor = edge_duration
        for _ in range(middle_count):
            cursor += MIDDLE_BROLL_DURATION
            boundaries.append(round(cursor, 3))
        boundaries[-1] = round(video_duration - edge_duration, 3)
        boundaries.append(round(video_duration, 3))
        return boundaries

    shot_count = max(2, round(video_duration / 4))
    return [round(video_duration * index / shot_count, 3) for index in range(shot_count + 1)]


def choose_middle_template(text: str) -> str:
    lower = text.lower()
    score = 0
    if re.search(r"(\$|₹|€|\d)", text):
        score += 2
    if any(month in lower for month in MONTH_TERMS):
        score += 2
    score += min(2, sum(1 for term in PRODUCT_TERMS if term in lower))
    if any(phrase in lower for phrase in EXPLANATORY_PHRASES):
        score -= 1
    return "template_3" if score >= 3 else "template_2"


def assign_templates(segments: list[Segment]) -> None:
    for segment in segments:
        if segment.index == 1 or segment.index == len(segments):
            segment.template = "template_1"
        else:
            middle_index = segment.index - 2
            segment.template = TEMPLATE_SEQUENCE[middle_index % len(TEMPLATE_SEQUENCE)]
        segment.broll_file = f"{segment.index:02d}_{slugify(segment.text, f'segment_{segment.index:02d}')}.png"
        if segment.template == "template_1":
            segment.broll_file = None


def pick_font() -> str | None:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def wrap_placeholder_label(text: str, max_chars: int) -> str:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        projected_len = current_len + len(word) + (1 if current else 0)
        if current and projected_len > max_chars:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len = projected_len
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def placeholder_text_style(template: str) -> tuple[int, int]:
    if template == "template_2":
        return 15, 34
    return 16, 36


def create_placeholder_images(broll_dir: Path, segments: list[Segment], overwrite: bool) -> None:
    broll_dir.mkdir(parents=True, exist_ok=True)
    font_path = pick_font()
    label_dir = broll_dir / ".label_cache"
    label_dir.mkdir(exist_ok=True)
    for segment in segments:
        if not segment.broll_file:
            continue
        output_path = broll_dir / segment.broll_file
        if output_path.exists() and not overwrite:
            continue
        max_chars, font_size = placeholder_text_style(segment.template)
        label = wrap_placeholder_label(
            output_path.stem.replace("_", " ").upper(),
            max_chars=max_chars,
        )
        label_path = label_dir / f"{output_path.stem}.txt"
        label_path.write_text(label, encoding="utf-8")
        placeholder_width, placeholder_height = broll_dimensions_for_template(segment.template)
        drawtext = (
            "drawtext="
            f"fontfile='{font_path}':"
            f"textfile='{label_path}':"
            "fontcolor=white:"
            f"fontsize={font_size}:"
            "x=(w-text_w)/2:"
            "y=(h-text_h)/2:"
            "line_spacing=12"
        ) if font_path else None
        vf = [drawtext] if drawtext else []
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=#111827:s={placeholder_width}x{placeholder_height}",
                "-frames:v",
                "1",
                "-update",
                "1",
                *([ "-vf", ",".join(vf) ] if vf else []),
                str(output_path),
            ]
        )


def broll_dimensions_for_template(template: str) -> tuple[int, int]:
    if template == "template_2":
        return WIDTH, TEMPLATE2_BROLL_HEIGHT
    return WIDTH, HEIGHT


def broll_frame_count(duration: float, fps: int) -> int:
    return max(1, round(duration * fps))


def build_broll_zoompan_filter(
    input_index: int,
    output_label: str,
    output_width: int,
    output_height: int,
    duration: float,
    fps: int,
) -> str:
    frame_count = broll_frame_count(duration, fps)
    zoom_span = max(frame_count - 1, 1)
    zoom_expr = (
        f"min({BROLL_END_ZOOM:.3f},"
        f"{BROLL_START_ZOOM:.3f}+({BROLL_END_ZOOM - BROLL_START_ZOOM:.3f}*on/{zoom_span}))"
    )
    return (
        f"[{input_index}:v]trim=end_frame=1,"
        f"zoompan=z='{zoom_expr}':"
        "x='iw/2-(iw/zoom/2)':"
        "y='ih/2-(ih/zoom/2)':"
        f"d={frame_count}:"
        f"s={output_width}x{output_height}:"
        f"fps={fps},"
        f"trim=duration={duration:.3f},"
        f"setpts=PTS-STARTPTS[{output_label}]"
    )


def remove_unused_broll_files(broll_dir: Path, segments: list[Segment]) -> None:
    if not broll_dir.exists():
        return
    expected_files = {segment.broll_file for segment in segments if segment.broll_file}
    for path in broll_dir.iterdir():
        if path.is_file() and path.name not in expected_files:
            path.unlink()


def write_edit_plan(output_path: Path, video_path: Path, srt_path: Path, segments: list[Segment]) -> None:
    payload = {
        "video": video_path.name,
        "srt": srt_path.name,
        "formula": "template_1 + repeated relevant templates + template_1",
        "segments": [asdict(segment) for segment in segments],
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_filter_complex(
    segments: list[Segment],
    include_audio: bool,
    image_inputs: dict[str, int],
    burn_subtitles: bool,
    fps: int,
) -> tuple[str, str, str]:
    filters: list[str] = []
    concat_parts: list[str] = []
    for zero_based_index, segment in enumerate(segments):
        video_trim = (
            f"[0:v]trim=start={segment.start:.3f}:end={segment.end:.3f},"
            "setpts=PTS-STARTPTS"
        )
        if segment.template == "template_1":
            filters.append(
                video_trim
                + f",scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                + f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black[v{zero_based_index}]"
            )
        elif segment.template == "template_2":
            if not segment.broll_file:
                raise SystemExit(f"Missing B-roll file mapping for segment {segment.index}")
            input_index = image_inputs[segment.broll_file]
            filters.append(
                build_broll_zoompan_filter(
                    input_index=input_index,
                    output_label=f"b{zero_based_index}",
                    output_width=WIDTH,
                    output_height=TEMPLATE2_BROLL_HEIGHT,
                    duration=segment.duration,
                    fps=fps,
                )
            )
            filters.append(
                video_trim
                + f",scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                + f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black[a_src{zero_based_index}]"
            )
            filters.append(
                f"color=c=black:s={WIDTH}x{HEIGHT}:d={segment.duration:.3f}[base{zero_based_index}]"
            )
            filters.append(
                f"[base{zero_based_index}][a_src{zero_based_index}]overlay=0:{TEMPLATE2_AROLL_Y}[a_shift{zero_based_index}]"
            )
            filters.append(
                f"[a_shift{zero_based_index}][b{zero_based_index}]overlay=0:0[v{zero_based_index}]"
            )
        else:
            if not segment.broll_file:
                raise SystemExit(f"Missing B-roll file mapping for segment {segment.index}")
            input_index = image_inputs[segment.broll_file]
            filters.append(
                build_broll_zoompan_filter(
                    input_index=input_index,
                    output_label=f"v{zero_based_index}",
                    output_width=WIDTH,
                    output_height=HEIGHT,
                    duration=segment.duration,
                    fps=fps,
                )
            )

        if include_audio:
            filters.append(
                f"[0:a]atrim=start={segment.start:.3f}:end={segment.end:.3f},"
                f"asetpts=PTS-STARTPTS[a{zero_based_index}]"
            )
            concat_parts.append(f"[v{zero_based_index}][a{zero_based_index}]")
        else:
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={segment.duration:.3f}[a{zero_based_index}]"
            )
            concat_parts.append(f"[v{zero_based_index}][a{zero_based_index}]")

    filters.append(
        "".join(concat_parts)
        + f"concat=n={len(segments)}:v=1:a=1[concat_v][concat_a]"
    )
    video_label = "concat_v"
    if burn_subtitles:
        style = (
            "FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF&,"
            "OutlineColour=&H00000000&,BorderStyle=1,Outline=2,Shadow=0,"
            "Alignment=2,MarginV=80"
        )
        filters.append(
            f"[concat_v]subtitles=filename='captions_render.srt':force_style='{style}'[final_v]"
        )
        video_label = "final_v"
    return ";".join(filters), video_label, "concat_a"


def render_video(
    folder: Path,
    video_path: Path,
    srt_path: Path,
    output_dir: Path,
    broll_dir: Path,
    segments: list[Segment],
    fps: int,
    include_audio: bool,
    burn_subtitles: bool,
) -> Path:
    image_segments = [segment for segment in segments if segment.broll_file]
    image_inputs: dict[str, int] = {}
    command = ["ffmpeg", "-y", "-i", str(video_path)]
    for position, segment in enumerate(image_segments, start=1):
        image_inputs[segment.broll_file] = position
        command.extend(["-loop", "1", "-i", str(broll_dir / segment.broll_file)])

    if burn_subtitles:
        shutil.copyfile(srt_path, output_dir / "captions_render.srt")

    filter_complex, video_label, audio_label = build_filter_complex(
        segments=segments,
        include_audio=include_audio,
        image_inputs=image_inputs,
        burn_subtitles=burn_subtitles,
        fps=fps,
    )
    output_path = output_dir / "final_edit.mp4"
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{video_label}]",
            "-map",
            f"[{audio_label}]",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run(command, cwd=output_dir)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a 9:16 A-roll/B-roll edit from a single video and SRT file."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder containing exactly one .mp4 and one .srt file.",
    )
    parser.add_argument(
        "--overwrite-placeholders",
        action="store_true",
        help="Regenerate the PNG placeholders even if files already exist.",
    )
    parser.add_argument(
        "--burn-subs",
        action="store_true",
        help="Burn the provided SRT onto the final output.",
    )
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        raise SystemExit(f"{folder} is not a folder.")

    video_path = discover_single_file(folder, (".mp4", ".mov", ".m4v"))
    srt_path = discover_single_file(folder, (".srt",))
    duration, fps = probe_video(video_path)
    cues = parse_srt(srt_path)
    segments = build_segments(cues, duration)
    assign_templates(segments)

    broll_dir = folder / "B_roll"
    output_dir = folder / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    create_placeholder_images(
        broll_dir=broll_dir,
        segments=segments,
        overwrite=args.overwrite_placeholders,
    )
    remove_unused_broll_files(broll_dir, segments)
    write_edit_plan(output_dir / "edit_plan.json", video_path, srt_path, segments)
    output_path = render_video(
        folder=folder,
        video_path=video_path,
        srt_path=srt_path,
        output_dir=output_dir,
        broll_dir=broll_dir,
        segments=segments,
        fps=fps,
        include_audio=has_audio_stream(video_path),
        burn_subtitles=args.burn_subs,
    )
    print(f"Rendered {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
