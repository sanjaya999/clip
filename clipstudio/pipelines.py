import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import clip_engine, hooks
from .highlights import SavedSettings
from .library import Library
from .models import AIChoice, Clip, PipelineInfo, PipelineStepInfo, Run, StepRecord

log = logging.getLogger(__name__)


@dataclass
class StepContext:
    clip: Clip
    clip_path: Path
    ai: AIChoice
    saved: SavedSettings
    output_path: Path
    hook: str | None


@dataclass(frozen=True)
class Step:
    id: str
    label: str
    run: Callable[[StepContext], str | None]


@dataclass(frozen=True)
class Pipeline:
    id: str
    label: str
    description: str
    steps: tuple[Step, ...]
    needs_captions: bool

    def info(self) -> PipelineInfo:
        return PipelineInfo(
            id=self.id,
            label=self.label,
            description=self.description,
            steps=[PipelineStepInfo(id=s.id, label=s.label) for s in self.steps],
            needs_captions=self.needs_captions,
        )

    def new_run(self, clip: Clip) -> Run:
        return Run(
            run_id=new_id(),
            pipeline=self.id,
            input_clip_id=clip.clip_id,
            steps=[StepRecord(id=s.id, label=s.label) for s in self.steps],
            created_at=time.time(),
        )


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def _write_hook(ctx: StepContext) -> str:
    _, ctx.hook = hooks.write_hook(
        ctx.clip.captions,
        ctx.clip.start_time,
        ctx.clip.end_time,
        ctx.ai.provider,
        api_key=ctx.ai.api_key,
        model=ctx.ai.model,
        base_url=ctx.ai.base_url,
        saved=ctx.saved,
    )
    return ctx.hook


def _draw_hook(ctx: StepContext) -> None:
    clip_engine.burn_hook(ctx.clip_path, ctx.hook, ctx.output_path)


PIPELINES: dict[str, Pipeline] = {
    p.id: p for p in (
        Pipeline(
            id="context-hook",
            label="Context hook",
            description="The AI writes one line from the clip's subtitles, drawn over its first 2 seconds.",
            steps=(
                Step("write-hook", "Write the hook", _write_hook),
                Step("draw-hook", "Draw it on the clip", _draw_hook),
            ),
            needs_captions=True,
        ),
    )
}


def execute(
    run: Run,
    ai: AIChoice,
    *,
    library: Library,
    clips_dir: Path,
    saved: SavedSettings,
    on_clip: Callable[[Clip], None] | None = None,
) -> None:
    """Run every step in order, saving the run after each change. The worker is the run's only writer.
    `on_clip` gets the clip a finished run added to the library."""
    pipeline = PIPELINES[run.pipeline]
    clip = library.get_clip(run.input_clip_id)
    ctx = StepContext(
        clip=clip,
        clip_path=clips_dir / clip.file_name,
        ai=ai,
        saved=saved,
        output_path=clips_dir / f".{run.run_id}.part.mp4",
        hook=clip.hook,
    )
    run.begin()
    library.save_run(run)
    for index, step in enumerate(pipeline.steps):
        try:
            detail = step.run(ctx)
            if index == len(pipeline.steps) - 1:
                run.output_clip_id = _publish(ctx, run, library, clips_dir)
        except Exception as exc:
            log.warning("Run %s failed at %s", run.run_id, step.id, exc_info=True)
            ctx.output_path.unlink(missing_ok=True)
            run.fail(str(exc) or type(exc).__name__)
            library.save_run(run)
            return
        run.advance(detail)
        library.save_run(run)
    if on_clip and run.output_clip_id:
        on_clip(library.get_clip(run.output_clip_id))


def _publish(ctx: StepContext, run: Run, library: Library, clips_dir: Path) -> str:
    """Rename the finished output into place and add it to the library as a clip of the same source range."""
    clip_id = new_id()
    stem = Path(ctx.clip.file_name).stem.removeprefix(f"{ctx.clip.clip_id}_")
    final = clips_dir / f"{clip_id}_{stem}_{run.pipeline}.mp4"
    ctx.output_path.replace(final)
    library.add_clip(ctx.clip.model_copy(update={
        "clip_id": clip_id,
        "file_name": final.name,
        "hook": ctx.hook,
        "made_by_run": run.run_id,
        "created_at": time.time(),
    }))
    return clip_id
