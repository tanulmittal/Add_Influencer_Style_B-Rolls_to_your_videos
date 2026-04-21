#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from broll_prompts import (
    generate_broll_prompts_for_project,
    get_groq_client,
)

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
SUBTITLE_ACTIVE_TEXT_FILL = (255, 232, 115, 255)
BROLL_START_ZOOM = 1.0
BROLL_END_ZOOM = 1.05
MIN_EDGE_DURATION = 1.0
MAX_EDGE_DURATION = 3.0
MIDDLE_BROLL_DURATION = 2.0
GROQ_WORD_TRANSCRIPT_MODEL = "whisper-large-v3-turbo"
WORD_TRANSCRIPT_LANGUAGE = "en"
MIN_WORD_DURATION_SECONDS = 0.01
WORD_BREAK_GAP_SECONDS = 0.45
WORD_BREAK_MAX_WORDS = 7
WORD_BREAK_MAX_CHARS = 36
OPENING_THUMBNAIL_FILE = "thumbnail.png"
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
class WordToken:
    word: str
    start: float
    end: float


@dataclass
class SubtitleCard:
    start: float
    end: float
    image_path: Path


class GroqTranscriptionError(RuntimeError):
    pass


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


def normalize_word_text(text: str) -> str:
    cleaned = clean_caption_text(text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def normalize_transcript_language(value: str) -> str:
    cleaned = str(value).strip().lower().replace("_", "-")
    aliases = {
        "english": "en",
    }
    return aliases.get(cleaned, cleaned)


def resolve_subtitle_model(subtitle_model: str | None) -> str:
    return subtitle_model or GROQ_WORD_TRANSCRIPT_MODEL


def format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


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

    thumbnail_path = broll_dir / OPENING_THUMBNAIL_FILE
    if overwrite or not thumbnail_path.exists():
        create_placeholder_image_with_pillow(
            output_path=thumbnail_path,
            label="THUMBNAIL",
            font_path=font_path,
            font_size=36,
            width=WIDTH,
            height=HEIGHT,
        )

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
        placeholder_width, placeholder_height = broll_dimensions_for_template(segment.template)
        create_placeholder_image_with_pillow(
            output_path=output_path,
            label=label,
            font_path=font_path,
            font_size=font_size,
            width=placeholder_width,
            height=placeholder_height,
        )


def measure_text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def measure_text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    _, top, _, bottom = draw.textbbox((0, 0), text, font=font)
    return bottom - top


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


def wrap_words_to_lines(
    words: list[str],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[list[tuple[int, str]]]:
    if not words:
        return []

    space_width = measure_text_width(draw, " ", font)
    lines: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_width = 0
    for index, word in enumerate(words):
        word_width = measure_text_width(draw, word, font)
        projected = word_width if not current else current_width + space_width + word_width
        if current and projected > max_width:
            lines.append(current)
            current = [(index, word)]
            current_width = word_width
            continue
        current.append((index, word))
        current_width = projected
    if current:
        lines.append(current)
    return lines


def render_highlighted_subtitle_card(
    output_path: Path,
    words: list[str],
    active_index: int,
    font_path: str,
) -> None:
    scratch = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scratch)
    font = ImageFont.truetype(font_path, SUBTITLE_FONT_SIZE)
    wrapped_lines = wrap_words_to_lines(words, draw, font, SUBTITLE_TEXT_MAX_WIDTH)
    if not wrapped_lines:
        raise SystemExit("Cannot render subtitle card with no words.")

    space_width = measure_text_width(draw, " ", font)
    line_height = measure_text_height(draw, "Ag", font)
    line_widths: list[int] = []
    for line in wrapped_lines:
        width = 0
        for position, (_, word) in enumerate(line):
            width += measure_text_width(draw, word, font)
            if position < len(line) - 1:
                width += space_width
        line_widths.append(width)

    image_width = math.ceil(max(line_widths) + SUBTITLE_PADDING_X * 2)
    image_height = math.ceil(
        line_height * len(wrapped_lines)
        + SUBTITLE_LINE_SPACING * max(0, len(wrapped_lines) - 1)
        + SUBTITLE_PADDING_Y * 2
    )

    image = Image.new("RGBA", (image_width, image_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, image_width - 1, image_height - 1),
        radius=SUBTITLE_CORNER_RADIUS,
        fill=SUBTITLE_BOX_FILL,
    )
    y = SUBTITLE_PADDING_Y
    for line, line_width in zip(wrapped_lines, line_widths):
        x = (image_width - line_width) / 2
        for position, (word_index, word) in enumerate(line):
            fill = SUBTITLE_ACTIVE_TEXT_FILL if word_index == active_index else SUBTITLE_TEXT_FILL
            draw.text((x, y), word, font=font, fill=fill)
            x += measure_text_width(draw, word, font)
            if position < len(line) - 1:
                x += space_width
        y += line_height + SUBTITLE_LINE_SPACING
    image.save(output_path)


def build_cue_subtitle_cards(subtitle_dir: Path, cues: list[Cue]) -> list[SubtitleCard]:
    subtitle_dir.mkdir(parents=True, exist_ok=True)
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


def should_refresh_transcript_cache(
    cache_path: Path,
    source_path: Path,
    backend: str,
    model_name: str,
    language: str,
) -> bool:
    if not cache_path.is_file():
        return True
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True

    tokens = payload.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        return True
    if payload.get("backend") != backend:
        return True
    if payload.get("model") != model_name:
        return True
    if normalize_transcript_language(str(payload.get("language", ""))) != normalize_transcript_language(
        language
    ):
        return True
    return int(payload.get("source_mtime_ns", -1)) != source_path.stat().st_mtime_ns


def normalize_word_tokens(raw_tokens: object, source_name: str) -> list[WordToken]:
    if not isinstance(raw_tokens, list):
        raise ValueError(f"{source_name} is missing a tokens list.")

    tokens: list[WordToken] = []
    previous_end = -1.0
    for raw_token in raw_tokens:
        if not isinstance(raw_token, dict):
            raise ValueError(f"{source_name} contains a non-object token.")
        word = normalize_word_text(str(raw_token.get("word", "")))
        start = raw_token.get("start")
        end = raw_token.get("end")
        if not word:
            raise ValueError(f"{source_name} contains an empty word token.")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"{source_name} contains a token with non-numeric timing.")
        start_value = round(float(start), 3)
        end_value = round(float(end), 3)
        if end_value <= start_value:
            raise ValueError(f"{source_name} contains a token with non-positive duration.")
        if tokens and (start_value < previous_end or end_value < previous_end):
            raise ValueError(f"{source_name} contains non-monotonic word timings.")
        tokens.append(WordToken(word=word, start=start_value, end=end_value))
        previous_end = end_value
    if not tokens:
        raise ValueError(f"{source_name} did not contain any word tokens.")
    return tokens


