# Add Influencer Style B-Rolls to your videos

Add Influencer Style - B-Rolls to your videos (current support 9:16 videos)

## Preview

### Before

![Before edit](assets/readme/before.gif)

### After

![After edit](assets/readme/after.gif)

Create an edited video using:

- `video.mp4`
- `audio.srt`

## Simple Flow

1. Create a project folder inside this repo.
2. Add these files inside that folder:

```text
your-project/
├── video.mp4
└── audio.srt
```

3. Run:

```bash
python3 create.py your-project
```

Subtitles are burned into the output by default using the project SRT timing.

- Font: `Inter`
- Size: `37px`
- Style: white text with a black `75%` opacity rounded background box
- Placement: bottom-center in the lower safe area

To skip subtitles for a render, use:

```bash
python3 create.py your-project --no-subs
```

## OpenRouter Setup

To generate AI B-roll prompts, add your OpenRouter key in `.env`.

```bash
cp .env.example .env
```

Then update `.env`:

```env
OPENROUTER_API_KEY=your_openrouter_api_key
```

## B-roll Replacement Flow

After running `create.py`, the tool generates temp B-roll images and prompts for you.

Go to the generated folder:

```text
your-project/
├── B_roll/
└── output/
```

Replace the generated temporary images inside `B_roll/` with your own image B-rolls, then run:

```bash
python3 recreate.py your-project
```

To rerender without subtitles:

```bash
python3 recreate.py your-project --no-subs
```

Your influencer explainer video will be ready in:

```text
your-project/output/final_edit.mp4
```
