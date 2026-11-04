"""Stream mode: the preview and every clip read the platform's stream, and nothing but clips is written to disk.

Local HLS playlists stand in for the platform, so these run offline through the same ffmpeg seek path."""
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipstudio import app as app_module
from clipstudio import clip_engine
from clipstudio.app import CLIPS_DIR, MEDIA_DIR, app, library
from clipstudio.clip_engine import RemoteInput, RemoteMedia

client = TestClient(app)
URL = "https://www.youtube.com/watch?v=strmtest001"
KEY = "youtube_strmtest001"
DURATION = 24
# MPEG-TS has no global header, so each keyframe repeats the mpeg4 header for a reader that starts mid-stream.
MPEG4_IN_TS = ["-bsf:v", "dump_extra=freq=keyframe"]


def _probe(path: Path | str, entries: str) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def hls(tmp_path_factory) -> dict[str, Path]:
    """A video-only and an audio-only HLS stream, the way YouTube serves them, plus one muxed like Twitch."""
    root = tmp_path_factory.mktemp("hls")
    video = ["-f", "lavfi", "-i", f"testsrc=duration={DURATION}:size=320x240:rate=30"]
    audio = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}"]
    hls_out = ["-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod"]
    jobs = {
        "video": [*video, "-c:v", "mpeg4", "-g", "30", *MPEG4_IN_TS, *hls_out],
        "audio": [*audio, "-c:a", "aac", *hls_out],
        "muxed": [*video, *audio, "-c:v", "mpeg4", "-g", "30", *MPEG4_IN_TS, "-c:a", "aac", *hls_out],
    }
    playlists = {}
    for name, args in jobs.items():
        folder = root / name
        folder.mkdir()
        playlists[name] = folder / "index.m3u8"
        subprocess.run(["ffmpeg", "-y", *args, str(playlists[name])], check=True, capture_output=True)
    return playlists


def _split(hls) -> RemoteMedia:
    return RemoteMedia(RemoteInput(str(hls["video"])), RemoteInput(str(hls["audio"])), float(DURATION))


def test_playlist_covers_the_whole_video_in_numbered_segments():
    playlist = clip_engine.preview_playlist(25.0)
    assert playlist.startswith("#EXTM3U\n") and playlist.endswith("#EXT-X-ENDLIST\n")
    assert [line for line in playlist.splitlines() if line.endswith(".ts")] == ["0.ts", "1.ts", "2.ts"]
    assert "#EXTINF:5.000," in playlist
    assert clip_engine.preview_segment_count(30.0) == 3


def test_remote_args_pass_platform_headers_and_skip_a_seek_to_zero():
    remote = RemoteInput("https://example.com/v.m3u8", {"User-Agent": "UA", "Referer": "https://example.com/"})
    args = clip_engine._remote_args(RemoteMedia(remote, remote, 10.0), 0)
    assert "-ss" not in args
    assert args[args.index("-user_agent") + 1] == "UA"
    assert args[args.index("-headers") + 1] == "Referer: https://example.com/\r\n"
    assert args[-4:] == ["-map", "0:v:0", "-map", "1:a:0"]
    muxed = clip_engine._remote_args(RemoteMedia(remote, None, 10.0), 12.5)
    assert muxed[:2] == ["-ss", "12.500"] and muxed[-4:] == ["-map", "0:v:0", "-map", "0:a:0?"]


@pytest.mark.parametrize("layout", ["split", "muxed"])
def test_remote_cut_keeps_video_and_audio_for_the_exact_range(hls, tmp_path, layout):
    media = _split(hls) if layout == "split" else RemoteMedia(RemoteInput(str(hls["muxed"])), None, float(DURATION))
    out = tmp_path / "clip.mp4"
    clip_engine.cut_remote_clip(media, 9.0, 15.0, str(out))
    codecs = _probe(out, "stream=codec_type,codec_name")
    assert sorted(codecs) == ["aac,audio", "h264,video"]
    assert float(_probe(out, "format=duration")[0]) == pytest.approx(6.0, abs=0.15)