def load_cached_word_tokens(cache_path: Path) -> list[WordToken]:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid transcript cache {cache_path}: {exc}") from exc
    return normalize_word_tokens(payload.get("tokens"), f"transcript cache {cache_path}")


def load_usable_cached_word_tokens(
    cache_path: Path,
    source_path: Path,
    backend: str,
    model_name: str,
    language: str,
) -> list[WordToken] | None:
    if should_refresh_transcript_cache(
        cache_path,
        source_path,
        backend,
        model_name,
        language,
    ):
        return None
    try:
        return load_cached_word_tokens(cache_path)
    except ValueError:
        return None


def save_word_tokens_cache(
    cache_path: Path,
    source_path: Path,
    backend: str,
    model_name: str,
    language: str,
    tokens: list[WordToken],
) -> None:
    payload = {
        "source_video": source_path.name,
        "source_mtime_ns": source_path.stat().st_mtime_ns,
        "backend": backend,
        "model": model_name,
        "language": normalize_transcript_language(language),
        "tokens": [asdict(token) for token in tokens],
    }
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def extract_transcription_audio(video_path: Path, audio_path: Path) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ]
    )


def coerce_transcription_words(raw_words: object) -> list[dict[str, object]]:
    if not isinstance(raw_words, list):
        raise GroqTranscriptionError("Groq transcription response did not include word timestamps.")

    coerced_words: list[dict[str, object]] = []
    for raw_word in raw_words:
        if hasattr(raw_word, "model_dump"):
            payload = raw_word.model_dump()
        elif isinstance(raw_word, dict):
            payload = raw_word
        else:
            payload = {
                "word": getattr(raw_word, "word", None),
                "start": getattr(raw_word, "start", None),
                "end": getattr(raw_word, "end", None),
            }
        coerced_words.append(payload)
    return coerced_words


