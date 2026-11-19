"""Uploading finished clips to the platforms that welcome automated posting through an official API.

`DESTINATIONS` is the only place that knows which platforms exist. A destination lists the credentials it needs,
optionally how to connect an account through OAuth, and how to upload one file. Credentials live in the
AccountStore, never in the library and never in a response to the browser.
"""
import logging
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from .library import Library
from .models import AccountField, AccountInfo, Upload, UploadPrivacy, UploadStatus
from .settings_store import AccountStore, SavedAccount

log = logging.getLogger(__name__)

# Connecting to the platform is quick, but sending a large clip on a slow line can take many minutes.
API_TIMEOUT = 60
UPLOAD_TIMEOUT = (30, 1800)


class UploadError(RuntimeError):
    """The platform refused the upload or the credentials, with a message the user can act on."""


@dataclass(frozen=True)
class UploadJob:
    file: Path
    title: str
    description: str
    tags: list[str]
    privacy: UploadPrivacy


@dataclass(frozen=True)
class UploadResult:
    remote_id: str
    url: str | None
    detail: str


# Saves credential changes a platform hands back during an upload, such as a rotated refresh token.
SaveCredentials = Callable[[dict[str, str]], None]


@dataclass(frozen=True)
class OAuth:
    """How to connect an account in the browser: where to send the user, and how to turn the code into tokens."""
    authorize_url: Callable[[dict[str, str], str, str], str]
    exchange: Callable[[dict[str, str], str, str], dict[str, str]]
    # The credentials that must be saved before Connect can start, such as the OAuth client id and secret.
    needs: tuple[str, ...]


@dataclass(frozen=True)
class Destination:
    id: str
    label: str
    help: str
    fields: tuple[AccountField, ...]
    # Every one of these must be saved before an upload can start.
    required: tuple[str, ...]
    upload: Callable[[UploadJob, dict[str, str], SaveCredentials], UploadResult]
    oauth: OAuth | None = None
    privacy_note: str = ""

    def connected(self, account: SavedAccount) -> bool:
        return all(account.credentials.get(name) for name in self.required)

    def info(self, account: SavedAccount) -> AccountInfo:
        values = {}
        for f in self.fields:
            value = account.credentials.get(f.name)
            values[f.name] = _hint(value) if f.secret else value
        return AccountInfo(
            id=self.id,
            label=self.label,
            help=self.help,
            fields=list(self.fields),
            values=values,
            connected=self.connected(account),
            uses_oauth=self.oauth is not None,
            can_connect=self.oauth is not None and all(account.credentials.get(n) for n in self.oauth.needs),
            auto_upload=account.auto_upload,
            default_privacy=account.default_privacy,
            default_description=account.default_description,
            privacy_note=self.privacy_note,
        )


def _hint(value: str | None) -> str | None:
    if not value:
        return None
    return f"…{value[-4:]}" if len(value) > 8 else "saved"


def _error_message(res: requests.Response) -> str:
    try:
        body = res.json()
    except ValueError:
        return res.text[:300] or res.reason
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return error.get("message") or error.get("code") or str(error)
    if isinstance(error, str):
        description = body.get("error_description")
        return f"{error}: {description}" if description else error
    return str(body)[:300]


def _check(res: requests.Response, label: str) -> requests.Response:
    if not res.ok:
        raise UploadError(f"{label} answered {res.status_code}: {_error_message(res)}")
    return res


def _post_form(url: str, data: dict[str, str], label: str) -> dict:
    return _check(requests.post(url, data=data, timeout=API_TIMEOUT), label).json()


# ---------------------------------------------------------------------------------------------------- YouTube

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_PEOPLE_AND_BLOGS = "22"


def _youtube_authorize(creds: dict[str, str], redirect_uri: str, state: str) -> str:
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_SCOPE,
        # Offline access and a fresh consent are what make Google send a refresh token.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def _youtube_exchange(creds: dict[str, str], code: str, redirect_uri: str) -> dict[str, str]:
    data = _post_form(GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, "Google")
    if not data.get("refresh_token"):
        raise UploadError(
            "Google sent no refresh token. Remove this app's access at https://myaccount.google.com/permissions "
            "and connect again."
        )
    return {"refresh_token": data["refresh_token"]}


def _youtube_upload(job: UploadJob, creds: dict[str, str], _save: SaveCredentials) -> UploadResult:
    token = _post_form(GOOGLE_TOKEN_URL, {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }, "Google")["access_token"]
    size = job.file.stat().st_size
    # YouTube rejects angle brackets in titles and descriptions.
    title = (job.title.replace("<", "").replace(">", "").strip() or job.file.stem)[:100]
    metadata = {
        "snippet": {
            "title": title,
            "description": job.description.replace("<", "").replace(">", "")[:5000],
            "tags": job.tags,
            "categoryId": YOUTUBE_PEOPLE_AND_BLOGS,
        },
        "status": {"privacyStatus": job.privacy.value, "selfDeclaredMadeForKids": False},
    }
    session = _check(requests.post(
        YOUTUBE_UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        json=metadata,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
        timeout=API_TIMEOUT,
    ), "YouTube").headers["Location"]
    with job.file.open("rb") as body:
        res = _check(requests.put(
            session,
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(size)},
            timeout=UPLOAD_TIMEOUT,
        ), "YouTube")
    video_id = res.json()["id"]
    return UploadResult(video_id, f"https://youtu.be/{video_id}", f"Uploaded to YouTube as {job.privacy.value}.")


