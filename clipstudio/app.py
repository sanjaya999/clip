import logging
import os
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import clip_engine, highlights, hooks, pipelines
from .library import Library
from .models import (
    AIChoice, AIProvider, CaptionSegment, Clip, ClipRequest, MediaMode, PipelineInfo, ProviderSettings, Run, RunStatus,
    VideoInfo,
)
from .settings_store import ProviderSettingsStore
from .sources import SourceRef, UnsupportedSourceError, parse_source

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"
STORAGE_DIR = PROJECT_DIR / "storage"
MEDIA_DIR = STORAGE_DIR / "media"
CLIPS_DIR = STORAGE_DIR / "clips"

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("CLIP_STUDIO_DB", STORAGE_DIR / "clip_studio.db"))
provider_settings = ProviderSettingsStore(DB_PATH)
library = Library(DB_PATH)
# One worker, so one ffmpeg runs at a time and later runs wait in its queue.
runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
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
    return any(
        r.input_clip_id == clip_id and r.status in (RunStatus.QUEUED, RunStatus.RUNNING) for r in library.list_runs()
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
        raise HTTPException(status_code=400, detail="Cannot delete a clip that is currently in an active pipeline run.")
    _remove_clip(clip)
    return {"status": "deleted", "clip_id": clip_id}


@app.get("/api/pipelines", response_model=list[PipelineInfo])
def api_list_pipelines():
    return [p.info() for p in pipelines.PIPELINES.values()]


def _execute(run_id: str, ai: AIChoice) -> None:
    try:
        pipelines.execute(
            library.get_run(run_id), ai, library=library, clips_dir=CLIPS_DIR, saved=provider_settings.all()
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
