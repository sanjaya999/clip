import time
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

MEDIA_KEY_PATTERN = r"^(youtube|twitch|kick)_[A-Za-z0-9_-]+$"


class Platform(StrEnum):
    YOUTUBE = "youtube"
    TWITCH = "twitch"
    KICK = "kick"


class MediaMode(StrEnum):
    """How the editor reads a video: a downloaded file, or the platform's stream with nothing saved but clips."""
    DOWNLOAD = "download"
    STREAM = "stream"


class AIProvider(StrEnum):
    AUTO = "auto"
    CLAUDE = "claude"
    GEMINI = "gemini"
    OPENAI = "openai"
    LOCAL = "local"


class VideoInfo(BaseModel):
    video_id: str
    platform: Platform
    title: str
    duration: float = Field(ge=0.0)
    thumbnail: str
    stream_url: str | None = None
    local_path: str | None = None


class CaptionSegment(BaseModel):
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str

    @model_validator(mode="after")
    def validate_endpoints(self) -> "CaptionSegment":
        if self.end < self.start:
            raise ValueError("end time cannot be earlier than start time")
        return self


class HighlightClip(BaseModel):
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    title: str
    reason: str
    score: float = Field(ge=0.0, le=1.0, default=1.0)

    @model_validator(mode="after")
    def validate_endpoints(self) -> "HighlightClip":
        if self.end <= self.start:
            raise ValueError("end time must be strictly greater than start time")
        return self


class LineSpan(BaseModel):
    """A highlight as an AI model reports it: inclusive indices into the numbered transcript."""
    start_line: int
    end_line: int
    title: str
    reason: str
    score: float


class HighlightPlan(BaseModel):
    highlights: list[LineSpan]


class ProviderSettings(BaseModel):
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


class ProviderInfo(BaseModel):
    id: AIProvider
    label: str
    has_server_key: bool
    needs_key: bool
    accepts_key: bool = False
    saved_key_hint: str | None = None
    has_saved_settings: bool = False
    default_model: str | None = None
    uses_base_url: bool = False
    default_base_url: str | None = None