def repair_groq_word_timings(raw_words: list[dict[str, object]]) -> list[dict[str, object]]:
    repaired_words: list[dict[str, object]] = []
    previous_end = -1.0
    for raw_word in raw_words:
        payload = dict(raw_word)
        start = payload.get("start")
        end = payload.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            start_value = round(float(start), 3)
            end_value = round(float(end), 3)
            if previous_end >= 0:
                start_value = max(start_value, previous_end)
            if end_value <= start_value:
                end_value = round(start_value + MIN_WORD_DURATION_SECONDS, 3)
            payload["start"] = start_value
            payload["end"] = end_value
            previous_end = end_value
        repaired_words.append(payload)
    return repaired_words


def coerce_transcription_segments(raw_segments: object) -> list[dict[str, object]]:
    if not isinstance(raw_segments, list):
        return []

    coerced_segments: list[dict[str, object]] = []
    for raw_segment in raw_segments:
        if hasattr(raw_segment, "model_dump"):
            payload = raw_segment.model_dump()
        elif isinstance(raw_segment, dict):
            payload = raw_segment
        else:
            payload = {
                "text": getattr(raw_segment, "text", None),
                "start": getattr(raw_segment, "start", None),
                "end": getattr(raw_segment, "end", None),
            }
        coerced_segments.append(payload)
    return coerced_segments


def normalize_segment_cues(raw_segments: object, source_name: str) -> list[Cue]:
    if not isinstance(raw_segments, list):
        raise ValueError(f"{source_name} is missing a segments list.")

    cues: list[Cue] = []
    previous_end = -1.0
    for index, raw_segment in enumerate(raw_segments, start=1):
        if not isinstance(raw_segment, dict):
            raise ValueError(f"{source_name} contains a non-object segment.")
        text = normalize_word_text(str(raw_segment.get("text", "")))
        start = raw_segment.get("start")
        end = raw_segment.get("end")
        if not text:
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"{source_name} contains a segment with non-numeric timing.")
        start_value = round(float(start), 3)
        end_value = round(float(end), 3)
        if cues:
            start_value = max(start_value, previous_end)
        if end_value <= start_value:
            end_value = round(start_value + MIN_WORD_DURATION_SECONDS, 3)
        cues.append(Cue(index=index, start=start_value, end=end_value, text=text))
        previous_end = end_value
    if not cues:
        raise ValueError(f"{source_name} did not contain any transcript segments.")
    return cues


def build_phrase_cues(tokens: list[WordToken]) -> list[Cue]:
    cues: list[Cue] = []
    for index, phrase in enumerate(group_word_phrases(tokens), start=1):
        cues.append(
            Cue(
                index=index,
                start=round(phrase[0].start, 3),
                end=round(phrase[-1].end, 3),
                text=" ".join(token.word for token in phrase),
            )
        )
    return cues


