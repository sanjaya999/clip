import html
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import captions, clip_engine, highlights, hooks, pipelines, uploads
from .library import Library
from .models import (
    AccountInfo, AccountSettings, AIChoice, AIProvider, CaptionSegment, Clip, ClipRequest, EditSpec, MediaMode,
    PipelineInfo, ProviderSettings, Run, RunStatus, Upload, UploadRequest, UploadStatus, VideoInfo,
)
from .settings_store import AccountStore, ProviderSettingsStore
from .sources import SourceRef, UnsupportedSourceError, parse_source, source_from_media_key

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"
# The desktop app points this at the user's app-data folder, since its install folder may be read-only.
STORAGE_DIR = Path(os.environ.get("CLIP_STUDIO_STORAGE") or PROJECT_DIR / "storage")
MEDIA_DIR = STORAGE_DIR / "media"
CLIPS_DIR = STORAGE_DIR / "clips"

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("CLIP_STUDIO_DB", STORAGE_DIR / "clip_studio.db"))
provider_settings = ProviderSettingsStore(DB_PATH)
accounts = AccountStore(DB_PATH)
library = Library(DB_PATH)
# One worker, so one ffmpeg runs at a time and later runs wait in its queue.
runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
# Uploads wait on the network, not ffmpeg, so they get their own worker and never hold up a run.
uploader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload")
# OAuth connections in progress: state -> (platform, redirect URI). Each is used once.
oauth_states: dict[str, tuple[str, str]] = {}
log = logging.getLogger(__name__)
# Stream mode keeps no video file. A streamed video is its link and the stream URLs found for it, held here in memory.
# Platforms expire stream URLs after a few hours, so older ones are looked up again before use.
STREAM_URL_MAX_AGE = 3600


@dataclass
class OpenStream:
    source: SourceRef
    media: clip_engine.RemoteMedia
    resolved_at: float
    # The platform URIs behind each proxied track's segments, filled in when that track's playlist is first read.
    segment_urls: dict[str, list[str]] = field(default_factory=dict)


streams: dict[str, OpenStream] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Without this, stopping the server waits for every queued run. The next start marks them failed.
    runner.shutdown(wait=False, cancel_futures=True)
    uploader.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Video Clip Studio", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UrlRequest(BaseModel):
    url: str


class RunRequest(BaseModel):
    clip_id: str
    pipeline: str
    ai: AIChoice = AIChoice()


class RerunRequest(BaseModel):
    ai: AIChoice = AIChoice()


class HighlightsRequest(BaseModel):
    captions: list[CaptionSegment]
    provider: AIProvider = AIProvider.AUTO
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    query: str | None = None


def _source(url: str) -> SourceRef:
    try:
        return parse_source(url)
    except UnsupportedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/info", response_model=VideoInfo)
def api_get_info(payload: UrlRequest):
    source = _source(payload.url)
    try:
        info = clip_engine.get_video_info(source)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read video info: {exc}")
    if (MEDIA_DIR / f"{source.media_key}.mp4").exists():
        info.local_path = f"/media/{source.media_key}.mp4"
    return info


@app.post("/api/download")
def api_download_video(payload: UrlRequest):
    source = _source(payload.url)
    if not (MEDIA_DIR / f"{source.media_key}.mp4").exists():
        try:
            clip_engine.download_video(source, MEDIA_DIR)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Download failed: {exc}")
    return {
        "video_id": source.media_key,
        "stream_url": f"/media/{source.media_key}.mp4",
        "status": "ready"
    }


def _resolve_stream(source: SourceRef) -> OpenStream:
    try:
        media = clip_engine.resolve_stream(source)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not open the stream: {exc}")
    if media.duration <= 0:
        raise HTTPException(status_code=400, detail="This video reports no length, so it cannot be streamed. Download it.")
    stream = streams[source.media_key] = OpenStream(source, media, time.time())
    return stream