class AIChoice(BaseModel):
    """Which AI a pipeline step uses. It lives only in memory for the worker, never in the library."""
    provider: AIProvider = AIProvider.AUTO
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None

    @field_validator("api_key", "model", "base_url")
    @classmethod
    def blank_is_unset(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class ClipRequest(BaseModel):
    video_id: str = Field(pattern=MEDIA_KEY_PATTERN)
    start_time: float = Field(ge=0.0)
    end_time: float = Field(ge=0.0)
    filename: str | None = Field(default=None, pattern=r"^[\w.-]+$")
    captions: list[CaptionSegment] = []
    mode: MediaMode = MediaMode.DOWNLOAD

    @model_validator(mode="after")
    def validate_bounds(self) -> "ClipRequest":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be strictly greater than start_time")
        return self


class Clip(BaseModel):
    """A clip file in the library. Times and captions are in source-video seconds, even for a clip a run made."""
    clip_id: str
    video_id: str
    start_time: float
    end_time: float
    file_name: str
    captions: list[CaptionSegment]
    hook: str | None = None
    made_by_run: str | None = None
    created_at: float

    @computed_field
    @property
    def duration(self) -> float:
        return round(self.end_time - self.start_time, 2)

    @computed_field
    @property
    def download_url(self) -> str:
        return f"/downloads/{self.file_name}"

    @computed_field
    @property
    def preview_url(self) -> str:
        return f"/clips/{self.file_name}"


class StepState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class StepRecord(BaseModel):
    id: str
    label: str
    state: StepState = StepState.QUEUED
    started_at: float | None = None
    finished_at: float | None = None
    detail: str | None = None


class Run(BaseModel):
    run_id: str
    pipeline: str
    input_clip_id: str
    steps: list[StepRecord]
    output_clip_id: str | None = None
    created_at: float

    @computed_field
    @property
    def status(self) -> RunStatus:
        states = {step.state for step in self.steps}
        if StepState.FAILED in states:
            return RunStatus.FAILED
        if states == {StepState.DONE}:
            return RunStatus.DONE
        if StepState.RUNNING in states:
            return RunStatus.RUNNING
        return RunStatus.QUEUED

    def _current(self) -> int:
        return next(i for i, s in enumerate(self.steps) if s.state in (StepState.QUEUED, StepState.RUNNING))

    def begin(self) -> None:
        step = self.steps[self._current()]
        step.state, step.started_at = StepState.RUNNING, time.time()

    def advance(self, detail: str | None) -> None:
        """Finish the running step and start the next in one change, so a saved run is never between steps."""
        index = self._current()
        step = self.steps[index]
        step.state, step.detail, step.finished_at = StepState.DONE, detail, time.time()
        if index + 1 < len(self.steps):
            self.begin()

    def fail(self, detail: str) -> None:
        """Fail the first unfinished step and skip the ones after it."""
        index = self._current()
        step = self.steps[index]
        step.state, step.detail, step.finished_at = StepState.FAILED, detail, time.time()
        for later in self.steps[index + 1:]:
            later.state = StepState.SKIPPED

    def reset(self) -> None:
        """Reset all steps back to queued state for a retry."""
        for step in self.steps:
            step.state = StepState.QUEUED
            step.started_at = None
            step.finished_at = None
            step.detail = None
        self.output_clip_id = None


HEX_COLOR = r"^#[0-9a-fA-F]{6}$"


class CaptionPosition(StrEnum):
    BOTTOM = "bottom"
    MIDDLE = "middle"
    TOP = "top"


class Aspect(StrEnum):
    SOURCE = "source"
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"
    SQUARE = "1:1"
    FEED = "4:5"


class Fit(StrEnum):
    """How the clip fills a frame of another shape: cropped to fill it, or shrunk to fit with a colour around it."""
    CROP = "crop"
    FIT = "fit"


class CaptionStyle(BaseModel):
    """How burned-in captions look. Sizes are relative to the output frame, so the preview and the export agree."""
    font: str = "Arial"
    size: float = Field(default=6.5, ge=2.0, le=15.0, description="Font size as a percent of the frame height")
    color: str = Field(default="#ffffff", pattern=HEX_COLOR)
    outline_color: str = Field(default="#000000", pattern=HEX_COLOR)
    outline: float = Field(default=12.0, ge=0.0, le=30.0, description="Outline width as a percent of the font size")
    box: bool = False
    box_color: str = Field(default="#000000", pattern=HEX_COLOR)
    box_opacity: float = Field(default=0.6, ge=0.0, le=1.0)
    position: CaptionPosition = CaptionPosition.BOTTOM
    margin: float = Field(default=10.0, ge=0.0, le=45.0, description="Distance from the edge as a percent of the height")
    bold: bool = True
    italic: bool = False
    uppercase: bool = False


class EditSpec(BaseModel):
    """Everything the Studio page changes about a clip. Caption times are clip seconds, before the trim."""
    trim_start: float = Field(default=0.0, ge=0.0)
    trim_end: float | None = Field(default=None, gt=0.0)
    aspect: Aspect = Aspect.SOURCE
    fit: Fit = Fit.CROP
    focus_x: float = Field(default=50.0, ge=0.0, le=100.0)
    focus_y: float = Field(default=50.0, ge=0.0, le=100.0)
    background: str = Field(default="#000000", pattern=HEX_COLOR)
    border: float = Field(default=0.0, ge=0.0, le=10.0, description="Frame width as a percent of the shorter side")
    border_color: str = Field(default="#f2c32b", pattern=HEX_COLOR)
    brightness: float = Field(default=0.0, ge=-0.5, le=0.5)
    contrast: float = Field(default=1.0, ge=0.5, le=2.0)
    saturation: float = Field(default=1.0, ge=0.0, le=3.0)
    volume: float = Field(default=1.0, ge=0.0, le=3.0)
    fade: bool = False
    title: str = Field(default="", max_length=120)
    title_style: CaptionStyle = CaptionStyle(position=CaptionPosition.TOP, margin=8.0, size=5.5, box=True)
    captions: list[CaptionSegment] = []
    caption_style: CaptionStyle = CaptionStyle()

    @model_validator(mode="after")
    def validate_trim(self) -> "EditSpec":
        if self.trim_end is not None and self.trim_end <= self.trim_start:
            raise ValueError("trim_end must be later than trim_start")
        return self


class UploadPrivacy(StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class UploadStatus(StrEnum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"


class UploadRequest(BaseModel):
    clip_id: str
    platform: str
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=4000)
    tags: list[str] = []
    privacy: UploadPrivacy = UploadPrivacy.PRIVATE


class Upload(BaseModel):
    """One clip sent to one platform. Credentials never appear here."""
    upload_id: str
    clip_id: str
    platform: str
    title: str
    description: str = ""
    tags: list[str] = []
    privacy: UploadPrivacy = UploadPrivacy.PRIVATE
    status: UploadStatus = UploadStatus.QUEUED
    detail: str | None = None
    remote_url: str | None = None
    automatic: bool = False
    created_at: float
    finished_at: float | None = None


class AccountField(BaseModel):
    name: str
    label: str
    secret: bool
    help: str = ""


class AccountInfo(BaseModel):
    """A platform as the Publish page sees it. Secret fields are reported only as hints."""
    id: str
    label: str
    help: str
    fields: list[AccountField]
    # Non-secret fields as saved, and secret fields as a hint like "…a1b2", or None when unset.
    values: dict[str, str | None]
    connected: bool
    # Whether the platform signs in through OAuth, and whether the details it needs for that are saved.
    uses_oauth: bool
    can_connect: bool
    auto_upload: bool
    default_privacy: UploadPrivacy
    default_description: str
    privacy_note: str


class AccountSettings(BaseModel):
    """What the Publish page saves for a platform. A field left out or blank keeps its saved value."""
    credentials: dict[str, str] = {}
    auto_upload: bool | None = None
    default_privacy: UploadPrivacy | None = None
    default_description: str | None = Field(default=None, max_length=4000)


class PipelineStepInfo(BaseModel):
    id: str
    label: str


class PipelineInfo(BaseModel):
    id: str
    label: str
    description: str
    steps: list[PipelineStepInfo]
    needs_captions: bool