def parse_groq_transcript_response(transcription: object) -> tuple[str, list[WordToken], list[Cue]]:
    if hasattr(transcription, "model_dump"):
        payload = transcription.model_dump()
    elif isinstance(transcription, dict):
        payload = transcription
    else:
        payload = {}

    language = str(payload.get("language") or getattr(transcription, "language", "") or "").strip()
    if not language:
        language = WORD_TRANSCRIPT_LANGUAGE

    raw_words = payload.get("words")
    if raw_words is None:
        raw_words = getattr(transcription, "words", None)
    try:
        tokens = normalize_word_tokens(
            repair_groq_word_timings(coerce_transcription_words(raw_words)),
            "Groq transcription response",
        )
    except ValueError as exc:
        raise GroqTranscriptionError(str(exc)) from exc
    raw_segments = payload.get("segments")
    if raw_segments is None:
        raw_segments = getattr(transcription, "segments", None)
    try:
        cues = normalize_segment_cues(
            coerce_transcription_segments(raw_segments),
            "Groq transcription response",
        )
    except ValueError:
        cues = build_phrase_cues(tokens)
    return language, tokens, cues


def write_transcript_srt(transcript_path: Path, cues: list[Cue]) -> None:
    blocks = [
        "\n".join(
            [
                str(cue.index),
                f"{format_srt_timestamp(cue.start)} --> {format_srt_timestamp(cue.end)}",
                cue.text,
            ]
        )
        for cue in cues
    ]
    transcript_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def generate_transcript_artifacts(
    video_path: Path,
    output_dir: Path,
    model_name: str,
    language: str,
) -> tuple[str, list[WordToken], list[Cue]]:
    audio_path = output_dir / ".transcription_audio.wav"
    extract_transcription_audio(video_path, audio_path)
    try:
        client = get_groq_client()
        with audio_path.open("rb") as audio_file:
            try:
                transcription = client.audio.transcriptions.create(
                    file=(audio_path.name, audio_file.read()),
                    model=model_name,
                    temperature=0,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                    language=language,
                )
            except Exception as exc:
                raise GroqTranscriptionError("Groq transcription request failed.") from exc
        return parse_groq_transcript_response(transcription)
    finally:
        if audio_path.exists():
            audio_path.unlink()


def transcribe_word_tokens(
    video_path: Path,
    output_dir: Path,
    model_name: str,
    language: str,
) -> list[WordToken]:
    cache_path = output_dir / "word_timestamps.json"
    cached_tokens = load_usable_cached_word_tokens(
        cache_path,
        video_path,
        "groq",
        model_name,
        language,
    )
    if cached_tokens is not None:
        return cached_tokens

    detected_language, tokens, _ = generate_transcript_artifacts(
        video_path=video_path,
        output_dir=output_dir,
        model_name=model_name,
        language=language,
    )

    save_word_tokens_cache(
        cache_path,
        video_path,
        "groq",
        model_name,
        detected_language or language,
        tokens,
    )
    return tokens


def ensure_project_transcript(
    video_path: Path,
    output_dir: Path,
    model_name: str,
    language: str,
) -> list[Cue]:
    cache_path = output_dir / "word_timestamps.json"
    transcript_path = output_dir / "transcript.srt"
    if transcript_path.is_file():
        return parse_srt(transcript_path)

    detected_language, tokens, cues = generate_transcript_artifacts(
        video_path=video_path,
        output_dir=output_dir,
        model_name=model_name,
        language=language,
    )
    save_word_tokens_cache(
        cache_path,
        video_path,
        "groq",
        model_name,
        detected_language or language,
        tokens,
    )
    write_transcript_srt(transcript_path, cues)
    return cues


def should_break_word_phrase(current_tokens: list[WordToken], next_token: WordToken) -> bool:
    if not current_tokens:
        return False
    previous = current_tokens[-1]
    if next_token.start - previous.end >= WORD_BREAK_GAP_SECONDS:
        return True
    if len(current_tokens) >= WORD_BREAK_MAX_WORDS:
        return True
    projected_text = " ".join(token.word for token in [*current_tokens, next_token])
    if len(projected_text) > WORD_BREAK_MAX_CHARS:
        return True
    if re.search(r"[.!?,:;]$", previous.word):
        return True
    return False


