import re
import urllib.parse
from dataclasses import dataclass
from typing import Literal

from .models import Platform

SourceKind = Literal["video", "clip"]


class UnsupportedSourceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceRef:
    platform: Platform
    kind: SourceKind
    id: str
    url: str

    @property
    def media_key(self) -> str:
        return f"{self.platform}_{self.id}"


# Each pattern matches "<host><path>?<query>" with the www./m./go. host prefix removed.
# re.ASCII keeps \w to [A-Za-z0-9_] so every id is safe inside a media key.
_SOURCE_PATTERNS: tuple[tuple[Platform, SourceKind, re.Pattern[str]], ...] = tuple(
    (platform, kind, re.compile(pattern, re.ASCII))
    for platform, kind, pattern in [
        (Platform.YOUTUBE, "video", r"youtube\.com/watch/?\?(?:.*&)?v=(?P<id>[\w-]{11})(?:&|$)"),
        (Platform.YOUTUBE, "video", r"youtube\.com/(?:shorts|embed|live)/(?P<id>[\w-]{11})/?\?"),
        (Platform.YOUTUBE, "video", r"youtu\.be/(?P<id>[\w-]{11})/?\?"),
        (Platform.TWITCH, "video", r"twitch\.tv/(?:\w+/v|videos)/(?P<id>\d+)/?\?"),
        (Platform.TWITCH, "clip", r"clips\.twitch\.tv/(?P<id>[\w-]+)/?\?"),
        (Platform.TWITCH, "clip", r"twitch\.tv/\w+/clip/(?P<id>[\w-]+)/?\?"),
        (Platform.KICK, "video", r"kick\.com/[\w-]+/videos/(?P<id>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?\?"),
        (Platform.KICK, "clip", r"kick\.com/[\w-]+/clips/(?P<id>clip_\w+)/?\?"),
        (Platform.KICK, "clip", r"kick\.com/[\w-]+/?\?(?:.*&)?clip=(?P<id>clip_\w+)(?:&|$)"),
    ]
)

_UNSUPPORTED_MESSAGE = (
    "Unsupported link. Paste a YouTube video (watch, youtu.be, shorts, embed or past live stream), "
    "a Twitch VOD (twitch.tv/videos/...) or clip (clips.twitch.tv/... or twitch.tv/<channel>/clip/...), "
    "or a Kick VOD (kick.com/<channel>/videos/...) or clip (kick.com/<channel>/clips/...). "
    "Live channel pages are not supported."
)


# The link to read a video again from its media key alone. Only platforms with subtitles need a row:
# a clip keeps only its media key, and subtitles are the one thing a clip fetches from its video later.
_VIDEO_URLS: dict[Platform, str] = {
    Platform.YOUTUBE: "https://www.youtube.com/watch?v={id}",
}


def source_from_media_key(media_key: str) -> SourceRef | None:
    """The source a media key came from, or None when the platform has no row in _VIDEO_URLS."""
    platform_name, _, media_id = media_key.partition("_")
    try:
        platform = Platform(platform_name)
    except ValueError:
        return None
    template = _VIDEO_URLS.get(platform)
    if template is None or not media_id:
        return None
    try:
        return parse_source(template.format(id=media_id))
    except UnsupportedSourceError:
        return None


def parse_source(url: str) -> SourceRef:
    text = url.strip()
    if "://" not in text:
        text = f"https://{text}"
    parsed = urllib.parse.urlsplit(text)
    host = re.sub(r"^(?:www|m|go)\.", "", parsed.hostname or "")
    target = f"{host}{parsed.path}?{parsed.query}"
    for platform, kind, pattern in _SOURCE_PATTERNS:
        match = pattern.match(target)
        if match:
            clean_url = urllib.parse.urlunsplit(parsed._replace(fragment=""))
            return SourceRef(platform=platform, kind=kind, id=match["id"], url=clean_url)
    raise UnsupportedSourceError(_UNSUPPORTED_MESSAGE)
