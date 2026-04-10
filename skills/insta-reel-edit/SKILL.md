---
name: insta-reel-edit
description: Create or adjust 9:16 talking-head edits built from one A-roll video, one SRT timing reference, and local B-roll PNG files using ffmpeg. Use when Codex needs to patch or rerun a workflow like `build_srt_edit.py`, manage template-based A-roll/B-roll layouts, regenerate `final_edit.mp4`, update timing or sequence rules, or handle B-roll placeholder and filename matching issues.
---

# Insta Reel Edit

## Overview

Use the existing local render script when the repo already has one, usually `build_srt_edit.py`. Patch the current workflow instead of rebuilding it from scratch unless the project is missing the script entirely.

Treat the `.srt` file as a timing and segmentation reference by default. Do not burn subtitles unless the user explicitly asks for them.

## Workflow

1. Find the current render script and inspect the active assumptions before editing anything.
   Prefer `rg --files -g 'build_srt_edit.py'`.
   Check the template layout constants, segment-duration constants, sequence logic, placeholder generation, and final render command.

2. Confirm the project layout.
   Expect one source video and one `.srt` inside the target folder.
   Expect `B_roll/` for B-roll images and `output/` for rendered files.
   Expect the final render at `output/final_edit.mp4` and the shot map at `output/edit_plan.json`.

3. Preserve the current template model unless the user asks to change it.
   `template_1`: full A-roll, 9:16.
   `template_2`: top-strip B-roll with shifted A-roll below it.
   `template_3`: full B-roll.

4. Preserve the current B-roll handling rules unless the user asks to change them.
   Match uploaded files by the exact PNG filenames listed in `output/edit_plan.json`.
   Use `.label_cache/` only for placeholder label text, not as actual B-roll input.
   Generate placeholders only for missing expected files.
   Keep placeholder dimensions aligned to the template layout.

5. Handle common user requests directly.
   If the user says "redo" or "rerender" after updating images, rerun the render without changing logic.
   If the user asks to change timing, patch the duration constants and segment-boundary logic, then rerender.
   If the user asks to change layout, patch the ffmpeg filter graph, then rerender.
   If the user asks to change the template order, patch the sequence constant or assignment logic, then rerender.
   If the user asks to change B-roll motion, patch the zoom or pan expressions in the image branch, then rerender.

6. Rerender with the project’s existing command pattern.

```bash
python3 build_srt_edit.py '<folder>'
```

Use overwrite-placeholder flags only when the user explicitly wants placeholder regeneration.

## Edit Formula

Use this default sequence unless the user asks for a different one:

`template_1 -> 3 -> 1 -> 2 -> 3 -> 1 -> 2 -> 3 -> 2 -> 1 -> 3 -> 1 -> 2 -> 3 -> 1 -> 2 -> 3 -> 2 -> ... -> template_1`

Treat the sequence above as the middle-pattern rule with `template_1` forced at both ends.

Map the numeric shorthand as:
`1 = template_1`
`2 = template_2`
`3 = template_3`

## Template Rules

Keep the start and end logic explicit. If the project already reserves `template_1` for the first and last shot, preserve that unless the user changes it.

Keep the middle template sequence deterministic when the user provides a pattern. Do not replace a user-specified pattern with heuristic alternation or randomization.

Keep shot durations short and editorially simple unless the user changes them. In this workflow, A-roll edge shots and B-roll middle shots are controlled by top-level constants in the script.

## Motion Rules

For animated B-roll stills, prefer `zoompan` on the image input rather than a static overscale.

Use a per-shot animation that starts at `1.0x` and ends at the configured end zoom across the shot duration. Center the zoom unless the user asks for directional motion.

When the user says the scale change "is not working," verify that the graph is generating motion over time and not just cropping a larger static image.

## Validation

After changes, rerender and verify the output exists.

Check:
`output/final_edit.mp4` renders successfully.
`output/edit_plan.json` still matches the expected template sequence and B-roll filenames.
The video keeps the expected `1080x1920` layout.
Updated B-roll assets from `B_roll/` are actually the files being read during render.

If the user is iterating on assets only, do not describe old placeholders as current inputs. Rerun the render and report the updated output path.