def group_word_phrases(tokens: list[WordToken]) -> list[list[WordToken]]:
    phrases: list[list[WordToken]] = []
    current: list[WordToken] = []
    for token in tokens:
        if should_break_word_phrase(current, token):
            phrases.append(current)
            current = [token]
            continue
        current.append(token)
    if current:
        phrases.append(current)
    return phrases


def build_word_subtitle_cards(
    output_dir: Path,
    subtitle_dir: Path,
    video_path: Path,
    model_name: str,
    language: str,
) -> list[SubtitleCard]:
    subtitle_dir.mkdir(parents=True, exist_ok=True)

    tokens = transcribe_word_tokens(
        video_path=video_path,
        output_dir=output_dir,
        model_name=model_name,
        language=language,
    )
    font_path = pick_inter_font()
    subtitle_cards: list[SubtitleCard] = []
    card_index = 1
    for phrase in group_word_phrases(tokens):
        phrase_words = [token.word for token in phrase]
        for word_index, token in enumerate(phrase):
            frame_end = phrase[word_index + 1].start if word_index + 1 < len(phrase) else token.end
            if frame_end <= token.start:
                frame_end = max(token.end, token.start + 0.05)
            image_path = subtitle_dir / f"word_{card_index:04d}.png"
            render_highlighted_subtitle_card(
                image_path,
                phrase_words,
                word_index,
                font_path,
            )
            subtitle_cards.append(
                SubtitleCard(
                    start=round(token.start, 3),
                    end=round(frame_end, 3),
                    image_path=image_path,
                )
            )
            card_index += 1
    return subtitle_cards


def build_subtitle_cards(
    output_dir: Path,
    subtitle_dir: Path,
    video_path: Path,
    cues: list[Cue],
    subtitle_mode: str,
    subtitle_model: str,
    subtitle_language: str,
) -> list[SubtitleCard]:
    if subtitle_mode == "cue":
        return build_cue_subtitle_cards(subtitle_dir, cues)
    try:
        return build_word_subtitle_cards(
            output_dir=output_dir,
            subtitle_dir=subtitle_dir,
            video_path=video_path,
            model_name=subtitle_model,
            language=subtitle_language,
        )
    except GroqTranscriptionError as exc:
        print(
            f"Groq word transcription failed ({exc}). Falling back to cue-timed subtitles.",
            file=sys.stderr,
        )
        return build_cue_subtitle_cards(subtitle_dir, cues)


def broll_dimensions_for_template(template: str) -> tuple[int, int]:
    if template == "template_2":
        return WIDTH, TEMPLATE2_BROLL_HEIGHT
    return WIDTH, HEIGHT


def build_broll_motion_filter(
    input_index: int,
    output_label: str,
    output_width: int,
    output_height: int,
    duration: float,
    fps: int,
) -> str:
    safe_duration = max(duration, 0.001)
    progress_expr = f"min(1,max(0,t/{safe_duration:.6f}))"
    zoom_expr = (
        f"{BROLL_START_ZOOM:.3f}"
        f"+({BROLL_END_ZOOM - BROLL_START_ZOOM:.3f}*{progress_expr})"
    )
    cover_scale_expr = f"max({output_width}/iw,{output_height}/ih)"
    normalized_width_expr = f"2*ceil(iw*{cover_scale_expr}/2)"
    normalized_height_expr = f"2*ceil(ih*{cover_scale_expr}/2)"
    zoomed_width_expr = f"2*ceil(iw*({zoom_expr})/2)"
    zoomed_height_expr = f"2*ceil(ih*({zoom_expr})/2)"
    return (
        f"[{input_index}:v]"
        "loop=loop=-1:size=1:start=0,"
        f"fps={fps},"
        f"trim=duration={duration:.3f},"
        "setpts=PTS-STARTPTS,"
        f"scale=w='{normalized_width_expr}':"
        f"h='{normalized_height_expr}':"
        "flags=lanczos+accurate_rnd:"
        "eval=frame,"
        f"crop=w={output_width}:h={output_height}:"
        f"x='(iw-{output_width})/2':"
        f"y='(ih-{output_height})/2':"
        "exact=1,"
        f"scale=w='{zoomed_width_expr}':"
        f"h='{zoomed_height_expr}':"
        "flags=lanczos+accurate_rnd:"
        "eval=frame,"
        f"crop=w={output_width}:h={output_height}:"
        f"x='(iw-{output_width})/2':"
        f"y='(ih-{output_height})/2':"
        "exact=1,"
        f"setpts=PTS-STARTPTS[{output_label}]"
    )