def test_preview_segment_is_low_res_mpegts_placed_on_the_video_timeline(hls, tmp_path):
    seg = tmp_path / "1.ts"
    seg.write_bytes(clip_engine.preview_segment(_split(hls), 1))
    streams = _probe(seg, "stream=codec_name,height,start_time")
    video = next(s for s in streams if s.startswith("h264"))
    audio = next(s for s in streams if s.startswith("aac"))
    assert video.split(",")[1] == "240"
    # The muxer adds a constant 1.4 s, which the player cancels out, so segment 1 sits 10 s after segment 0.
    assert float(video.split(",")[2]) == pytest.approx(clip_engine.PREVIEW_SEGMENT_SECONDS + 1.4, abs=0.1)
    assert float(audio.split(",")[-1]) == pytest.approx(clip_engine.PREVIEW_SEGMENT_SECONDS + 1.4, abs=0.1)


@pytest.fixture
def opened(hls, monkeypatch):
    media = _split(hls)
    monkeypatch.setattr(clip_engine, "resolve_stream", lambda source: media)
    res = client.post("/api/stream", json={"url": URL})
    assert res.status_code == 200
    yield res.json()
    app_module.streams.pop(KEY, None)


def test_open_stream_serves_a_playlist_and_segments_without_downloading(opened):
    assert opened == {"video_id": KEY, "stream_url": f"/stream/{KEY}/index.m3u8", "duration": DURATION, "status": "ready"}
    playlist = client.get(opened["stream_url"])
    assert playlist.status_code == 200 and playlist.headers["content-type"] == "application/vnd.apple.mpegurl"
    segment = client.get(f"/stream/{KEY}/0.ts")
    assert segment.status_code == 200 and segment.headers["content-type"] == "video/mp2t" and segment.content
    assert client.get(f"/stream/{KEY}/3.ts").status_code == 404
    assert not (MEDIA_DIR / f"{KEY}.mp4").exists()


def test_stream_mode_export_cuts_from_the_stream_into_the_library(opened):
    res = client.post("/api/clip", json={"video_id": KEY, "start_time": 2.0, "end_time": 5.0, "mode": "stream"})
    assert res.status_code == 200, res.text
    clip = res.json()
    path = CLIPS_DIR / clip["file_name"]
    try:
        assert sorted(_probe(path, "stream=codec_type")) == ["audio", "video"]
        assert library.get_clip(clip["clip_id"]) is not None
    finally:
        path.unlink(missing_ok=True)
        library.delete_clip(clip["clip_id"])


def test_a_stream_the_server_does_not_hold_asks_to_load_again():
    for path in (f"/stream/{KEY}/index.m3u8", f"/stream/{KEY}/0.ts"):
        assert client.get(path).status_code == 404
    res = client.post("/api/clip", json={"video_id": KEY, "start_time": 0, "end_time": 1, "mode": "stream"})
    assert res.status_code == 404 and "Load the video again" in res.json()["detail"]


def test_stale_stream_urls_are_looked_up_again(opened, hls, monkeypatch):
    calls = []
    monkeypatch.setattr(clip_engine, "resolve_stream", lambda source: calls.append(source.url) or _split(hls))
    app_module.streams[KEY].resolved_at = time.time() - app_module.STREAM_URL_MAX_AGE - 1
    assert client.get(f"/stream/{KEY}/index.m3u8").status_code == 200
    assert calls == [URL]


def test_a_video_without_a_length_cannot_be_streamed(monkeypatch):
    monkeypatch.setattr(clip_engine, "resolve_stream", lambda source: RemoteMedia(RemoteInput("x"), None, 0.0))
    res = client.post("/api/stream", json={"url": URL})
    assert res.status_code == 400 and "Download it" in res.json()["detail"]
    assert KEY not in app_module.streams


def _fmt(fid, height, vcodec="avc1.4D401E", acodec="none", protocol="m3u8_native"):
    return {"format_id": fid, "url": f"https://x/{fid}", "height": height, "vcodec": vcodec, "acodec": acodec,
            "protocol": protocol}