def _stream(media_key: str) -> OpenStream:
    stream = streams.get(media_key)
    if stream is None:
        raise HTTPException(status_code=404, detail="This stream is not open on the server. Load the video again.")
    if time.time() - stream.resolved_at > STREAM_URL_MAX_AGE:
        stream = _resolve_stream(stream.source)
    return stream


def _playlist(text: str) -> Response:
    return Response(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


def _proxy_track(stream: OpenStream, track: str) -> clip_engine.RemoteInput:
    remote = (stream.media.proxy_tracks() or {}).get(track)
    if remote is None:
        raise HTTPException(status_code=404, detail="This stream has no such track")
    return remote


def _proxied_playlist(stream: OpenStream, track: str) -> str:
    remote = _proxy_track(stream, track)
    try:
        res = clip_engine.fetch_remote(remote)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"The platform did not send the playlist: {exc}")
    text, stream.segment_urls[track] = clip_engine.proxy_playlist(res.text, res.url, track)
    return text


@app.post("/api/stream")
def api_open_stream(payload: UrlRequest):
    """Open a video for streaming: the preview and every clip read the platform's stream, and nothing is downloaded."""
    source = _source(payload.url)
    stream = _resolve_stream(source)
    return {
        "video_id": source.media_key,
        "stream_url": f"/stream/{source.media_key}/index.m3u8",
        "duration": stream.media.duration,
        "status": "ready",
    }


@app.get("/stream/{media_key}/index.m3u8")
def serve_stream_playlist(media_key: str):
    """The preview: the platform's own HLS passed through this server when the browser can play it, else ffmpeg cuts."""
    media = _stream(media_key).media
    tracks = media.proxy_tracks()
    return _playlist(clip_engine.proxy_master_playlist(tracks) if tracks else clip_engine.preview_playlist(media.duration))


@app.get("/stream/{media_key}/{track}.m3u8")
def serve_proxied_playlist(media_key: str, track: str):
    return _playlist(_proxied_playlist(_stream(media_key), track))


@app.get("/stream/{media_key}/{track}/{index}")
def serve_proxied_segment(media_key: str, track: str, index: int):
    stream = _stream(media_key)
    remote = _proxy_track(stream, track)
    if track not in stream.segment_urls:
        _proxied_playlist(stream, track)
    urls = stream.segment_urls[track]
    if not 0 <= index < len(urls):
        raise HTTPException(status_code=404, detail="No such segment")
    try:
        res = clip_engine.fetch_remote(remote, urls[index])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"The platform did not send the segment: {exc}")
    return Response(res.content, media_type=res.headers.get("content-type") or "video/mp2t")


@app.get("/stream/{media_key}/{index}.ts")
def serve_stream_segment(media_key: str, index: int):
    media = _stream(media_key).media
    if media.proxy_tracks() or not 0 <= index < clip_engine.preview_segment_count(media.duration):
        raise HTTPException(status_code=404, detail="No such preview segment")
    try:
        segment = clip_engine.preview_segment(media, index)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return Response(segment, media_type="video/mp2t")


@app.post("/api/captions")
def api_get_captions(payload: UrlRequest):
    source = _source(payload.url)
    try:
        segments = clip_engine.fetch_captions(source)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Captions extraction failed: {exc}")
    return {"video_id": source.media_key, "captions": segments}


@app.get("/api/ai/providers")
def api_list_providers():
    saved = provider_settings.all()
    return {
        "providers": highlights.list_providers(saved),
        "auto": highlights.resolve_provider(AIProvider.AUTO, saved),
    }


def _configurable(provider: AIProvider) -> AIProvider:
    if provider in (AIProvider.AUTO, AIProvider.LOCAL):
        raise HTTPException(status_code=400, detail=f"There is nothing to save for {provider.value}.")
    return provider


@app.put("/api/ai/providers/{provider}")
def api_save_provider_settings(provider: AIProvider, payload: ProviderSettings):
    cleaned = ProviderSettings(**{k: (v or "").strip() or None for k, v in payload.model_dump().items()})
    provider_settings.save(_configurable(provider), cleaned)
    return api_list_providers()


