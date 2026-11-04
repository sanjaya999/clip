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


class PipelineStepInfo(BaseModel):
    id: str
    label: str


class PipelineInfo(BaseModel):
    id: str
    label: str
    description: str
    steps: list[PipelineStepInfo]
    needs_captions: bool
