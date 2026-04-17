#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from broll_prompts import generate_broll_prompts_for_project

WIDTH = 1080
HEIGHT = 1920
TEMPLATE2_BROLL_HEIGHT = int(HEIGHT * 0.4)
TEMPLATE2_AROLL_Y = int(HEIGHT * 0.3)
SUBTITLE_FONT_SIZE = 37
SUBTITLE_TEXT_MAX_WIDTH = int(WIDTH * 0.72)
SUBTITLE_PADDING_X = 28
SUBTITLE_PADDING_Y = 18
SUBTITLE_CORNER_RADIUS = 22
SUBTITLE_LINE_SPACING = 8
SUBTITLE_BOTTOM_MARGIN = int(HEIGHT * 0.2)
SUBTITLE_BOX_FILL = (0, 0, 0, 191)
SUBTITLE_TEXT_FILL = (255, 255, 255, 255)
BROLL_START_ZOOM = 1.0
BROLL_END_ZOOM = 1.1
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


@dataclass
class SubtitleCard:
    start: float
    end: float
    image_path: Path


def run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


@functools.lru_cache(maxsize=1)
def ffmpeg_has_filter(name: str) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        check=True,
    )
    return any(line.split()[-1] == name for line in result.stdout.splitlines() if line.strip())


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


