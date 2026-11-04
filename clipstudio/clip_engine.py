import functools
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException

from .models import CaptionSegment, Platform, VideoInfo
from .sources import SourceRef

CHAT_TRACKS = {"live_chat", "rechat"}
SUBTITLE_EXTS = ("json3", "vtt")
BROWSER_VIDEO_CODECS = {"h264", "vp9", "av1"}
HOOK_SECONDS = 2
# Stream mode prefers HLS: ffmpeg seeks it by fetching only the segments a cut needs, while YouTube's fragmented
# MP4 makes it read from the start. H.264 decodes fastest. Every clip is re-encoded, so the container does not matter.
STREAM_FORMAT = "/".join([
    "bestvideo[protocol^=m3u8][height<=1080][vcodec^=avc1]+bestaudio[protocol^=m3u8]",
    "bestvideo[protocol^=m3u8][height<=1080]+bestaudio[protocol^=m3u8]",
    "best[protocol^=m3u8][height<=1080]",
    "bestvideo[height<=1080]+bestaudio",
    "best",
])
PREVIEW_SEGMENT_SECONDS = 10
PREVIEW_HEIGHT = 480

_VTT_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})"
_VTT_CUE_TIMING = re.compile(rf"{_VTT_TIME}\s+-->\s+{_VTT_TIME}")
_VTT_TAG = re.compile(r"<[^>]*>")


def _ydl(**opts: Any) -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, **opts})


def get_video_info(source: SourceRef) -> VideoInfo:
    with _ydl(skip_download=True) as ydl:
        meta = ydl.extract_info(source.url, download=False)
    if not meta:
        raise ValueError(f"Could not extract metadata from {source.url}")

    stream_url = None
    formats = meta.get("formats") or []
    progressive_formats = [
        f for f in formats
        if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("url")
    ]
    if progressive_formats:
        progressive_formats.sort(key=lambda f: f.get("height") or 0, reverse=True)
        stream_url = progressive_formats[0].get("url")
    elif formats:
        stream_url = formats[-1].get("url")

    return VideoInfo(
        video_id=source.media_key,
        platform=source.platform,
        title=meta.get("title") or "Unknown Title",
        duration=float(meta.get("duration") or 0.0),
        thumbnail=meta.get("thumbnail") or "",
        stream_url=stream_url,
    )


@dataclass(frozen=True)
class RemoteInput:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    protocol: str = ""
    codec: str = ""

    @property
    def is_hls(self) -> bool:
        return self.protocol.startswith("m3u8")


@dataclass(frozen=True)
class RemoteMedia:
    """A video read straight from the platform: one muxed input, or separate video and audio inputs."""
    video: RemoteInput
    audio: RemoteInput | None
    duration: float
    # A smaller rendition of `video` for the editor's preview, when the platform offers one. Same layout as `video`.
    preview_video: RemoteInput | None = None

    def for_preview(self) -> "RemoteMedia":
        return replace(self, video=self.preview_video) if self.preview_video else self

    def proxy_tracks(self) -> dict[str, RemoteInput] | None:
        """The platform's HLS renditions the browser can play as they are, or None when ffmpeg must cut the preview."""
        media = self.for_preview()
        tracks = {"video": media.video, **({"audio": media.audio} if media.audio else {})}
        if all(t.is_hls for t in tracks.values()) and media.video.codec.startswith("avc1"):
            return tracks
        return None


def _remote_input(fmt: dict[str, Any]) -> RemoteInput:
    codec = fmt.get("vcodec") if fmt.get("vcodec") not in (None, "none") else fmt.get("acodec")
    return RemoteInput(fmt["url"], dict(fmt.get("http_headers") or {}), fmt.get("protocol") or "", codec or "")


def _preview_format(formats: list[dict[str, Any]], video_fmt: dict[str, Any], split: bool) -> dict[str, Any] | None:
    """The best rendition up to PREVIEW_HEIGHT that reads like `video_fmt`: same protocol, and video-only if split.
    H.264 comes first, since the browser can play it straight from the platform."""
    def fits(f: dict[str, Any]) -> bool:
        return (
            bool(f.get("url")) and f.get("protocol") == video_fmt.get("protocol")
            and f.get("vcodec") not in (None, "none") and (f.get("acodec") == "none") == split
            and 0 < (f.get("height") or 0) <= PREVIEW_HEIGHT
        )
    candidates = [f for f in formats if fits(f)]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (str(f.get("vcodec")).startswith("avc1"), f["height"], f.get("tbr") or 0))


