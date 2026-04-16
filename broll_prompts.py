#!/usr/bin/env python3
from __future__ import annotations

import functools
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from openai import APIError, OpenAI, RateLimitError


@dataclass
class PromptEntry:
    segment_index: int
    template: str
    broll_file: str
    segment_text: str
    prompt: str


SCHOLARSHIP_TERMS = (
    "scholarship",
    "scholarships",
    "merit",
    "funding",
    "funded",
    "financial aid",
    "percent",
    "%",
)
UNIVERSITY_TERMS = (
    "university",
    "universities",
    "campus",
    "australia",
    "australian",
    "anu",
    "go8",
    "monash",
    "sydney",
    "destination",
    "global",
    "world-class",
)
GUIDANCE_TERMS = (
    "guidance",
    "support",
    "application",
    "apply",
    "shortlisting",
    "course",
    "counselor",
    "counsellor",
    "advisor",
    "adviser",
    "help",
)
EVENT_TERMS = (
    "event",
    "join",
    "office",
    "meet",
    "visit",
    "april",
    "am",
    "pm",
    "day",
)
CLARITY_TERMS = (
    "confused",
    "clarity",
    "future",
    "opportunity",
    "upgrade",
    "chance",
    "planning",
    "realize",
)
CTA_TERMS = (
    "follow",
    "more",
    "scrolling",
    "stop scrolling",
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

STYLE_SUFFIX = (
    "photorealistic commercial photography, premium education marketing campaign, "
    "cinematic natural lighting, aspirational student-focused mood, polished realistic details, "
    "clean composition, no text, no logos, no watermark"
)
TEMPLATE_2_FRAMING = (
    "wide horizontal composition designed for a clean top-strip banner crop above talking-head footage, "
    "strong subject placement with clear negative space and balanced upper-frame composition"
)
TEMPLATE_3_FRAMING = (
    "full-scene vertical composition for immersive full-screen 9:16 b-roll, "
    "rich depth, cinematic framing, strong storytelling through environment"
)
DEFAULT_FRAMING = (
    "full-frame composition with clear focal subject and flexible crop-safe framing"
)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemma-4-26b-a4b-it:free"
DEFAULT_OPENROUTER_TIMEOUT_SECONDS = 8.0
REPO_ROOT = Path(__file__).resolve().parent


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
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        if " " in term or not term.isalnum():
            if term in text:
                return True
            continue
        if re.search(rf"\b{re.escape(term)}\b", text):
            return True
    return False


def build_project_context(text: str) -> str:
    context_bits: list[str] = []
    lower = text.lower()

    if "anu" in lower:
        context_bits.append(
            "an ambitious student in an elite Australian university setting inspired by Canberra"
        )
    elif "sydney" in lower:
        context_bits.append(
            "students in a prestigious urban Australian campus environment inspired by Sydney"
        )
    elif "monash" in lower:
        context_bits.append(
            "students in a modern high-achievement Australian campus setting inspired by Monash"
        )
    elif "go8" in lower:
        context_bits.append(
            "a premium Australian Group of Eight university atmosphere"
        )
    elif "australia" in lower or "australian" in lower:
        context_bits.append(
            "an aspirational Australian higher-education environment"
        )

    if "planet education" in lower:
        context_bits.append(
            "a professional education consultancy environment without visible branding"
        )

    return ", ".join(context_bits)


def build_scene_description(segment_text: str) -> str:
    lower = normalize_text(segment_text).lower()
    project_context = build_project_context(lower)
    context_suffix = f", {project_context}" if project_context else ""

    if contains_any(lower, SCHOLARSHIP_TERMS):
        return (
            "a confident prospective student reviewing scholarship success and admissions options "
            "with an expert advisor in a polished counseling session"
            f"{context_suffix}"
        )

    if contains_any(lower, UNIVERSITY_TERMS):
        return (
            "ambitious students experiencing a world-class campus environment, walking through a "
            "premium academic setting that feels globally respected and future-focused"
            f"{context_suffix}"
        )

    if contains_any(lower, GUIDANCE_TERMS):
        return (
            "a one-on-one admissions planning session with a student and advisor reviewing course "
            "options on a laptop in a bright modern consultation space"
            f"{context_suffix}"
        )

    if contains_any(lower, EVENT_TERMS) or contains_any(lower, MONTH_TERMS):
        return (
            "an energetic university admissions event scene with students checking in, meeting "
            "advisors, and exploring opportunities in a welcoming consultation venue"
            f"{context_suffix}"
        )

    if contains_any(lower, CLARITY_TERMS):
        return (
            "a focused student in a moment of clarity and motivation, planning their academic "
            "future with confidence in a premium study-oriented environment"
            f"{context_suffix}"
        )

    if contains_any(lower, CTA_TERMS):
        return (
            "a modern student browsing education opportunities on a phone or laptop in a clean, "
            "aspirational study space with no readable screen text"
            f"{context_suffix}"
        )

    return (
        "an aspirational student success scene in a premium higher-education environment, "
        "capturing ambition, opportunity, and forward momentum"
        f"{context_suffix}"
    )


def framing_for_template(template: str) -> str:
    if template == "template_2":
        return TEMPLATE_2_FRAMING
    if template == "template_3":
        return TEMPLATE_3_FRAMING
    return DEFAULT_FRAMING


def build_rule_based_prompt(segment_text: str, template: str) -> str:
    scene_description = build_scene_description(segment_text)
    framing = framing_for_template(template)
    return (
        f"Photorealistic cinematic marketing image of {scene_description}, "
        f"{framing}, {STYLE_SUFFIX}."
    )


def prompt_provider() -> str:
    load_local_env()
    provider = os.environ.get("BROLL_PROMPT_PROVIDER", "").strip().lower()
    if provider in {"openrouter", "rule-based"}:
        return provider
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    return "rule-based"


def openrouter_model() -> str:
    load_local_env()
    return os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)