@app.delete("/api/ai/providers/{provider}")
def api_forget_provider_settings(provider: AIProvider):
    provider_settings.forget(_configurable(provider))
    return api_list_providers()


@app.post("/api/ai-highlights")
def api_get_highlights(payload: HighlightsRequest):
    try:
        provider, clips = highlights.find_highlights(
            payload.captions,
            payload.provider,
            api_key=payload.api_key or None,
            model=payload.model or None,
            base_url=payload.base_url or None,
            query=payload.query or None,
            saved=provider_settings.all(),
        )
    except highlights.ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except highlights.ProviderCallError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"provider": provider, "highlights": clips}


@app.post("/api/clip", response_model=Clip)
def api_create_clip(payload: ClipRequest):
    if payload.mode is MediaMode.STREAM:
        media = _stream(payload.video_id).media
        cut = lambda out: clip_engine.cut_remote_clip(media, payload.start_time, payload.end_time, out)
    else:
        source_path = MEDIA_DIR / f"{payload.video_id}.mp4"
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="Load the video before exporting a clip.")
        cut = lambda out: clip_engine.cut_clip(str(source_path), payload.start_time, payload.end_time, out)

    clip_id = pipelines.new_id()
    safe_name = payload.filename or f"clip_{payload.video_id}_{int(payload.start_time)}_{int(payload.end_time)}.mp4"
    if not safe_name.endswith(".mp4"):
        safe_name += ".mp4"

    output_path = CLIPS_DIR / f"{clip_id}_{safe_name}"
    try:
        cut(str(output_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Clip cutting failed: {exc}")

    before, inside = hooks.hook_window(payload.captions, payload.start_time, payload.end_time)
    clip = Clip(
        clip_id=clip_id,
        video_id=payload.video_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        file_name=output_path.name,
        captions=[*before, *inside],
        created_at=time.time(),
    )
    library.add_clip(clip)
    return clip


@app.get("/api/clips", response_model=list[Clip])
def api_list_clips():
    return library.list_clips()


def _in_active_run(clip_id: str) -> bool:
    """True while a run reads the clip or an upload sends it, so its file must stay."""
    return any(
        r.input_clip_id == clip_id and r.status in (RunStatus.QUEUED, RunStatus.RUNNING) for r in library.list_runs()
    ) or any(
        u.clip_id == clip_id and u.status in (UploadStatus.QUEUED, UploadStatus.UPLOADING) for u in library.list_uploads()
    )


def _remove_clip(clip: Clip) -> None:
    (CLIPS_DIR / clip.file_name).unlink(missing_ok=True)
    library.delete_clip(clip.clip_id)


@app.delete("/api/clips")
def api_clear_clips():
    """Delete every clip except the ones an active run is still reading."""
    kept = 0
    deleted = 0
    for clip in library.list_clips():
        if _in_active_run(clip.clip_id):
            kept += 1
            continue
        _remove_clip(clip)
        deleted += 1
    return {"status": "cleared", "count": deleted, "kept": kept}


@app.delete("/api/clips/{clip_id}")
def api_delete_clip(clip_id: str):
    clip = library.get_clip(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    if _in_active_run(clip_id):
        raise HTTPException(status_code=400, detail="Cannot delete a clip that an active run or upload is still using.")
    _remove_clip(clip)
    return {"status": "deleted", "clip_id": clip_id}


@app.get("/api/pipelines", response_model=list[PipelineInfo])
def api_list_pipelines():
    return [p.info() for p in pipelines.PIPELINES.values()]


def _execute(run_id: str, ai: AIChoice) -> None:
    try:
        pipelines.execute(
            library.get_run(run_id), ai, library=library, clips_dir=CLIPS_DIR, saved=provider_settings.all(),
            on_clip=_queue_auto_uploads,
        )
    except Exception:
        log.exception("Run %s stopped outside its steps", run_id)


@app.post("/api/runs", response_model=Run, status_code=202)
def api_start_run(payload: RunRequest):
    clip = library.get_clip(payload.clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="That clip is not in the library.")
    pipeline = pipelines.PIPELINES.get(payload.pipeline)
    if pipeline is None:
        raise HTTPException(status_code=400, detail=f"There is no pipeline called {payload.pipeline}.")
    if pipeline.needs_captions and not clip.captions:
        raise HTTPException(
            status_code=400,
            detail=f"{pipeline.label} needs subtitles, and this clip has none. Export it from a video with subtitles.",
        )
    run = pipeline.new_run(clip)
    library.add_run(run)
    runner.submit(_execute, run.run_id, payload.ai)
    return run


@app.get("/api/runs", response_model=list[Run])
def api_list_runs():
    return library.list_runs()


@app.delete("/api/runs")
def api_clear_runs():
    count = library.clear_finished_runs()
    return {"status": "cleared", "count": count}


@app.get("/api/runs/{run_id}", response_model=Run)
def api_get_run(run_id: str):
    run = library.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.post("/api/runs/{run_id}/rerun", response_model=Run)
def api_rerun(run_id: str, payload: RerunRequest):
    run = library.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
        raise HTTPException(status_code=400, detail="Run is already active.")
    clip = library.get_clip(run.input_clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="The input clip for this run is no longer in the library.")
    # Running a finished run again replaces its output instead of adding a second clip of the same range.
    previous = run.output_clip_id and library.get_clip(run.output_clip_id)
    if previous:
        if _in_active_run(previous.clip_id):
            raise HTTPException(status_code=400, detail="This run's clip is being used by another run right now.")
        _remove_clip(previous)
    run.reset()
    library.save_run(run)
    runner.submit(_execute, run.run_id, payload.ai)
    return run


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: str):
    run = library.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
        raise HTTPException(status_code=400, detail="Cannot delete an active run.")
    library.delete_run(run_id)
    return {"status": "deleted", "run_id": run_id}


def _library_clip(clip_id: str) -> Clip:
    clip = library.get_clip(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="That clip is not in the library.")
    return clip


@app.get("/api/clips/{clip_id}", response_model=Clip)
def api_get_clip(clip_id: str):
    return _library_clip(clip_id)


@app.get("/api/studio")
def api_studio_options():
    """What the Studio page offers: the caption fonts, and a fresh edit with every setting at its default."""
    return {"fonts": list(captions.FONTS), "defaults": EditSpec().model_dump(mode="json")}


@app.post("/api/clips/{clip_id}/auto-captions")
def api_auto_captions(clip_id: str):
    """The clip's subtitles on its own clock. A clip saved without them fetches them from its video first."""
    clip = _library_clip(clip_id)
    lines = captions.clip_captions(clip.captions, clip.start_time, clip.end_time)
    if lines:
        return {"captions": lines, "fetched": False}
    source = source_from_media_key(clip.video_id)
    if source is None:
        raise HTTPException(
            status_code=400,
            detail="This platform publishes no subtitles. Add captions by hand with Add caption.",
        )
    try:
        segments = clip_engine.fetch_captions(source)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch subtitles: {exc}")
    before, inside = hooks.hook_window(segments, clip.start_time, clip.end_time)
    library.set_captions(clip.clip_id, [*before, *inside])
    return {"captions": captions.clip_captions(inside, clip.start_time, clip.end_time), "fetched": True}


@app.post("/api/clips/{clip_id}/render", response_model=Clip)
def api_render_clip(clip_id: str, spec: EditSpec):
    """Export the Studio's edit of a clip as a new clip in the library."""
    clip = _library_clip(clip_id)
    source = CLIPS_DIR / clip.file_name
    if not source.is_file():
        raise HTTPException(status_code=404, detail="This clip's file is missing from storage.")
    new_id = pipelines.new_id()
    stem = Path(clip.file_name).stem.removeprefix(f"{clip.clip_id}_").removesuffix("_studio")
    final = CLIPS_DIR / f"{new_id}_{stem}_studio.mp4"
    part = CLIPS_DIR / f".{new_id}.part.mp4"
    try:
        offset, length = clip_engine.render_edit(source, spec, part)
    except ValueError as exc:
        part.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"The export failed: {exc}")
    part.replace(final)
    start = clip.start_time + offset
    made = clip.model_copy(update={
        "clip_id": new_id,
        "file_name": final.name,
        "start_time": round(start, 3),
        "end_time": round(start + length, 3),
        "made_by_run": None,
        "created_at": time.time(),
    })
    library.add_clip(made)
    _queue_auto_uploads(made)
    return made


def _destination(platform: str) -> uploads.Destination:
    destination = uploads.DESTINATIONS.get(platform)
    if destination is None:
        raise HTTPException(status_code=404, detail=f"There is no upload platform called {platform}.")
    return destination


def _account_list() -> list[AccountInfo]:
    return [d.info(accounts.get(d.id)) for d in uploads.DESTINATIONS.values()]


@app.get("/api/accounts", response_model=list[AccountInfo])
def api_list_accounts():
    return _account_list()


@app.put("/api/accounts/{platform}", response_model=list[AccountInfo])
def api_save_account(platform: str, payload: AccountSettings):
    destination = _destination(platform)
    unknown = set(payload.credentials) - {f.name for f in destination.fields}
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"{destination.label} has no field called {', '.join(sorted(unknown))}."
        )
    account = accounts.get(platform)
    # A blank field keeps what is saved, so the page never needs to know a secret to change the others.
    account.credentials.update({k: v.strip() for k, v in payload.credentials.items() if v.strip()})
    if payload.auto_upload is not None:
        account.auto_upload = payload.auto_upload
    if payload.default_privacy is not None:
        account.default_privacy = payload.default_privacy
    if payload.default_description is not None:
        account.default_description = payload.default_description
    accounts.save(platform, account)
    return _account_list()


