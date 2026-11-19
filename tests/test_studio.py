import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipstudio import app as app_module
from clipstudio import captions, clip_engine
from clipstudio.app import CLIPS_DIR, app, library
from clipstudio.models import (
    Aspect, CaptionPosition, CaptionSegment, CaptionStyle, Clip, EditSpec, Fit,
)
from clipstudio.sources import source_from_media_key

client = TestClient(app)

SOURCE_CAPTIONS = [
    CaptionSegment(start=8.0, end=9.5, text="before the clip"),
    CaptionSegment(start=9.5, end=11.0, text="first line"),
    CaptionSegment(start=11.0, end=13.0, text="second {line}"),
    CaptionSegment(start=13.5, end=16.0, text="runs past the end"),
]


def _frame(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


@pytest.fixture
def library_clip():
    """A 4-second 640x360 clip in the library, cut from 10 s to 14 s of a YouTube video."""
    clip_id = uuid.uuid4().hex[:8]
    path = CLIPS_DIR / f"{clip_id}_studio_test.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=25",
         "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "mpeg4", "-q:v", "3", "-c:a", "aac", str(path)],
        check=True, capture_output=True,
    )
    clip = Clip(
        clip_id=clip_id, video_id="youtube_jNQXAC9IVRw", start_time=10.0, end_time=14.0, file_name=path.name,
        captions=SOURCE_CAPTIONS, created_at=time.time(),
    )
    library.add_clip(clip)
    yield clip
    path.unlink(missing_ok=True)
    library.delete_clip(clip_id)


def test_clip_captions_move_onto_the_clip_clock_and_stop_at_its_ends():
    lines = captions.clip_captions(SOURCE_CAPTIONS, 10.0, 14.0)
    assert [(c.start, c.end, c.text) for c in lines] == [
        (0.0, 1.0, "first line"), (1.0, 3.0, "second {line}"), (3.5, 4.0, "runs past the end"),
    ]


def test_ass_values():
    assert captions.ass_color("#ff8000") == "&H000080FF"
    assert captions.ass_color("#000000", opacity=0.0) == "&HFF000000"
    assert captions.ass_time(3725.456) == "1:02:05.46"
    assert captions.ass_text("a {b} \\c\nnext", uppercase=True) == "A (B) /C\\NNEXT"


def test_build_ass_sizes_text_by_the_frame_and_keeps_the_title_up_all_clip():
    style = CaptionStyle(size=10, margin=20, position=CaptionPosition.TOP, box=True, font="Not A Font")
    ass = captions.build_ass(
        [CaptionSegment(start=0.5, end=1.5, text="hello")], style, 1080, 1920, title="Title!", duration=4.0,
    )
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    caption_style = next(line for line in ass.splitlines() if line.startswith("Style: Caption,"))
    fields = caption_style.removeprefix("Style: ").split(",")
    assert fields[1] == "Arial", "an unknown font falls back to the first offered font"
    assert fields[2] == "192", "10% of a 1920-pixel frame"
    assert fields[15] == "3", "a box is BorderStyle 3"
    assert fields[18] == "8", "top centre"
    assert fields[21] == "384", "20% margin"
    assert "Dialogue: 1,0:00:00.00,0:00:04.00,Title,,0,0,0,,Title!" in ass
    assert "Dialogue: 0,0:00:00.50,0:00:01.50,Caption,,0,0,0,,hello" in ass


@pytest.mark.parametrize("aspect, fit, expected", [
    (Aspect.SOURCE, Fit.CROP, (1280, 720)),
    (Aspect.PORTRAIT, Fit.CROP, (404, 720)),
    (Aspect.PORTRAIT, Fit.FIT, (1080, 1920)),
    (Aspect.SQUARE, Fit.CROP, (720, 720)),
    (Aspect.SQUARE, Fit.FIT, (1280, 1280)),
    (Aspect.LANDSCAPE, Fit.CROP, (1280, 720)),
])
def test_edit_frame_crops_inside_or_fits_around_and_stays_under_the_cap(aspect, fit, expected):
    assert clip_engine.edit_frame(1280, 720, aspect, fit) == expected


