import dataclasses
import time
import urllib.parse
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipstudio import app as app_module
from clipstudio import uploads
from clipstudio.app import CLIPS_DIR, DB_PATH, accounts, app, library
from clipstudio.library import UPLOAD_INTERRUPTED, Library
from clipstudio.models import Clip, Upload, UploadPrivacy, UploadStatus
from clipstudio.settings_store import SavedAccount

client = TestClient(app, follow_redirects=False)

SECRET = "super-secret-refresh-token-9876"


@pytest.fixture(autouse=True)
def clean_accounts():
    for platform in uploads.DESTINATIONS:
        accounts.forget(platform)
    yield
    for platform in uploads.DESTINATIONS:
        accounts.forget(platform)


@pytest.fixture
def clip():
    clip_id = uuid.uuid4().hex[:8]
    path = CLIPS_DIR / f"{clip_id}_upload_test.mp4"
    path.write_bytes(b"\x00" * 2048)
    made = Clip(clip_id=clip_id, video_id="youtube_jNQXAC9IVRw", start_time=0, end_time=2, file_name=path.name,
                captions=[], hook="A hook line", created_at=time.time())
    library.add_clip(made)
    yield made
    path.unlink(missing_ok=True)
    library.delete_clip(clip_id)


@pytest.fixture
def fake_youtube(monkeypatch):
    """Replace YouTube's upload call, recording each job."""
    jobs = []

    def upload(job, creds, save):
        jobs.append((job, dict(creds)))
        save({"refresh_token": "rotated"})
        return uploads.UploadResult("vid123", "https://youtu.be/vid123", "Uploaded.")

    monkeypatch.setitem(uploads.DESTINATIONS, "youtube", dataclasses.replace(uploads.DESTINATIONS["youtube"], upload=upload))
    return jobs


def _connect_youtube(**settings):
    client.put("/api/accounts/youtube", json={
        "credentials": {"client_id": "cid.apps", "client_secret": "csecret-1234", "refresh_token": SECRET},
        **settings,
    })


def _flush_uploads():
    app_module.uploader.submit(lambda: None).result(timeout=30)


def test_accounts_never_send_a_secret_back():
    _connect_youtube()
    youtube = next(a for a in client.get("/api/accounts").json() if a["id"] == "youtube")
    assert youtube["connected"] is True and youtube["uses_oauth"] is True
    assert youtube["values"] == {"client_id": "cid.apps", "client_secret": "…1234", "refresh_token": "…9876"}
    assert SECRET not in client.get("/api/accounts").text


def test_saving_a_blank_secret_keeps_the_saved_one():
    _connect_youtube()
    client.put("/api/accounts/youtube", json={"credentials": {"client_secret": "", "refresh_token": "  "},
                                              "auto_upload": True})
    saved = accounts.get("youtube")
    assert saved.credentials["refresh_token"] == SECRET and saved.auto_upload is True


def test_unknown_platforms_and_fields_are_refused():
    assert client.get("/api/accounts/myspace/connect").status_code == 404
    res = client.put("/api/accounts/youtube", json={"credentials": {"password": "x"}})
    assert res.status_code == 400 and "password" in res.json()["detail"]


def test_upload_needs_a_connected_account(clip):
    res = client.post("/api/uploads", json={"clip_id": clip.clip_id, "platform": "youtube"})
    assert res.status_code == 400 and "Connect YouTube" in res.json()["detail"]


def test_upload_runs_in_the_worker_and_records_the_link(clip, fake_youtube):
    _connect_youtube()
    res = client.post("/api/uploads", json={
        "clip_id": clip.clip_id, "platform": "youtube", "title": "", "tags": [" a ", ""], "privacy": "unlisted",
    })
    assert res.status_code == 202, res.text
    _flush_uploads()
    done = library.get_upload(res.json()["upload_id"])
    assert done.status is UploadStatus.DONE and done.remote_url == "https://youtu.be/vid123"
    job, creds = fake_youtube[0]
    assert job.title == "A hook line", "a blank title falls back to the clip's hook"
    assert job.tags == ["a"] and job.privacy is UploadPrivacy.UNLISTED
    assert job.file == CLIPS_DIR / clip.file_name and creds["refresh_token"] == SECRET
    assert accounts.get("youtube").credentials["refresh_token"] == "rotated"
    assert SECRET not in client.get("/api/uploads").text


def test_a_failed_upload_keeps_the_reason_and_can_be_retried(clip, monkeypatch):
    def refuse(job, creds, save):
        raise uploads.UploadError("YouTube answered 403: quota exceeded")

    monkeypatch.setitem(uploads.DESTINATIONS, "youtube", dataclasses.replace(uploads.DESTINATIONS["youtube"], upload=refuse))
    _connect_youtube()
    upload_id = client.post("/api/uploads", json={"clip_id": clip.clip_id, "platform": "youtube"}).json()["upload_id"]
    _flush_uploads()
    failed = library.get_upload(upload_id)
    assert failed.status is UploadStatus.FAILED and "quota" in failed.detail
    retried = client.post(f"/api/uploads/{upload_id}/retry")
    assert retried.status_code == 202 and retried.json()["upload_id"] != upload_id
    _flush_uploads()
    assert client.delete(f"/api/uploads/{upload_id}").status_code == 200


