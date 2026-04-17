#!/usr/bin/env python3
from __future__ import annotations

import functools
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from groq import Groq


@dataclass
class PromptEntry:
    segment_index: int
    template: str
    broll_file: str
    segment_text: str
    prompt: str


TEMPLATE_2_FRAMING = (
    "wide horizontal composition designed for a clean top-strip banner crop above talking-head footage, "
    "clear subject placement with balanced negative space and a clean upper frame"
)
TEMPLATE_3_FRAMING = (
    "full-scene vertical composition for immersive full-screen 9:16 b-roll, "
    "cinematic depth, strong environmental storytelling, and clean crop-safe framing"
)
DEFAULT_FRAMING = (
    "clean full-frame composition with a clear focal subject and flexible crop-safe framing"
)
DEFAULT_GROQ_PROMPT_MODEL = "openai/gpt-oss-20b"
REPO_ROOT = Path(__file__).resolve().parent
DISALLOWED_RESPONSE_PATTERNS = (
    r"\b[\w-]+\.(?:mp4|srt|py|json|md|png)\b",
    r"\b(?:university|campus|scholarship|admissions|classroom)\b",
)
DISALLOWED_RESPONSE_PHRASES = (
    "folder named",
    "file named",
    "labeled ",
    "labelled ",
    "readable text",
    "ui copy",
)


