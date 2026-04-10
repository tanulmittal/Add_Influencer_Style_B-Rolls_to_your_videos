# Project Template

Put exactly one source video and exactly one SRT file in this folder.

Example:

- `my-reel.mp4`
- `my-reel.srt`

The render script expects this structure:

```text
project-template/
├── my-reel.mp4
├── my-reel.srt
├── B_roll/
└── output/
```

Run from the repo root:

```bash
python3 build_srt_edit.py "project-template"
```

Put replacement B-roll PNG files inside `project-template/B_roll/`.