@app.delete("/api/accounts/{platform}", response_model=list[AccountInfo])
def api_forget_account(platform: str):
    _destination(platform)
    accounts.forget(platform)
    return _account_list()


@app.get("/api/accounts/{platform}/connect")
def api_connect_account(platform: str, request: Request):
    """Send the browser to the platform's sign-in page. It comes back to the callback below."""
    destination = _destination(platform)
    account = accounts.get(platform)
    if destination.oauth is None:
        raise HTTPException(status_code=400, detail=f"{destination.label} connects with a pasted token, not a sign-in.")
    if not all(account.credentials.get(n) for n in destination.oauth.needs):
        raise HTTPException(status_code=400, detail=f"Save the {destination.label} app details before connecting.")
    state = secrets.token_urlsafe(24)
    redirect_uri = str(request.url_for("api_account_callback", platform=platform))
    oauth_states[state] = (platform, redirect_uri)
    return RedirectResponse(destination.oauth.authorize_url(account.credentials, redirect_uri, state))


def _callback_page(title: str, message: str) -> HTMLResponse:
    return HTMLResponse(
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Clip Studio</title>'
        '<link rel="stylesheet" href="/css/app.css"><link rel="stylesheet" href="/css/publish.css"></head>'
        f'<body><main class="callback"><h1 class="pane-title">{html.escape(title)}</h1>'
        f'<p>{html.escape(message)}</p><p><a class="btn" href="/publish.html">Back to Publish</a></p></main></body></html>'
    )