def test_edit_filters_follow_the_spec():
    spec = EditSpec(aspect=Aspect.PORTRAIT, focus_x=25, border=2, brightness=0.1, volume=0, fade=True)
    video, audio = clip_engine.edit_filters(spec, 1280, 720, 4.0, Path("x.ass"))
    assert video.startswith("crop=404:720:(iw-404)*0.2500:(ih-720)*0.5000,scale=404:720")
    assert "eq=brightness=0.100" in video and "drawbox=" in video and "subtitles=filename=" in video
    assert "fade=t=in" in video and video.endswith("setsar=1")
    assert audio is None, "volume 0 mutes the export"

    plain_video, plain_audio = clip_engine.edit_filters(EditSpec(), 1280, 720, 4.0, None)
    assert plain_video == "setsar=1" and plain_audio == "anull"


def test_edit_spec_rejects_bad_values():
    with pytest.raises(ValueError):
        EditSpec(trim_start=3, trim_end=2)
    with pytest.raises(ValueError):
        EditSpec(border_color="red")


def test_studio_options_offer_fonts_and_defaults():
    data = client.get("/api/studio").json()
    assert data["fonts"][0] == "Arial"
    assert EditSpec.model_validate(data["defaults"]) == EditSpec()


def test_render_exports_a_reshaped_captioned_clip_as_a_new_library_clip(library_clip):
    spec = {
        "trim_start": 1.0, "trim_end": 3.5, "aspect": "9:16", "border": 2, "title": "Title",
        "captions": [{"start": 1.2, "end": 2.5, "text": "burned in"}], "fade": True,
    }
    res = client.post(f"/api/clips/{library_clip.clip_id}/render", json=spec)
    assert res.status_code == 200, res.text
    made = res.json()
    output = CLIPS_DIR / made["file_name"]
    try:
        assert made["clip_id"] != library_clip.clip_id and made["file_name"].endswith("_studio.mp4")
        assert (made["start_time"], made["end_time"]) == (11.0, 13.5), "times stay in source-video seconds"
        assert _frame(output) == (202, 360)
        assert abs(clip_engine.media_duration(output) - 2.5) < 0.15
        assert library.get_clip(made["clip_id"]) is not None
    finally:
        output.unlink(missing_ok=True)
        library.delete_clip(made["clip_id"])


def test_render_of_an_unknown_clip_is_404():
    assert client.post("/api/clips/nope/render", json={}).status_code == 404


def test_auto_captions_use_the_saved_lines_first(library_clip, monkeypatch):
    monkeypatch.setattr(clip_engine, "fetch_captions", lambda source: pytest.fail("should not fetch"))
    data = client.post(f"/api/clips/{library_clip.clip_id}/auto-captions").json()
    assert data["fetched"] is False
    assert [c["text"] for c in data["captions"]] == ["first line", "second {line}", "runs past the end"]


def test_auto_captions_fetch_and_keep_them_when_the_clip_has_none(library_clip, monkeypatch):
    library.set_captions(library_clip.clip_id, [])
    asked = []

    def fetch(source):
        asked.append(source.url)
        return SOURCE_CAPTIONS

    monkeypatch.setattr(clip_engine, "fetch_captions", fetch)
    data = client.post(f"/api/clips/{library_clip.clip_id}/auto-captions").json()
    assert asked == ["https://www.youtube.com/watch?v=jNQXAC9IVRw"]
    assert data["fetched"] is True and data["captions"][0] == {"start": 0.0, "end": 1.0, "text": "first line"}
    assert [c.text for c in library.get_clip(library_clip.clip_id).captions] == [c.text for c in SOURCE_CAPTIONS]


def test_auto_captions_explain_platforms_without_subtitles(library_clip):
    twitch = library_clip.model_copy(update={"clip_id": library_clip.clip_id + "t", "video_id": "twitch_123",
                                             "captions": []})
    library.add_clip(twitch)
    try:
        res = client.post(f"/api/clips/{twitch.clip_id}/auto-captions")
        assert res.status_code == 400 and "no subtitles" in res.json()["detail"]
    finally:
        library.delete_clip(twitch.clip_id)


def test_source_from_media_key():
    assert source_from_media_key("youtube_jNQXAC9IVRw").url == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert source_from_media_key("twitch_123") is None
    assert source_from_media_key("vimeo_1") is None
    assert source_from_media_key("youtube_bad") is None


def test_render_queues_uploads_for_accounts_with_auto_upload(library_clip, monkeypatch):
    queued = []
    monkeypatch.setattr(app_module, "_queue_auto_uploads", queued.append)
    res = client.post(f"/api/clips/{library_clip.clip_id}/render", json={})
    made = res.json()
    try:
        assert [c.clip_id for c in queued] == [made["clip_id"]]
    finally:
        (CLIPS_DIR / made["file_name"]).unlink(missing_ok=True)
        library.delete_clip(made["clip_id"])
