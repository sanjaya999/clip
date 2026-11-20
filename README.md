# Clip Studio

Cut short clips from YouTube, Twitch and Kick videos. Pick the range by hand, or let an AI read the subtitles and suggest moments.

Every exported clip is kept in a library. Send a clip into a pipeline from the editor's "Then send to" menu or from the Pipeline page, and follow each step there until it finishes. A pipeline's output is a new clip, so you can send it on to another pipeline. The first pipeline, Context hook, has an AI write one line from the clip's subtitles and draws it over the first 2 seconds.

## Caption and finish clips in the Studio

Open any clip from the Pipeline page with "Edit in Studio", or from the link the Editor shows after an export.

- **Add captions automatically** places the video's own subtitles on the clip's timeline. A clip saved before its subtitles loaded fetches them from YouTube first. Twitch and Kick publish no subtitles, so write those captions with Add caption.
- Edit each caption's words and times, split it at the playhead, join it with the next one, shift them all, or cut long lines into 2 to 6 words for short-form video.
- Style the captions and an optional title: font, size, colours, outline or box, position, bold, italic and capitals, or start from one of the looks.
- Reshape the clip to 9:16, 1:1, 4:5 or 16:9 by cropping (and choosing what to keep) or by fitting it inside a colour, add a border, trim it, and change brightness, contrast, saturation, volume and fades.

The preview shows each change as the export will draw it. Export burns everything in with ffmpeg and adds the result to the library as a new clip.

## Upload to YouTube, TikTok and Facebook

The Publish page uploads a clip to the platforms that welcome automated posting through an official API: YouTube, TikTok and a Facebook Page. Under Accounts, save each platform's app details and choose Connect, or paste a token. They stay in `storage/clip_studio.db` and are never shown again in full. Tick "Upload every finished clip automatically" to send every Studio export and pipeline result to that account with its default privacy and description.

Each account's card says what to create in the platform's developer console. YouTube and TikTok need a redirect URI, which the card shows. Until a platform reviews your app, uploads are private: YouTube keeps unverified projects' uploads private, and TikTok makes unaudited apps' posts visible only to you.

## Desktop app

`desktop/` wraps Clip Studio in an Electron app that bundles Python and ffmpeg, so it installs on Windows, macOS and Linux with nothing else. Tagging a release builds the installers on GitHub. See [desktop/README.md](desktop/README.md).

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

- `clipstudio/` is the app: routes and the pipeline and upload workers in `app.py`, pipelines in `pipelines.py`, upload platforms in `uploads.py`, captions in `captions.py`, and the Editor, Pipeline, Studio and Publish pages in `static/`. `python -m clipstudio` serves it without `start.sh`.
- `desktop/` is the Electron app and its release notes.
- `tests/` holds the pytest suite.
- `storage/` holds downloaded videos (Download mode only), exported clips, and a SQLite file with the clip library, the runs, the uploads, saved AI settings and upload accounts. Git ignores its contents. Set `CLIP_STUDIO_STORAGE` to keep it somewhere else.

## Working with an AI agent

Read `AGENTS.md` first. It requires the pstack plugin.
