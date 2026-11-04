# Clip Studio

Cut short clips from YouTube, Twitch and Kick videos. Pick the range by hand, or let an AI read the subtitles and suggest moments.

Every exported clip is kept in a library. Send a clip into a pipeline from the editor's "Then send to" menu or from the Pipeline page, and follow each step there until it finishes. A pipeline's output is a new clip, so you can send it on to another pipeline. The first pipeline, Context hook, has an AI write one line from the clip's subtitles and draws it over the first 2 seconds.

## Download or stream

The menu next to "Load video" picks how the editor reads a video:

- **Download** saves the whole video to `storage/media` first, at up to 720p. Preview and export then run from that file.
- **Stream** saves nothing but your clips. The preview plays the platform's own 480p stream through the server, and each export seeks into the full stream and fetches only the clip's part, at up to 1080p. Use it for long VODs: a 15-second clip from an 8-hour Twitch VOD takes about half a minute instead of a 13 GB download.

Stream mode needs the internet while you work, and the preview loads hls.js from jsDelivr. A streamed video stays open on the server until it restarts. After a restart, choose Load video again.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- ffmpeg with `drawtext`

## Run

```bash
./start.sh
```

This creates `.venv`, installs the dependencies from `pyproject.toml`, and serves the app at http://localhost:8000.

## AI providers

Choose Claude, Gemini, any OpenAI-compatible server, or the local keyword method in the Highlights panel. Type a key in the page with "Remember on this computer" ticked, and it is saved to `storage/clip_studio.db` for next time. You can instead set `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `OPENAI_API_KEY` with `OPENAI_MODEL` and optional `OPENAI_BASE_URL` before you run `./start.sh`.

When configuring an OpenAI-compatible provider (or router like OpenRouter), provide the base URL up through the version prefix, omitting `/chat/completions` (the app appends `/chat/completions` automatically):
- **OpenRouter**: `https://openrouter.ai/api/v1`
- **Ollama**: `http://localhost:11434/v1`
- **LM Studio**: `http://localhost:1234/v1`
- **Groq**: `https://api.groq.com/openai/v1`

Twitch and Kick do not publish subtitles, so AI features work only on videos that have them.

## Test

```bash
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```

## Layout

- `clipstudio/` is the app: routes and the pipeline worker in `app.py`, pipelines in `pipelines.py`, and the editor and Pipeline pages in `static/`.
- `tests/` holds the pytest suite.
- `storage/` holds downloaded videos (Download mode only), exported clips, and a SQLite file with the clip library, the runs and saved AI settings. Git ignores its contents.

## Working with an AI agent

Read `AGENTS.md` first. It requires the pstack plugin.