def test_preview_reads_the_largest_matching_rendition_up_to_480p():
    clip_video = _fmt("1080", 1080)
    formats = [
        _fmt("360", 360), _fmt("480vp9", 480, vcodec="vp09"), _fmt("480", 480), _fmt("720", 720), clip_video,
        _fmt("480dash", 480, protocol="https"), _fmt("480muxed", 480, acodec="mp4a.40.2"), _fmt("audio", None, "none"),
    ]
    assert clip_engine._preview_format(formats, clip_video, split=True)["format_id"] == "480"
    assert clip_engine._preview_format(formats, clip_video, split=False)["format_id"] == "480muxed"
    assert clip_engine._preview_format([clip_video], clip_video, split=True) is None


def test_preview_segments_read_the_preview_rendition_and_clips_the_full_one():
    full, small = RemoteInput("https://x/1080.m3u8"), RemoteInput("https://x/480.m3u8")
    media = RemoteMedia(full, None, 10.0, preview_video=small)
    assert media.for_preview().video is small and media.video is full
    assert RemoteMedia(full, None, 10.0).for_preview().video is full


SOURCE_PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-MAP:URI="init.mp4"
#EXTINF:10.0,
0.ts

#EXTINF:4.5,
https://cdn.example.com/abs/1-muted.ts
#EXT-X-ENDLIST
"""


def test_proxied_playlist_points_every_uri_back_at_the_server():
    text, uris = clip_engine.proxy_playlist(SOURCE_PLAYLIST, "https://vod.example.com/a/b/index.m3u8", "video")
    assert uris == [
        "https://vod.example.com/a/b/init.mp4", "https://vod.example.com/a/b/0.ts", "https://cdn.example.com/abs/1-muted.ts",
    ]
    assert '#EXT-X-MAP:URI="video/0"' in text and "\nvideo/1\n" in text and "\nvideo/2\n" in text
    assert "example.com" not in text


def test_master_playlist_joins_a_separate_audio_track():
    assert 'URI="audio.m3u8"' in clip_engine.proxy_master_playlist({"video": RemoteInput("v"), "audio": RemoteInput("a")})
    assert "audio" not in clip_engine.proxy_master_playlist({"video": RemoteInput("v")})


class _FakeResponse:
    def __init__(self, url, body):
        self.url, self.content, self.text, self.headers = url, body, body.decode(), {"content-type": "video/mp2t"}


def test_an_h264_hls_stream_is_previewed_through_the_server_without_ffmpeg(monkeypatch):
    video = RemoteInput("https://vod.example.com/v/index.m3u8", {"Referer": "r"}, "m3u8_native", "avc1.4D401E")
    audio = RemoteInput("https://vod.example.com/a/index.m3u8", {}, "m3u8_native", "mp4a.40.2")
    monkeypatch.setattr(clip_engine, "resolve_stream", lambda source: RemoteMedia(video, audio, 14.5))
    fetched = []

    def fake_fetch(remote, url=None):
        fetched.append((remote, url or remote.url))
        body = SOURCE_PLAYLIST.encode() if (url or remote.url).endswith(".m3u8") else b"segment-bytes"
        return _FakeResponse(url or remote.url, body)

    monkeypatch.setattr(clip_engine, "fetch_remote", fake_fetch)
    monkeypatch.setattr(clip_engine, "preview_segment", lambda *a: pytest.fail("the proxy path must not run ffmpeg"))
    try:
        assert client.post("/api/stream", json={"url": URL}).status_code == 200
        master = client.get(f"/stream/{KEY}/index.m3u8").text
        assert "video.m3u8" in master and 'URI="audio.m3u8"' in master
        assert "audio/1" in client.get(f"/stream/{KEY}/audio.m3u8").text
        # A segment asked for before its playlist, as after a server restart, loads the playlist first.
        segment = client.get(f"/stream/{KEY}/video/2")
        assert segment.status_code == 200 and segment.content == b"segment-bytes"
        assert fetched[-1] == (video, "https://cdn.example.com/abs/1-muted.ts")
        assert client.get(f"/stream/{KEY}/video/3").status_code == 404
        assert client.get(f"/stream/{KEY}/subtitles.m3u8").status_code == 404
        assert client.get(f"/stream/{KEY}/0.ts").status_code == 404
    finally:
        app_module.streams.pop(KEY, None)