def resolve_stream(source: SourceRef) -> RemoteMedia:
    """Find the stream URLs ffmpeg can read in place, so nothing but the clip is written to disk."""
    with _ydl(format=STREAM_FORMAT) as ydl:
        meta = ydl.extract_info(source.url, download=False)
    if not meta:
        raise ValueError(f"Could not extract metadata from {source.url}")
    picked = [f for f in (meta.get("requested_formats") or [meta]) if f.get("url")]
    if not picked:
        raise ValueError("The platform offered no stream for this video")
    video_fmt = next((f for f in picked if f.get("vcodec") != "none"), picked[0])
    audio_fmt = next((f for f in picked if f is not video_fmt), None)
    preview_fmt = _preview_format(meta.get("formats") or [], video_fmt, split=audio_fmt is not None)
    return RemoteMedia(
        video=_remote_input(video_fmt),
        audio=_remote_input(audio_fmt) if audio_fmt else None,
        duration=float(meta.get("duration") or 0.0),
        preview_video=_remote_input(preview_fmt) if preview_fmt and preview_fmt is not video_fmt else None,
    )


def _remote_input_args(remote: RemoteInput, start: float) -> list[str]:
    headers = dict(remote.headers)
    # A seek to 0 fails on an HLS stream whose first frame is a little after 0, and ffmpeg then drops seconds of video.
    args = ["-ss", f"{start:.3f}"] if start > 0 else []
    if remote.url.startswith(("http://", "https://")):
        args += ["-reconnect", "1", "-reconnect_delay_max", "5"]
    # ffmpeg sends its own User-Agent, so the platform's goes in its own option rather than as a second header.
    agent = headers.pop("User-Agent", None)
    if agent:
        args += ["-user_agent", agent]
    if headers:
        args += ["-headers", "".join(f"{name}: {value}\r\n" for name, value in headers.items())]
    return [*args, "-i", remote.url]


def _remote_args(media: RemoteMedia, start: float) -> list[str]:
    """ffmpeg inputs seeked to `start` on the platform's servers, mapped to one video and one audio stream."""
    args = _remote_input_args(media.video, start)
    if media.audio is None:
        return [*args, "-map", "0:v:0", "-map", "0:a:0?"]
    return [*args, *_remote_input_args(media.audio, start), "-map", "0:v:0", "-map", "1:a:0"]


_URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')


def proxy_master_playlist(tracks: dict[str, RemoteInput]) -> str:
    """The playlist the editor opens for a proxied stream: the video track, joined to the audio track when separate."""
    lines = ["#EXTM3U", "#EXT-X-STREAM-INF:BANDWIDTH=2000000", "video.m3u8"]
    if "audio" in tracks:
        lines[1:2] = [
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"',
            '#EXT-X-STREAM-INF:BANDWIDTH=2000000,AUDIO="audio"',
        ]
    return "\n".join(lines) + "\n"


def proxy_playlist(text: str, base_url: str, track: str) -> tuple[str, list[str]]:
    """Point every URI in a platform media playlist at this server's `<track>/<n>`, and return the URIs replaced."""
    uris: list[str] = []

    def point(uri: str) -> str:
        uris.append(urllib.parse.urljoin(base_url, uri))
        return f"{track}/{len(uris) - 1}"

    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lines.append(_URI_ATTRIBUTE.sub(lambda m: f'URI="{point(m[1])}"', line) if line.startswith("#") else point(line))
    return "\n".join(lines) + "\n", uris


def fetch_remote(remote: RemoteInput, url: str | None = None) -> requests.Response:
    """GET a playlist or segment the way the platform expects, with the headers yt-dlp found for it."""
    res = requests.get(url or remote.url, headers=remote.headers, timeout=30)
    res.raise_for_status()
    return res