# ----------------------------------------------------------------------------------------------------- TikTok

TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TIKTOK_SCOPE = "user.info.basic,video.publish"
TIKTOK_PRIVACY = {
    UploadPrivacy.PRIVATE: "SELF_ONLY",
    UploadPrivacy.UNLISTED: "MUTUAL_FOLLOW_FRIENDS",
    UploadPrivacy.PUBLIC: "PUBLIC_TO_EVERYONE",
}
# TikTok takes a file of up to 64 MB in one piece, and larger ones in chunks of 5 to 64 MB.
TIKTOK_SINGLE_CHUNK_MAX = 64 * 1024 * 1024
TIKTOK_CHUNK = 10 * 1024 * 1024
TIKTOK_POLL_SECONDS = 5
TIKTOK_POLL_LIMIT = 36
TIKTOK_CAPTION_MAX = 2200


def _tiktok_authorize(creds: dict[str, str], redirect_uri: str, state: str) -> str:
    return TIKTOK_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": creds["client_key"],
        "scope": TIKTOK_SCOPE,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    })


def _tiktok_exchange(creds: dict[str, str], code: str, redirect_uri: str) -> dict[str, str]:
    data = _post_form(TIKTOK_TOKEN_URL, {
        "client_key": creds["client_key"],
        "client_secret": creds["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, "TikTok")
    if not data.get("refresh_token"):
        raise UploadError(f"TikTok sent no refresh token: {data.get('error_description') or data}")
    return {"refresh_token": data["refresh_token"]}


def _tiktok_api(url: str, token: str, body: dict) -> dict:
    res = requests.post(
        url, json=body, timeout=API_TIMEOUT,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
    )
    data = _check(res, "TikTok").json()
    error = data.get("error") or {}
    if error.get("code") not in (None, "ok"):
        raise UploadError(f"TikTok refused the post: {error.get('message') or error.get('code')}")
    return data.get("data") or {}


def tiktok_chunks(size: int) -> list[tuple[int, int]]:
    """Inclusive byte ranges for TikTok's chunked upload. The last chunk takes the remainder."""
    if size <= TIKTOK_SINGLE_CHUNK_MAX:
        return [(0, size - 1)]
    count = size // TIKTOK_CHUNK
    return [(i * TIKTOK_CHUNK, size - 1 if i == count - 1 else (i + 1) * TIKTOK_CHUNK - 1) for i in range(count)]


def _tiktok_upload(job: UploadJob, creds: dict[str, str], save: SaveCredentials) -> UploadResult:
    tokens = _post_form(TIKTOK_TOKEN_URL, {
        "client_key": creds["client_key"],
        "client_secret": creds["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
    }, "TikTok")
    if tokens.get("refresh_token") and tokens["refresh_token"] != creds["refresh_token"]:
        save({"refresh_token": tokens["refresh_token"]})
    token = tokens["access_token"]

    size = job.file.stat().st_size
    chunks = tiktok_chunks(size)
    hashtags = " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in job.tags if t.strip())
    caption = "\n\n".join(part for part in (job.title.strip(), job.description.strip(), hashtags) if part)
    started = _tiktok_api(TIKTOK_INIT_URL, token, {
        "post_info": {
            "title": caption[:TIKTOK_CAPTION_MAX],
            "privacy_level": TIKTOK_PRIVACY[job.privacy],
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunks[0][1] + 1,
            "total_chunk_count": len(chunks),
        },
    })
    with job.file.open("rb") as body:
        for first, last in chunks:
            body.seek(first)
            _check(requests.put(
                started["upload_url"],
                data=body.read(last - first + 1),
                headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {first}-{last}/{size}"},
                timeout=UPLOAD_TIMEOUT,
            ), "TikTok")

    publish_id = started["publish_id"]
    for _ in range(TIKTOK_POLL_LIMIT):
        status = _tiktok_api(TIKTOK_STATUS_URL, token, {"publish_id": publish_id})
        if status.get("status") == "PUBLISH_COMPLETE":
            return UploadResult(publish_id, None, f"Posted to TikTok as {TIKTOK_PRIVACY[job.privacy]}.")
        if status.get("status") == "FAILED":
            raise UploadError(f"TikTok could not publish the clip: {status.get('fail_reason') or 'no reason given'}")
        time.sleep(TIKTOK_POLL_SECONDS)
    return UploadResult(publish_id, None, "Sent to TikTok. It is still processing there, so check the app shortly.")


# --------------------------------------------------------------------------------------------- Facebook Page

FACEBOOK_GRAPH_VIDEO = "https://graph-video.facebook.com/v21.0"


def _facebook_upload(job: UploadJob, creds: dict[str, str], _save: SaveCredentials) -> UploadResult:
    page_id = creds["page_id"].strip()
    description = "\n\n".join(p for p in (job.description.strip(), " ".join(f"#{t}" for t in job.tags)) if p)
    with job.file.open("rb") as body:
        res = _check(requests.post(
            f"{FACEBOOK_GRAPH_VIDEO}/{urllib.parse.quote(page_id)}/videos",
            data={
                "access_token": creds["page_token"],
                "title": job.title,
                "description": description,
                # A Page video is either published or kept unpublished; there is no unlisted setting.
                "published": "true" if job.privacy is UploadPrivacy.PUBLIC else "false",
            },
            files={"source": (job.file.name, body, "video/mp4")},
            timeout=UPLOAD_TIMEOUT,
        ), "Facebook")
    video_id = str(res.json()["id"])
    state = "published" if job.privacy is UploadPrivacy.PUBLIC else "unpublished"
    return UploadResult(video_id, f"https://www.facebook.com/{page_id}/videos/{video_id}",
                        f"Uploaded to the Facebook Page, {state}.")


# ----------------------------------------------------------------------------------------------------- table

DESTINATIONS: dict[str, Destination] = {
    d.id: d for d in (
        Destination(
            id="youtube",
            label="YouTube",
            help=(
                "Create an OAuth client of type Desktop app in Google Cloud Console, enable the YouTube Data API v3, "
                "and add yourself as a test user. Save its client ID and secret here, then choose Connect."
            ),
            fields=(
                AccountField(name="client_id", label="OAuth client ID", secret=False),
                AccountField(name="client_secret", label="OAuth client secret", secret=True),
                AccountField(name="refresh_token", label="Refresh token", secret=True,
                             help="Filled in by Connect. Paste one only if you made it elsewhere."),
            ),
            required=("client_id", "client_secret", "refresh_token"),
            upload=_youtube_upload,
            oauth=OAuth(_youtube_authorize, _youtube_exchange, needs=("client_id", "client_secret")),
            privacy_note="Vertical clips up to 3 minutes show as Shorts. Unverified API projects can only upload private videos.",
        ),
        Destination(
            id="tiktok",
            label="TikTok",
            help=(
                "Create an app at developers.tiktok.com with Login Kit and the Content Posting API (Direct Post), "
                "add this page's callback URL as a redirect URI, and save the client key and secret here."
            ),
            fields=(
                AccountField(name="client_key", label="Client key", secret=False),
                AccountField(name="client_secret", label="Client secret", secret=True),
                AccountField(name="refresh_token", label="Refresh token", secret=True,
                             help="Filled in by Connect. TikTok replaces it from time to time, and this app keeps up."),
            ),
            required=("client_key", "client_secret", "refresh_token"),
            upload=_tiktok_upload,
            oauth=OAuth(_tiktok_authorize, _tiktok_exchange, needs=("client_key", "client_secret")),
            privacy_note="Until TikTok audits your app, every post is private (only you can see it).",
        ),
        Destination(
            id="facebook",
            label="Facebook Page",
            help=(
                "Get a long-lived Page access token with pages_manage_posts and publish_video, "
                "for example from the Graph API Explorer, and save it with the Page ID."
            ),
            fields=(
                AccountField(name="page_id", label="Page ID", secret=False),
                AccountField(name="page_token", label="Page access token", secret=True),
            ),
            required=("page_id", "page_token"),
            upload=_facebook_upload,
            privacy_note="Public publishes the video on the Page. Private and Unlisted keep it unpublished.",
        ),
    )
}


def execute(upload: Upload, *, library: Library, accounts: AccountStore, clips_dir: Path) -> None:
    """Send one queued upload to its platform. The upload worker is its only writer."""
    destination = DESTINATIONS[upload.platform]
    clip = library.get_clip(upload.clip_id)
    upload.status = UploadStatus.UPLOADING
    library.save_upload(upload)
    try:
        if clip is None:
            raise UploadError("The clip was deleted before it could be uploaded.")
        account = accounts.get(upload.platform)
        if not destination.connected(account):
            raise UploadError(f"{destination.label} is not connected. Set it up on the Publish page.")
        result = destination.upload(
            UploadJob(clips_dir / clip.file_name, upload.title, upload.description, upload.tags, upload.privacy),
            account.credentials,
            lambda changes: accounts.update_credentials(upload.platform, changes),
        )
        upload.status, upload.remote_url, upload.detail = UploadStatus.DONE, result.url, result.detail
    except Exception as exc:
        log.warning("Upload %s to %s failed", upload.upload_id, upload.platform, exc_info=True)
        upload.status, upload.detail = UploadStatus.FAILED, str(exc) or type(exc).__name__
    upload.finished_at = time.time()
    library.save_upload(upload)