def test_a_clip_being_uploaded_cannot_be_deleted(clip):
    upload_id = uuid.uuid4().hex[:8]
    library.add_upload(Upload(upload_id=upload_id, clip_id=clip.clip_id, platform="youtube", title="t",
                              status=UploadStatus.UPLOADING, created_at=time.time()))
    try:
        assert client.delete(f"/api/clips/{clip.clip_id}").status_code == 400
    finally:
        library.delete_upload(upload_id)


def test_auto_upload_queues_only_connected_accounts_that_want_it(clip, fake_youtube):
    _connect_youtube(auto_upload=True, default_privacy="public", default_description="from the app")
    client.put("/api/accounts/tiktok", json={"credentials": {"client_key": "k"}, "auto_upload": True})
    app_module._queue_auto_uploads(clip)
    _flush_uploads()
    mine = [u for u in library.list_uploads() if u.clip_id == clip.clip_id]
    assert [(u.platform, u.automatic, u.privacy, u.description) for u in mine] == \
        [("youtube", True, UploadPrivacy.PUBLIC, "from the app")]


def test_connect_sends_the_user_to_google_and_the_callback_saves_the_token(monkeypatch):
    client.put("/api/accounts/youtube", json={"credentials": {"client_id": "cid.apps", "client_secret": "cs"}})
    res = client.get("/api/accounts/youtube/connect")
    assert res.status_code == 307
    target = urllib.parse.urlsplit(res.headers["location"])
    query = dict(urllib.parse.parse_qsl(target.query))
    assert target.netloc == "accounts.google.com"
    assert query["client_id"] == "cid.apps" and query["access_type"] == "offline"
    assert query["redirect_uri"].endswith("/api/accounts/youtube/callback")

    exchanged = []

    def exchange(creds, code, redirect_uri):
        exchanged.append((code, redirect_uri))
        return {"refresh_token": "fresh"}

    destination = uploads.DESTINATIONS["youtube"]
    monkeypatch.setitem(uploads.DESTINATIONS, "youtube", dataclasses.replace(
        destination, oauth=dataclasses.replace(destination.oauth, exchange=exchange)))
    page = client.get("/api/accounts/youtube/callback", params={"state": query["state"], "code": "abc"})
    assert "connected" in page.text
    assert exchanged == [("abc", query["redirect_uri"])]
    assert accounts.get("youtube").credentials["refresh_token"] == "fresh"
    again = client.get("/api/accounts/youtube/callback", params={"state": query["state"], "code": "abc"})
    assert "expired" in again.text, "a state is used once"


def test_tiktok_chunks():
    assert uploads.tiktok_chunks(1000) == [(0, 999)]
    size = 100 * 1024 * 1024 + 123
    chunks = uploads.tiktok_chunks(size)
    assert chunks[0] == (0, uploads.TIKTOK_CHUNK - 1) and chunks[-1][1] == size - 1
    assert all(b[0] == a[1] + 1 for a, b in zip(chunks, chunks[1:]))


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.ok = status < 400
        self.text, self.reason = str(self._body), "reason"

    def json(self):
        return self._body


def test_youtube_upload_talks_to_the_resumable_api(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * 10)
    calls = []

    def post(url, **kw):
        calls.append(("POST", url, kw))
        if url == uploads.GOOGLE_TOKEN_URL:
            return FakeResponse(body={"access_token": "at"})
        return FakeResponse(headers={"Location": "https://upload.example/session"})

    def put(url, **kw):
        calls.append(("PUT", url, kw))
        return FakeResponse(body={"id": "abc"})

    monkeypatch.setattr(uploads.requests, "post", post)
    monkeypatch.setattr(uploads.requests, "put", put)
    result = uploads._youtube_upload(
        uploads.UploadJob(video, "Title <b>", "desc", ["t"], UploadPrivacy.PRIVATE),
        {"client_id": "c", "client_secret": "s", "refresh_token": "r"}, lambda changes: None,
    )
    assert result.url == "https://youtu.be/abc"
    token_call, start_call, put_call = calls
    assert token_call[2]["data"]["grant_type"] == "refresh_token"
    assert start_call[2]["json"]["snippet"]["title"] == "Title b"
    assert start_call[2]["json"]["status"]["privacyStatus"] == "private"
    assert start_call[2]["headers"]["X-Upload-Content-Length"] == "10"
    assert put_call[1] == "https://upload.example/session"


def test_platform_errors_reach_the_user():
    with pytest.raises(uploads.UploadError, match="invalid_grant: Token has been expired"):
        uploads._check(FakeResponse(400, {"error": "invalid_grant", "error_description": "Token has been expired"}),
                       "Google")


def test_opening_the_library_fails_uploads_a_previous_process_left_open():
    upload_id = uuid.uuid4().hex[:8]
    library.add_upload(Upload(upload_id=upload_id, clip_id="gone", platform="youtube", title="t",
                              status=UploadStatus.QUEUED, created_at=time.time()))
    reopened = Library(Path(DB_PATH))
    upload = reopened.get_upload(upload_id)
    assert upload.status is UploadStatus.FAILED and upload.detail == UPLOAD_INTERRUPTED
    reopened.delete_upload(upload_id)


def test_saved_account_defaults():
    assert accounts.get("tiktok") == SavedAccount()
