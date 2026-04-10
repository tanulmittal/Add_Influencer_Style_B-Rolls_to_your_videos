# Insta Reel Edit

Template-based `ffmpeg` workflow for editing 9:16 talking-head reels using:
- one A-roll video
- one `.srt` file for timing/reference
- local B-roll PNG assets

The current workflow is implemented in [build_srt_edit.py](/Users/tanulmittal/Documents/Code/Video%20Editing/build_srt_edit.py).

## What It Does

The editor cuts a vertical source video into short shots and applies these templates:

- `template_1`: full A-roll
- `template_2`: top B-roll strip with A-roll shifted lower
- `template_3`: full B-roll

Default edit formula:

`template_1 -> 3 -> 1 -> 2 -> 3 -> 1 -> 2 -> 3 -> 2 -> 1 -> 3 -> 1 -> 2 -> 3 -> 1 -> 2 -> 3 -> 2 -> ... -> template_1`

Numeric mapping:

- `1 = template_1`
- `2 = template_2`
- `3 = template_3`

The `.srt` is used for segmentation and naming reference by default. Subtitles are not burned unless explicitly enabled.

## Recommended Repo Structure

```text
insta-reel-edit/
├── build_srt_edit.py
├── README.md
├── skills/
│   └── insta-reel-edit/
│       ├── SKILL.md
│       └── agents/
│           └── openai.yaml
└── examples/
    └── project-template/
        ├── input/
        │   ├── video.mp4
        │   └── captions.srt
        ├── B_roll/
        └── output/
```

For real use, each project folder should look like:

```text
project-name/
├── source.mp4
├── source.srt
├── B_roll/
└── output/
```

## Requirements

- Python 3
- `ffmpeg`
- `ffprobe`

On macOS with Homebrew:

```bash
brew install ffmpeg
```

## How To Run

From the repo root:

```bash
python3 build_srt_edit.py "<project-folder>"
```

Example:

```bash
python3 build_srt_edit.py "ChatGpt $100"
```

Current output files:

- `output/final_edit.mp4`
- `output/edit_plan.json`

## How B-roll Works

- Put your replacement B-roll PNG files inside `B_roll/`
- Match the exact filenames expected in `output/edit_plan.json`
- `template_2` and `template_3` shots use those PNGs
- Missing files can be replaced by generated placeholders

Important:

- `.label_cache/` is only for placeholder label text
- The actual render uses PNGs from `B_roll/`

## Motion

For still-image B-roll, the workflow animates each shot with a centered zoom from `1.0x` to `1.2x` across the shot duration.

## Installing The Codex Skill

The portable skill copy should live in:

```text
skills/insta-reel-edit/
```

To install it on any machine:

```bash
mkdir -p ~/.codex/skills
cp -R skills/insta-reel-edit ~/.codex/skills/
```

Then use it in Codex like this:

```text
Use $insta-reel-edit to rerender this reel after I update B-roll.
```

## Suggested Git Strategy

Commit:

- `build_srt_edit.py`
- `README.md`
- `skills/insta-reel-edit/`

Usually do not commit:

- source videos
- exported videos
- large image assets
- generated caches

## Push To GitHub

```bash
git init
git add build_srt_edit.py README.md skills
git commit -m "Add insta reel edit workflow"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

On another device:

```bash
git clone <your-github-repo-url>
cd <repo-name>
brew install ffmpeg
python3 build_srt_edit.py "<project-folder>"
```