def preview_playlist(duration: float) -> str:
    """An HLS playlist whose segments `preview_segment` cuts on request, so the editor can seek a stream."""
    count = preview_segment_count(duration)
    lines = [
        "#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{PREVIEW_SEGMENT_SECONDS}",
        "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for index in range(count):
        length = min(PREVIEW_SEGMENT_SECONDS, duration - index * PREVIEW_SEGMENT_SECONDS)
        lines += [f"#EXTINF:{length:.3f},", f"{index}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def preview_segment_count(duration: float) -> int:
    return max(1, -int(-duration // PREVIEW_SEGMENT_SECONDS))


def preview_segment(media: RemoteMedia, index: int) -> bytes:
    """Cut one low-resolution preview segment from the stream and return it as MPEG-TS, without touching disk."""
    start = index * PREVIEW_SEGMENT_SECONDS
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", *_remote_args(media.for_preview(), start),
            "-t", str(PREVIEW_SEGMENT_SECONDS),
            "-vf", f"scale=-2:'min({PREVIEW_HEIGHT},ih)'", *_h264_args(preview=True),
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            # Each segment carries its place in the video, so the player lines them up on one timeline.
            "-output_ts_offset", str(start), "-f", "mpegts", "pipe:1",
        ],
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"ffmpeg could not cut preview segment {index}: {result.stderr.decode(errors='replace')}")
    return result.stdout


def download_video(source: SourceRef, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{source.media_key}.mp4"
    ydl_opts = {
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
        "outtmpl": str(output_dir / f"{source.media_key}.%(ext)s"),
        "merge_output_format": "mp4",
        # Twitch and Kick serve single-stream HLS, which arrives as MPEG-TS and needs a remux to mp4.
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "concurrent_fragment_downloads": 8,
        "overwrites": True,
    }
    with _ydl(**ydl_opts) as ydl:
        ydl.download([source.url])
    if not target.exists():
        raise FileNotFoundError(f"Download finished but {target} is missing")
    ensure_browser_playable(target)
    return target


@functools.cache
def _h264_args(preview: bool = False) -> tuple[str, ...]:
    """Encoder options for a clip, or with `preview` for a stream preview segment, which must encode faster than it plays."""
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if " libx264 " in encoders:
        return ("-c:v", "libx264", "-preset", "veryfast" if preview else "fast", "-crf", "26" if preview else "22")
    # Builds without libx264, such as Fedora's ffmpeg-free, ship OpenH264, which takes a bitrate instead of a CRF.
    return ("-c:v", "libopenh264", "-b:v", "1M" if preview else "4M")


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with exit code {result.returncode}: {result.stderr}")


def _video_codec(path: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def ensure_browser_playable(path: Path) -> None:
    """Re-encode to H.264 in place when the browser preview cannot play the codec (Twitch clips arrive as HEVC)."""
    if _video_codec(path) in BROWSER_VIDEO_CODECS:
        return
    converted = path.with_name(f"{path.stem}.h264{path.suffix}")
    _run_ffmpeg(["-i", str(path), *_h264_args(), "-c:a", "copy", "-movflags", "+faststart", str(converted)])
    converted.replace(path)


def fetch_captions(source: SourceRef) -> list[CaptionSegment]:
    if source.platform is Platform.YOUTUBE:
        try:
            segments = _transcript_api_captions(source.id)
        except (YouTubeTranscriptApiException, requests.RequestException):
            segments = []
        if segments:
            return segments
    return _ytdlp_captions(source)


def _language_rank(code: str) -> int:
    if code == "en":
        return 0
    if code.startswith("en-"):
        return 1
    return 2


def _transcript_api_captions(video_id: str) -> list[CaptionSegment]:
    transcripts = YouTubeTranscriptApi().list(video_id)
    best = min(transcripts, key=lambda t: (_language_rank(t.language_code) == 2, t.is_generated))
    return _segments((s.start, s.start + s.duration, s.text) for s in best.fetch())


def _pick_subtitle_track(info: dict[str, Any]) -> dict[str, Any] | None:
    for field in ("subtitles", "automatic_captions"):
        tracks = {lang: fmts for lang, fmts in (info.get(field) or {}).items() if lang not in CHAT_TRACKS}
        if not tracks:
            continue
        lang = min(tracks, key=_language_rank)
        for ext in SUBTITLE_EXTS:
            track = next((f for f in tracks[lang] if f.get("ext") == ext and f.get("url")), None)
            if track:
                return track
    return None


def _ytdlp_captions(source: SourceRef) -> list[CaptionSegment]:
    with _ydl(skip_download=True) as ydl:
        info = ydl.extract_info(source.url, download=False)
        track = _pick_subtitle_track(info or {})
        if track is None:
            return []
        with ydl.urlopen(track["url"]) as resp:
            body = resp.read().decode("utf-8")
    if track["ext"] == "json3":
        return parse_json3(json.loads(body))
    return parse_vtt(body)


def _segments(rows: Iterable[tuple[float, float, str]]) -> list[CaptionSegment]:
    segments: list[CaptionSegment] = []
    for start, end, raw_text in rows:
        text = " ".join(raw_text.split())
        if not text or (segments and segments[-1].text == text):
            continue
        segments.append(CaptionSegment(start=round(start, 3), end=round(max(end, start), 3), text=text))
    return segments


def parse_json3(raw: dict[str, Any]) -> list[CaptionSegment]:
    rows = []
    for event in raw.get("events") or []:
        if "segs" not in event or "tStartMs" not in event:
            continue
        start = event["tStartMs"] / 1000.0
        end = start + event.get("dDurationMs", 0) / 1000.0
        rows.append((start, end, "".join(seg.get("utf8", "") for seg in event["segs"])))
    return _segments(rows)


def _vtt_seconds(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def parse_vtt(text: str) -> list[CaptionSegment]:
    rows = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.strip().splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = _VTT_CUE_TIMING.search(lines[timing_index])
        if not timing:
            continue
        start = _vtt_seconds(*timing.groups()[:4])
        end = _vtt_seconds(*timing.groups()[4:])
        for line in lines[timing_index + 1:]:
            rows.append((start, end, html.unescape(_VTT_TAG.sub("", line))))
    return _segments(rows)


def _frame_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    width, height = out.split(",")[:2]
    return int(width), int(height)


def _filter_path(path: Path) -> str:
    # A Windows path's drive colon would end the option, so use forward slashes and escape the colon.
    return "'" + path.as_posix().replace(":", r"\:") + "'"


def _hook_font() -> str:
    # Windows ffmpeg builds ship without a fontconfig config, and drawtext crashes looking up "Sans" there.
    windows_font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    if sys.platform == "win32" and windows_font.is_file():
        return f"fontfile={_filter_path(windows_font)}"
    return "font=Sans"


def _hook_filter(text_file: Path, width: int, height: int, chars: int) -> str:
    # Roughly 0.6 em per character, so the line fits 90% of the width; never taller than 1/14 of the frame.
    size = max(12, int(min(height / 14, 0.9 * width / (0.6 * max(chars, 1)))))
    return (
        f"drawtext=textfile={_filter_path(text_file)}:expansion=none:{_hook_font()}:fontcolor=white:fontsize={size}"
        f":box=1:boxcolor=black@0.65:boxborderw={size // 2}:x=(w-text_w)/2:y=h*0.08"
        f":enable='lt(t,{HOOK_SECONDS})'"
    )


def _encode_clip(input_args: list[str], duration: float, output_path: str) -> str:
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        *input_args,
        "-t", str(round(max(duration, 0.1), 3)),
        *_h264_args(),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_file),
    ])
    return str(out_file.resolve())


def cut_clip(source_video_path: str, start_time: float, end_time: float, output_path: str) -> str:
    source = Path(source_video_path)
    if not source.exists():
        raise FileNotFoundError(f"Source video not found: {source_video_path}")
    return _encode_clip(["-ss", str(round(start_time, 3)), "-i", str(source)], end_time - start_time, output_path)


def cut_remote_clip(media: RemoteMedia, start_time: float, end_time: float, output_path: str) -> str:
    """Cut a clip by seeking into the platform's streams, so only the clip's own bytes are fetched."""
    return _encode_clip(_remote_args(media, start_time), end_time - start_time, output_path)


def burn_hook(input_path: Path, text: str, output_path: Path) -> None:
    """Re-encode a clip with the hook line drawn over its first HOOK_SECONDS."""
    with tempfile.TemporaryDirectory() as tmp:
        text_file = Path(tmp) / "hook.txt"
        text_file.write_text(text, encoding="utf-8")
        _run_ffmpeg([
            "-i", str(input_path),
            "-vf", _hook_filter(text_file, *_frame_size(input_path), len(text)),
            *_h264_args(),
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ])