def load_edit_plan(edit_plan_path: Path) -> dict:
    if not edit_plan_path.is_file():
        raise SystemExit(f"Missing edit plan: {edit_plan_path}")

    try:
        payload = json.loads(edit_plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {edit_plan_path}: {exc}") from exc

    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise SystemExit(f"Malformed edit plan {edit_plan_path}: missing 'segments' list.")
    return payload


@functools.lru_cache(maxsize=1)
def load_local_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sanitize_context_text(text: str) -> str:
    normalized = normalize_text(text)
    replacements = {
        "video.mp4": "a source video",
        "audio.srt": "a generated subtitle transcript",
        "transcript.srt": "a generated subtitle transcript",
        "create.py": "the main build script",
        "recreate.py": "the rerender script",
        "9-16": "9:16",
        "p-roll": "B-roll",
    }
    lowered = normalized.lower()
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    return re.sub(r"\s+", " ", lowered).strip()


def truncate_text(text: str, limit: int) -> str:
    normalized = normalize_text(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def framing_for_template(template: str) -> str:
    if template == "template_2":
        return TEMPLATE_2_FRAMING
    if template == "template_3":
        return TEMPLATE_3_FRAMING
    return DEFAULT_FRAMING


def groq_prompt_model() -> str:
    load_local_env()
    return os.environ.get("GROQ_PROMPT_MODEL", DEFAULT_GROQ_PROMPT_MODEL)


@functools.lru_cache(maxsize=1)
def get_groq_client() -> Groq:
    load_local_env()
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise SystemExit("GROQ_API_KEY is not set.")
    return Groq(api_key=api_key)


def flatten_message_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(part.strip() for part in parts if part.strip()).strip()
    return ""


def sanitize_remote_prompt(content: str, fallback_prompt: str) -> str:
    prompt = content.strip()
    if not prompt:
        return fallback_prompt
    prompt = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", prompt).strip()
    prompt = re.sub(r"\s*```$", "", prompt).strip()
    prompt = prompt.strip().strip('"').strip("'").strip()
    prompt = re.sub(r"\s+", " ", prompt)
    if not prompt:
        return fallback_prompt
    lower_prompt = prompt.lower()
    if any(phrase in lower_prompt for phrase in DISALLOWED_RESPONSE_PHRASES):
        return fallback_prompt
    if any(re.search(pattern, prompt, re.IGNORECASE) for pattern in DISALLOWED_RESPONSE_PATTERNS):
        return fallback_prompt
    if not prompt.endswith("."):
        prompt += "."
    return prompt


def build_project_context(segments: list[dict]) -> str:
    context_parts: list[str] = []
    for raw_segment in segments:
        text = sanitize_context_text(str(raw_segment.get("text", "")))
        if not text or text in context_parts:
            continue
        context_parts.append(text)
        if len(" | ".join(context_parts)) >= 500:
            break
    return truncate_text(" | ".join(context_parts), 500)


def find_neighbor_text(segments: list[dict], start_index: int, step: int) -> str:
    cursor = start_index + step
    while 0 <= cursor < len(segments):
        candidate = sanitize_context_text(str(segments[cursor].get("text", "")))
        if candidate:
            return candidate
        cursor += step
    return ""


def build_fallback_prompt(
    segment_text: str,
    template: str,
    previous_text: str,
    next_text: str,
    project_context: str,
) -> str:
    framing = framing_for_template(template)
    context_bits = [f"centered on the spoken beat: {segment_text}"]
    if previous_text:
        context_bits.append(f"story beat before this: {truncate_text(previous_text, 120)}")
    if next_text:
        context_bits.append(f"story beat after this: {truncate_text(next_text, 120)}")
    if project_context:
        context_bits.append(f"overall project context: {project_context}")
    return (
        "Photorealistic cinematic B-roll of a creator or developer workflow, "
        + ", ".join(context_bits)
        + f", with {framing}. Show recording, editing, rendering, laptop, phone, or desk-based production energy in a realistic workspace. "
        "Keep the visual literal to the software or tutorial context, with clean composition, natural cinematic lighting, and no readable text, logos, or watermark."
    )


def build_groq_prompt(
    segment_text: str,
    template: str,
    previous_text: str,
    next_text: str,
    project_context: str,
) -> str:
    fallback_prompt = build_fallback_prompt(
        segment_text=segment_text,
        template=template,
        previous_text=previous_text,
        next_text=next_text,
        project_context=project_context,
    )
    client = get_groq_client()
    messages = [
        {
            "role": "system",
            "content": (
                "You write single-paragraph image prompts for B-roll still generation. "
                "The spoken segment is the primary source of truth. "
                "Keep the scene literal to the line first, then add cinematic polish. "
                "Prefer creator, software, editing, developer, recording, laptop, phone, workspace, and tutorial-adjacent visuals "
                "when the segment is about repos, tools, code, editing, or product demos. "
                "Do not drift into unrelated stock themes like campuses, scholarships, admissions, classrooms, or generic student success "
                "unless those are explicitly present in the provided transcript context. "
                "Do not include readable text, UI copy, logos, watermarks, posters, or signage. "
                "Return only the final prompt text."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Current segment: {segment_text}\n"
                f"Previous segment: {previous_text or 'None'}\n"
                f"Next segment: {next_text or 'None'}\n"
                f"Project context: {project_context or 'None'}\n"
                f"Framing requirement: {framing_for_template(template)}\n"
                "Write a literal-but-cinematic image prompt for this segment. "
                "If the line references a software repo, editing workflow, subtitles, rendering, B-roll, or creator tooling, "
                "the visual should stay inside that world. "
                "Describe screens and interfaces abstractly. "
                "Do not mention filenames, script names, folder names, commands, quoted labels, readable screen text, logos, or watermark."
            ),
        },
    ]

    try:
        response = client.chat.completions.create(
            model=groq_prompt_model(),
            messages=messages,
        )
        message = response.choices[0].message
        content = flatten_message_content(message.content)
        return sanitize_remote_prompt(content, fallback_prompt)
    except Exception as exc:
        print(
            "Warning: Groq prompt generation failed; using deterministic fallback prompt."
            + f" Last error: {exc}",
            file=sys.stderr,
        )
        return fallback_prompt


def build_prompt(
    segment_text: str,
    template: str,
    previous_text: str = "",
    next_text: str = "",
    project_context: str = "",
) -> str:
    return build_groq_prompt(
        segment_text=segment_text,
        template=template,
        previous_text=previous_text,
        next_text=next_text,
        project_context=project_context,
    )


def build_broll_prompt_entries(edit_plan: dict) -> list[PromptEntry]:
    segments = edit_plan.get("segments")
    if not isinstance(segments, list):
        raise SystemExit("Malformed edit plan: missing 'segments' list.")

    project_context = build_project_context(
        [segment for segment in segments if isinstance(segment, dict)]
    )
    entries: list[PromptEntry] = []
    raw_entries = [
        raw_segment
        for raw_segment in segments
        if isinstance(raw_segment, dict) and raw_segment.get("broll_file")
    ]
    if raw_entries:
        print(
            f"Generating {len(raw_entries)} B-roll prompts with Groq...",
            file=sys.stderr,
            flush=True,
        )

    for raw_index, raw_segment in enumerate(segments):
        if not isinstance(raw_segment, dict):
            raise SystemExit("Malformed edit plan: each segment must be an object.")

        broll_file = raw_segment.get("broll_file")
        if not broll_file:
            continue

        template = str(raw_segment.get("template", ""))
        segment_text = sanitize_context_text(str(raw_segment.get("text", "")))
        previous_text = find_neighbor_text(segments, raw_index, -1)
        next_text = find_neighbor_text(segments, raw_index, 1)
        print(
            f"Generating prompt for {broll_file}...",
            file=sys.stderr,
            flush=True,
        )
        entries.append(
            PromptEntry(
                segment_index=int(raw_segment.get("index", 0)),
                template=template,
                broll_file=str(broll_file),
                segment_text=segment_text,
                prompt=build_prompt(
                    segment_text=segment_text,
                    template=template,
                    previous_text=previous_text,
                    next_text=next_text,
                    project_context=project_context,
                ),
            )
        )

    return entries


def write_broll_prompts_markdown(output_path: Path, entries: list[PromptEntry]) -> None:
    lines = ["# B-roll Prompts", ""]
    for entry in entries:
        lines.append(f"## {entry.broll_file}")
        lines.append(entry.prompt)
        lines.append("")
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def generate_broll_prompts_for_project(project_dir: Path) -> Path:
    edit_plan_path = project_dir / "output" / "edit_plan.json"
    edit_plan = load_edit_plan(edit_plan_path)
    entries = build_broll_prompt_entries(edit_plan)
    output_path = project_dir / "output" / "broll_prompts.md"
    write_broll_prompts_markdown(output_path, entries)
    return output_path