@app.get("/api/accounts/{platform}/callback", name="api_account_callback")
def api_account_callback(platform: str, state: str = "", code: str = "", error: str = "", error_description: str = ""):
    destination = _destination(platform)
    expected = oauth_states.pop(state, None)
    if expected is None or expected[0] != platform or destination.oauth is None:
        return _callback_page("Not connected", "This sign-in link expired or was already used. Choose Connect again.")
    if error or not code:
        return _callback_page("Not connected", f"{destination.label} said: {error_description or error or 'no code'}")
    try:
        tokens = destination.oauth.exchange(accounts.get(platform).credentials, code, expected[1])
    except Exception as exc:
        return _callback_page("Not connected", str(exc))
    accounts.update_credentials(platform, tokens)
    return _callback_page(f"{destination.label} connected", "Clips can upload to this account now. You can close this tab.")


def _queue_upload(upload: Upload) -> Upload:
    library.add_upload(upload)
    uploader.submit(_send_upload, upload.upload_id)
    return upload


def _send_upload(upload_id: str) -> None:
    try:
        uploads.execute(library.get_upload(upload_id), library=library, accounts=accounts, clips_dir=CLIPS_DIR)
    except Exception:
        log.exception("Upload %s stopped outside its platform call", upload_id)


def _default_title(clip: Clip) -> str:
    return (clip.hook or Path(clip.file_name).stem.removeprefix(f"{clip.clip_id}_").replace("_", " "))[:100]