def pick_placeholder_font() -> str | None:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def pick_inter_font() -> str:
    direct_candidates = [
        Path(__file__).resolve().parent / "fonts" / "Inter[opsz,wght].ttf",
        Path(__file__).resolve().parent / "fonts" / "Inter-Regular.ttf",
        Path(__file__).resolve().parent / "fonts" / "Inter.ttf",
        Path("/Library/Fonts/Inter-Regular.ttf"),
        Path("/Library/Fonts/Inter.ttf"),
        Path.home() / "Library" / "Fonts" / "Inter-Regular.ttf",
        Path.home() / "Library" / "Fonts" / "Inter.ttf",
        Path.home() / ".fonts" / "Inter-Regular.ttf",
        Path.home() / ".fonts" / "Inter.ttf",
    ]
    for candidate in direct_candidates:
        if candidate.exists():
            return str(candidate)

    search_dirs = [
        Path(__file__).resolve().parent / "fonts",
        Path("/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
        Path.home() / ".fonts",
    ]
    patterns = ("Inter*.ttf", "Inter*.otf", "Inter*.ttc")
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for pattern in patterns:
            for candidate in sorted(search_dir.glob(pattern)):
                if candidate.is_file():
                    return str(candidate)
    raise SystemExit(
        "Inter font not found. Install a local Inter .ttf file (for example "
        "/Library/Fonts/Inter-Regular.ttf or ~/Library/Fonts/Inter-Regular.ttf) "
        "and rerun the render."
    )


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


def create_placeholder_image_with_pillow(
    output_path: Path,
    label: str,
    font_path: str | None,
    font_size: int,
    width: int,
    height: int,
) -> None:
    image = Image.new("RGB", (width, height), "#111827")
    draw = ImageDraw.Draw(image)
    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        font = ImageFont.load_default()
    left, top, right, bottom = draw.multiline_textbbox(
        (0, 0),
        label,
        font=font,
        align="center",
        spacing=12,
    )
    text_width = right - left
    text_height = bottom - top
    position = ((width - text_width) / 2, (height - text_height) / 2)
    draw.multiline_text(
        position,
        label,
        font=font,
        fill="white",
        align="center",
        spacing=12,
    )
    image.save(output_path)


def create_placeholder_images(broll_dir: Path, segments: list[Segment], overwrite: bool) -> None:
    broll_dir.mkdir(parents=True, exist_ok=True)
    font_path = pick_placeholder_font()
    can_drawtext = bool(font_path) and ffmpeg_has_filter("drawtext")
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
        if not can_drawtext:
            create_placeholder_image_with_pillow(
                output_path=output_path,
                label=label,
                font_path=font_path,
                font_size=font_size,
                width=placeholder_width,
                height=placeholder_height,
            )
            continue
        drawtext = (
            "drawtext="
            f"fontfile='{font_path}':"
            f"textfile='{label_path}':"
            "fontcolor=white:"
            f"fontsize={font_size}:"
            "x=(w-text_w)/2:"
            "y=(h-text_h)/2:"
            "line_spacing=12"
        ) if can_drawtext else None
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


def measure_text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def wrap_text_to_pixel_width(
    text: str,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    words = text.split()
    if not words:
        return ""

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if measure_text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return "\n".join(lines)


def render_subtitle_card(
    output_path: Path,
    text: str,
    font_path: str,
) -> None:
    scratch = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scratch)
    font = ImageFont.truetype(font_path, SUBTITLE_FONT_SIZE)
    wrapped_text = wrap_text_to_pixel_width(text, draw, font, SUBTITLE_TEXT_MAX_WIDTH)
    left, top, right, bottom = draw.multiline_textbbox(
        (0, 0),
        wrapped_text,
        font=font,
        align="center",
        spacing=SUBTITLE_LINE_SPACING,
    )
    text_width = right - left
    text_height = bottom - top
    image_width = math.ceil(text_width + SUBTITLE_PADDING_X * 2)
    image_height = math.ceil(text_height + SUBTITLE_PADDING_Y * 2)

    image = Image.new("RGBA", (image_width, image_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, image_width - 1, image_height - 1),
        radius=SUBTITLE_CORNER_RADIUS,
        fill=SUBTITLE_BOX_FILL,
    )
    draw.multiline_text(
        ((image_width - text_width) / 2 - left, (image_height - text_height) / 2 - top),
        wrapped_text,
        font=font,
        fill=SUBTITLE_TEXT_FILL,
        align="center",
        spacing=SUBTITLE_LINE_SPACING,
    )
    image.save(output_path)


def build_subtitle_cards(output_dir: Path, cues: list[Cue]) -> list[SubtitleCard]:
    subtitle_dir = output_dir / "subtitle_cards"
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    for existing_file in subtitle_dir.glob("*.png"):
        existing_file.unlink()

    font_path = pick_inter_font()
    subtitle_cards: list[SubtitleCard] = []
    for cue in cues:
        cleaned_text = clean_caption_text(cue.text)
        if not cleaned_text or cue.end <= cue.start:
            continue
        image_path = subtitle_dir / f"cue_{cue.index:04d}.png"
        render_subtitle_card(image_path, cleaned_text, font_path)
        subtitle_cards.append(
            SubtitleCard(
                start=cue.start,
                end=cue.end,
                image_path=image_path,
            )
        )
    return subtitle_cards


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


def load_segments_from_edit_plan(edit_plan_path: Path) -> list[Segment]:
    try:
        payload = json.loads(edit_plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(
            f"Missing edit plan: {edit_plan_path}. Run create.py for this folder first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {edit_plan_path}: {exc}") from exc

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise SystemExit(f"Malformed edit plan {edit_plan_path}: missing 'segments' list.")

    segments: list[Segment] = []
    required_keys = ("index", "start", "end", "duration", "text", "template")
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise SystemExit(f"Malformed edit plan {edit_plan_path}: each segment must be an object.")
        missing_keys = [key for key in required_keys if key not in raw_segment]
        if missing_keys:
            joined = ", ".join(missing_keys)
            raise SystemExit(
                f"Malformed edit plan {edit_plan_path}: segment missing required keys: {joined}."
            )
        broll_file = raw_segment.get("broll_file")
        if broll_file is not None and not isinstance(broll_file, str):
            raise SystemExit(
                f"Malformed edit plan {edit_plan_path}: segment broll_file must be a string or null."
            )
        try:
            segments.append(
                Segment(
                    index=int(raw_segment["index"]),
                    start=float(raw_segment["start"]),
                    end=float(raw_segment["end"]),
                    duration=float(raw_segment["duration"]),
                    text=str(raw_segment["text"]),
                    template=str(raw_segment["template"]),
                    broll_file=broll_file,
                )
            )
        except (TypeError, ValueError) as exc:
            raise SystemExit(
                f"Malformed edit plan {edit_plan_path}: invalid segment field types."
            ) from exc

    return segments


def build_filter_complex(
    segments: list[Segment],
    include_audio: bool,
    image_inputs: dict[str, int],
    subtitle_cards: list[SubtitleCard],
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
        for subtitle_index, subtitle_card in enumerate(subtitle_cards):
            next_label = f"sub{subtitle_index}"
            input_index = image_inputs[str(subtitle_card.image_path)]
            filters.append(
                f"[{video_label}][{input_index}:v]"
                f"overlay=shortest=1:"
                f"x=(main_w-overlay_w)/2:"
                f"y=main_h-{SUBTITLE_BOTTOM_MARGIN}-overlay_h:"
                f"enable='between(t,{subtitle_card.start:.3f},{subtitle_card.end:.3f})'"
                f"[{next_label}]"
            )
            video_label = next_label
    return ";".join(filters), video_label, "concat_a"


def render_video(
    folder: Path,
    video_path: Path,
    srt_path: Path,
    output_dir: Path,
    broll_dir: Path,
    segments: list[Segment],
    cues: list[Cue],
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

    subtitle_cards: list[SubtitleCard] = []
    if burn_subtitles:
        subtitle_cards = build_subtitle_cards(output_dir, cues)
        for card in subtitle_cards:
            image_inputs[str(card.image_path)] = len(image_inputs) + 1
            command.extend(["-loop", "1", "-i", str(card.image_path)])

    filter_complex, video_label, audio_label = build_filter_complex(
        segments=segments,
        include_audio=include_audio,
        image_inputs=image_inputs,
        subtitle_cards=subtitle_cards,
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
    generate_broll_prompts_for_project(folder)
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