def remove_unused_broll_files(broll_dir: Path, segments: list[Segment]) -> None:
    if not broll_dir.exists():
        return
    expected_files = {segment.broll_file for segment in segments if segment.broll_file}
    expected_files.add(OPENING_THUMBNAIL_FILE)
    for path in broll_dir.iterdir():
        if path.is_dir() and path.name.startswith("."):
            shutil.rmtree(path)
        elif path.is_file() and path.name not in expected_files:
            path.unlink()


def write_edit_plan(
    output_path: Path,
    video_path: Path,
    transcript_path: Path,
    segments: list[Segment],
) -> None:
    payload = {
        "video": video_path.name,
        "transcript": transcript_path.name,
        "formula": "template_1 + repeated relevant templates + template_1",
        "segments": [asdict(segment) for segment in segments],
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def rebuild_project_edit_plan(
    video_path: Path,
    output_dir: Path,
    cues: list[Cue],
) -> tuple[list[Segment], int]:
    duration, fps = probe_video(video_path)
    segments = build_segments(cues, duration)
    assign_templates(segments)
    write_edit_plan(
        output_dir / "edit_plan.json",
        video_path,
        output_dir / "transcript.srt",
        segments,
    )
    return segments, fps


def prepare_project_render_inputs(
    video_path: Path,
    output_dir: Path,
    model_name: str,
    language: str,
) -> tuple[list[Cue], list[Segment], int]:
    cues = ensure_project_transcript(
        video_path=video_path,
        output_dir=output_dir,
        model_name=model_name,
        language=language,
    )
    segments, fps = rebuild_project_edit_plan(video_path, output_dir, cues)
    return cues, segments, fps


def prepare_broll_dir(folder: Path, create: bool) -> Path:
    canonical_dir = folder / "broll"
    legacy_dir = folder / "B_roll"
    if canonical_dir.exists() and legacy_dir.exists():
        raise SystemExit(
            f"Both {canonical_dir.name}/ and {legacy_dir.name}/ exist in {folder}. Remove one layout and rerun."
        )
    if legacy_dir.exists():
        legacy_dir.rename(canonical_dir)
    if create:
        canonical_dir.mkdir(parents=True, exist_ok=True)
    return canonical_dir


def sync_broll_files_by_segment_index(broll_dir: Path, segments: list[Segment]) -> None:
    if not broll_dir.exists():
        return
    for segment in segments:
        if not segment.broll_file:
            continue
        expected_path = broll_dir / segment.broll_file
        if expected_path.exists():
            continue
        legacy_matches = sorted(
            path
            for path in broll_dir.glob(f"{segment.index:02d}_*.png")
            if path.is_file() and path != expected_path
        )
        if len(legacy_matches) == 1:
            legacy_matches[0].rename(expected_path)


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


def find_opening_thumbnail_broll_file(broll_dir: Path) -> str | None:
    thumbnail_path = broll_dir / OPENING_THUMBNAIL_FILE
    if thumbnail_path.is_file():
        return OPENING_THUMBNAIL_FILE
    return None


def build_opening_thumbnail_filter(
    input_label: str,
    output_label: str,
    frame_duration: float,
) -> str:
    return (
        f"{input_label}trim=duration={frame_duration:.6f},"
        "setpts=PTS-STARTPTS,"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1"
        f"[{output_label}]"
    )


def build_filter_complex(
    segments: list[Segment],
    include_audio: bool,
    image_inputs: dict[str, int],
    subtitle_cards: list[SubtitleCard],
    burn_subtitles: bool,
    fps: int,
    opening_thumbnail_broll_file: str | None,
) -> tuple[str, str, str]:
    filters: list[str] = []
    concat_parts: list[str] = []
    frame_duration = 1 / fps
    subtitle_offset = frame_duration

    if opening_thumbnail_broll_file and opening_thumbnail_broll_file in image_inputs:
        opening_input_index = image_inputs[opening_thumbnail_broll_file]
        filters.append(
            build_opening_thumbnail_filter(
                input_label=f"[{opening_input_index}:v]",
                output_label="thumb_v",
                frame_duration=frame_duration,
            )
        )
    else:
        filters.append(
            f"[0:v]trim=start=0:end={frame_duration:.6f},"
            "setpts=PTS-STARTPTS,"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
            "setsar=1[thumb_v]"
        )

    filters.append(
        f"anullsrc=r=48000:cl=stereo,atrim=duration={frame_duration:.6f}[thumb_a]"
    )
    concat_parts.append("[thumb_v][thumb_a]")

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
                build_broll_motion_filter(
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
                build_broll_motion_filter(
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
        + f"concat=n={len(segments) + 1}:v=1:a=1[concat_v][concat_a]"
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
                f"enable='between(t,{subtitle_card.start + subtitle_offset:.3f},{subtitle_card.end + subtitle_offset:.3f})'"
                f"[{next_label}]"
            )
            video_label = next_label
    return ";".join(filters), video_label, "concat_a"


def render_video(
    video_path: Path,
    output_dir: Path,
    broll_dir: Path,
    segments: list[Segment],
    subtitle_cards: list[SubtitleCard],
    fps: int,
    include_audio: bool,
) -> Path:
    image_segments = [segment for segment in segments if segment.broll_file]
    opening_thumbnail_broll_file = find_opening_thumbnail_broll_file(broll_dir)
    image_inputs: dict[str, int] = {}
    command = ["ffmpeg", "-y", "-i", str(video_path)]
    if opening_thumbnail_broll_file:
        image_inputs[opening_thumbnail_broll_file] = 1
        command.extend(["-loop", "1", "-i", str(broll_dir / opening_thumbnail_broll_file)])
    for position, segment in enumerate(image_segments, start=len(image_inputs) + 1):
        image_inputs[segment.broll_file] = position
        command.extend(["-loop", "1", "-i", str(broll_dir / segment.broll_file)])

    for card in subtitle_cards:
        image_inputs[str(card.image_path)] = len(image_inputs) + 1
        command.extend(["-loop", "1", "-i", str(card.image_path)])

    filter_complex, video_label, audio_label = build_filter_complex(
        segments=segments,
        include_audio=include_audio,
        image_inputs=image_inputs,
        subtitle_cards=subtitle_cards,
        burn_subtitles=bool(subtitle_cards),
        fps=fps,
        opening_thumbnail_broll_file=opening_thumbnail_broll_file,
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
        description="Build a 9:16 A-roll/B-roll edit from a single source video."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder containing exactly one source video.",
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

    video_path = discover_single_file(folder, (".mp4", ".mov", ".m4v"))
    output_dir = folder / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    subtitle_model = resolve_subtitle_model(args.subtitle_model)
    cues, segments, fps = prepare_project_render_inputs(
        video_path=video_path,
        output_dir=output_dir,
        model_name=subtitle_model,
        language=args.subtitle_language,
    )

    broll_dir = prepare_broll_dir(folder, create=True)

    create_placeholder_images(
        broll_dir=broll_dir,
        segments=segments,
        overwrite=args.overwrite_placeholders,
    )
    remove_unused_broll_files(broll_dir, segments)
    generate_broll_prompts_for_project(folder)
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