def openrouter_fallback_models() -> list[str]:
    load_local_env()
    raw_value = os.environ.get("OPENROUTER_FALLBACK_MODELS", "")
    return [part.strip() for part in raw_value.split(",") if part.strip()]


def openrouter_timeout_seconds() -> float:
    load_local_env()
    raw_value = os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "").strip()
    if not raw_value:
        return DEFAULT_OPENROUTER_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw_value))
    except ValueError:
        return DEFAULT_OPENROUTER_TIMEOUT_SECONDS


@functools.lru_cache(maxsize=1)
def get_openrouter_client() -> OpenAI:
    load_local_env()
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set.")
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        max_retries=0,
    )


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


def sanitize_openrouter_prompt(content: str, fallback_prompt: str) -> str:
    prompt = content.strip()
    if not prompt:
        return fallback_prompt
    prompt = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", prompt).strip()
    prompt = re.sub(r"\s*```$", "", prompt).strip()
    prompt = prompt.strip().strip('"').strip("'").strip()
    prompt = re.sub(r"\s+", " ", prompt)
    if not prompt:
        return fallback_prompt
    if not prompt.endswith("."):
        prompt += "."
    return prompt


def build_openrouter_prompt(segment_text: str, template: str) -> str:
    fallback_prompt = build_rule_based_prompt(segment_text, template)
    scene_description = build_scene_description(segment_text)
    framing = framing_for_template(template)
    client = get_openrouter_client()
    models_to_try = [openrouter_model(), *openrouter_fallback_models()]
    messages = [
        {
            "role": "system",
            "content": (
                "You write single-paragraph prompts for AI image generation. "
                "Return only the final prompt text. "
                "Do not use markdown, bullets, labels, JSON, or explanations. "
                "Never request readable text, dates, logos, posters, watermarks, or branded signage inside the image. "
                "Keep prompts photorealistic, cinematic, aspirational, and suitable for education marketing B-roll."
            ),
        },
        {
            "role": "user",
            "content": (
                "Rewrite this into a stronger image-generation prompt.\n"
                f"Segment text: {segment_text}\n"
                f"Visual direction: {scene_description}\n"
                f"Framing requirement: {framing}\n"
                "Hard requirements: model-agnostic wording, premium commercial photography look, "
                "clean composition, natural cinematic lighting, student-focused scene, "
                "no text, no logos, no watermark.\n"
                "Return exactly one final prompt."
            ),
        },
    ]

    last_error: str | None = None
    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                timeout=openrouter_timeout_seconds(),
                extra_body={"reasoning": {"enabled": True}},
            )
            message = response.choices[0].message
            content = flatten_message_content(message.content)
            return sanitize_openrouter_prompt(content, fallback_prompt)
        except (RateLimitError, APIError) as exc:
            last_error = f"{model_name}: {exc}"
            continue

    print(
        "Warning: OpenRouter prompt generation failed; using local fallback prompt builder."
        + (f" Last error: {last_error}" if last_error else ""),
        file=sys.stderr,
    )
    return fallback_prompt


def build_prompt(segment_text: str, template: str) -> str:
    if prompt_provider() == "openrouter":
        return build_openrouter_prompt(segment_text, template)
    return build_rule_based_prompt(segment_text, template)


def build_broll_prompt_entries(edit_plan: dict) -> list[PromptEntry]:
    segments = edit_plan.get("segments")
    if not isinstance(segments, list):
        raise SystemExit("Malformed edit plan: missing 'segments' list.")

    raw_entries = [
        raw_segment
        for raw_segment in segments
        if isinstance(raw_segment, dict) and raw_segment.get("broll_file")
    ]
    if prompt_provider() == "openrouter" and raw_entries:
        print(
            f"Generating {len(raw_entries)} B-roll prompts with OpenRouter...",
            file=sys.stderr,
            flush=True,
        )

    entries: list[PromptEntry] = []
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            raise SystemExit("Malformed edit plan: each segment must be an object.")

        broll_file = raw_segment.get("broll_file")
        if not broll_file:
            continue

        template = str(raw_segment.get("template", ""))
        segment_text = normalize_text(str(raw_segment.get("text", "")))
        if prompt_provider() == "openrouter":
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
                prompt=build_prompt(segment_text, template),
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
