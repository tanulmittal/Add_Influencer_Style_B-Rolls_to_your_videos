#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from broll_prompts import generate_broll_prompts_for_project


SKIP_DIR_NAMES = {"skills", "__pycache__"}


def project_candidates(repo_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(repo_root.iterdir()):
        if not path.is_dir():
            continue
        if path.name.startswith(".") or path.name in SKIP_DIR_NAMES:
            continue
        candidates.append(path)
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate B-roll image prompts from output/edit_plan.json files."
    )
    parser.add_argument(
        "folders",
        nargs="*",
        help="Optional project folders to process. Defaults to scanning repo child folders.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    generated: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    if args.folders:
        for folder_name in args.folders:
            project_dir = (repo_root / folder_name).resolve()
            edit_plan_path = project_dir / "output" / "edit_plan.json"
            if not project_dir.is_dir():
                print(f"Missing project folder: {folder_name}", file=sys.stderr)
                return 1
            if not edit_plan_path.is_file():
                print(
                    f"Missing edit plan for {folder_name}: {edit_plan_path}",
                    file=sys.stderr,
                )
                return 1
            try:
                output_path = generate_broll_prompts_for_project(project_dir)
            except SystemExit as exc:
                print(str(exc), file=sys.stderr)
                return 1
            generated.append(f"{folder_name} -> {output_path.relative_to(repo_root)}")
    else:
        for project_dir in project_candidates(repo_root):
            edit_plan_path = project_dir / "output" / "edit_plan.json"
            if not edit_plan_path.is_file():
                skipped.append(project_dir.name)
                continue
            try:
                output_path = generate_broll_prompts_for_project(project_dir)
            except SystemExit as exc:
                failed.append(f"{project_dir.name}: {exc}")
                continue
            generated.append(f"{project_dir.name} -> {output_path.relative_to(repo_root)}")

    if generated:
        print("Generated prompt files:")
        for item in generated:
            print(f"- {item}")
    if skipped:
        print("Skipped folders without edit plans:")
        for item in skipped:
            print(f"- {item}")
    if failed:
        print("Failed folders:")
        for item in failed:
            print(f"- {item}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