def _queue_auto_uploads(clip: Clip | None) -> None:
    """Upload a finished clip to every connected account that has automatic upload turned on."""
    if clip is None:
        return
    for destination in uploads.DESTINATIONS.values():
        account = accounts.get(destination.id)
        if not (account.auto_upload and destination.connected(account)):
            continue
        _queue_upload(Upload(
            upload_id=pipelines.new_id(),
            clip_id=clip.clip_id,
            platform=destination.id,
            title=_default_title(clip),
            description=account.default_description,
            privacy=account.default_privacy,
            automatic=True,
            created_at=time.time(),
        ))


@app.post("/api/uploads", response_model=Upload, status_code=202)
def api_start_upload(payload: UploadRequest):
    clip = _library_clip(payload.clip_id)
    destination = _destination(payload.platform)
    if not destination.connected(accounts.get(destination.id)):
        raise HTTPException(status_code=400, detail=f"Connect {destination.label} on the Publish page first.")
    return _queue_upload(Upload(
        upload_id=pipelines.new_id(),
        clip_id=clip.clip_id,
        platform=destination.id,
        title=payload.title.strip() or _default_title(clip),
        description=payload.description,
        tags=[t.strip() for t in payload.tags if t.strip()],
        privacy=payload.privacy,
        created_at=time.time(),
    ))


@app.get("/api/uploads", response_model=list[Upload])
def api_list_uploads():
    return library.list_uploads()


def _finished_upload(upload_id: str) -> Upload:
    upload = library.get_upload(upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    if upload.status in (UploadStatus.QUEUED, UploadStatus.UPLOADING):
        raise HTTPException(status_code=400, detail="This upload is still in progress.")
    return upload


@app.post("/api/uploads/{upload_id}/retry", response_model=Upload, status_code=202)
def api_retry_upload(upload_id: str):
    upload = _finished_upload(upload_id)
    _library_clip(upload.clip_id)
    _destination(upload.platform)
    return _queue_upload(upload.model_copy(update={
        "upload_id": pipelines.new_id(), "status": UploadStatus.QUEUED, "detail": None, "remote_url": None,
        "automatic": False, "created_at": time.time(), "finished_at": None,
    }))


@app.delete("/api/uploads/{upload_id}")
def api_delete_upload(upload_id: str):
    _finished_upload(upload_id)
    library.delete_upload(upload_id)
    return {"status": "deleted", "upload_id": upload_id}


@app.delete("/api/uploads")
def api_clear_uploads():
    return {"status": "cleared", "count": library.clear_finished_uploads()}


@app.get("/media/{filename}")
def serve_media(filename: str):
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(file_path, media_type="video/mp4")


def _clip_file(filename: str) -> Path:
    file_path = CLIPS_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Clip file not found")
    return file_path


@app.get("/clips/{filename}")
def serve_clip(filename: str):
    return FileResponse(_clip_file(filename), media_type="video/mp4")


@app.get("/downloads/{filename}")
def serve_download(filename: str):
    file_path = _clip_file(filename)
    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
