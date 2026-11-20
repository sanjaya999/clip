import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .models import CaptionSegment, Clip, Run, RunStatus, Upload, UploadStatus

INTERRUPTED = "Stopped when the server restarted. Run it again."
UPLOAD_INTERRUPTED = "Stopped when the server restarted. Upload it again."

_CLIP_COLUMNS = ("clip_id", "video_id", "start_time", "end_time", "file_name", "captions", "hook", "made_by_run", "created_at")
_RUN_COLUMNS = ("run_id", "pipeline", "input_clip_id", "steps", "output_clip_id", "created_at")
_UPLOAD_COLUMNS = (
    "upload_id", "clip_id", "platform", "title", "description", "tags", "privacy", "status", "detail", "remote_url",
    "automatic", "created_at", "finished_at",
)


class Library:
    """Exported clips, pipeline runs and uploads, kept in SQLite. Nothing here ever holds an API key or a token."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS clips ("
                "clip_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, start_time REAL NOT NULL, end_time REAL NOT NULL, "
                "file_name TEXT NOT NULL, captions TEXT NOT NULL, hook TEXT, made_by_run TEXT, created_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, pipeline TEXT NOT NULL, input_clip_id TEXT NOT NULL, steps TEXT NOT NULL, "
                "output_clip_id TEXT, created_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS uploads ("
                "upload_id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, platform TEXT NOT NULL, title TEXT NOT NULL, "
                "description TEXT NOT NULL, tags TEXT NOT NULL, privacy TEXT NOT NULL, status TEXT NOT NULL, "
                "detail TEXT, remote_url TEXT, automatic INTEGER NOT NULL, created_at REAL NOT NULL, finished_at REAL)"
            )
        self._fail_interrupted_runs()
        self._fail_interrupted_uploads()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _fail_interrupted_runs(self) -> None:
        # Only the worker of the process that queued a run can finish it, so a run still open now never will.
        for run in self.list_runs():
            if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                run.fail(INTERRUPTED)
                self.save_run(run)

    def add_clip(self, clip: Clip) -> None:
        row = clip.model_dump(mode="json", include=set(_CLIP_COLUMNS))
        row["captions"] = json.dumps(row["captions"])
        with closing(self._connect()) as db, db:
            db.execute(
                f"INSERT INTO clips ({', '.join(_CLIP_COLUMNS)}) VALUES ({', '.join(':' + c for c in _CLIP_COLUMNS)})",
                row,
            )

    def get_clip(self, clip_id: str) -> Clip | None:
        clips = self._clips("WHERE clip_id = ?", (clip_id,))
        return clips[0] if clips else None

    def set_captions(self, clip_id: str, captions: list[CaptionSegment]) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE clips SET captions = ? WHERE clip_id = ?",
                (json.dumps([c.model_dump(mode="json") for c in captions]), clip_id),
            )

    def delete_clip(self, clip_id: str) -> bool:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE runs SET output_clip_id = NULL WHERE output_clip_id = ?", (clip_id,))
            cursor = db.execute("DELETE FROM clips WHERE clip_id = ?", (clip_id,))
            return cursor.rowcount > 0

    def list_clips(self) -> list[Clip]:
        return self._clips("ORDER BY created_at DESC, rowid DESC")

    def _clips(self, where: str, params: tuple = ()) -> list[Clip]:
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT {', '.join(_CLIP_COLUMNS)} FROM clips {where}", params).fetchall()
        return [Clip.model_validate({**row, "captions": json.loads(row["captions"])}) for row in map(dict, rows)]

    def add_run(self, run: Run) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                f"INSERT INTO runs ({', '.join(_RUN_COLUMNS)}) VALUES ({', '.join(':' + c for c in _RUN_COLUMNS)})",
                _run_row(run),
            )

    def save_run(self, run: Run) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE runs SET steps = :steps, output_clip_id = :output_clip_id WHERE run_id = :run_id",
                _run_row(run),
            )

    def get_run(self, run_id: str) -> Run | None:
        runs = self._runs("WHERE run_id = ?", (run_id,))
        return runs[0] if runs else None

    def delete_run(self, run_id: str) -> bool:
        with closing(self._connect()) as db, db:
            cursor = db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            return cursor.rowcount > 0

    def clear_finished_runs(self) -> int:
        finished_ids = [r.run_id for r in self.list_runs() if r.status in (RunStatus.DONE, RunStatus.FAILED)]
        if not finished_ids:
            return 0
        with closing(self._connect()) as db, db:
            placeholders = ", ".join("?" for _ in finished_ids)
            cursor = db.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", finished_ids)
            return cursor.rowcount

    def list_runs(self) -> list[Run]:
        return self._runs("ORDER BY created_at DESC, rowid DESC")

    def _runs(self, where: str, params: tuple = ()) -> list[Run]:
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT {', '.join(_RUN_COLUMNS)} FROM runs {where}", params).fetchall()
        return [Run.model_validate({**row, "steps": json.loads(row["steps"])}) for row in map(dict, rows)]

    def _fail_interrupted_uploads(self) -> None:
        # Uploads run in the process that queued them, so one still open now never finishes.
        for upload in self.list_uploads():
            if upload.status in (UploadStatus.QUEUED, UploadStatus.UPLOADING):
                upload.status, upload.detail = UploadStatus.FAILED, UPLOAD_INTERRUPTED
                self.save_upload(upload)

    def add_upload(self, upload: Upload) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                f"INSERT INTO uploads ({', '.join(_UPLOAD_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in _UPLOAD_COLUMNS)})",
                _upload_row(upload),
            )

    def save_upload(self, upload: Upload) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE uploads SET status = :status, detail = :detail, remote_url = :remote_url, "
                "finished_at = :finished_at WHERE upload_id = :upload_id",
                _upload_row(upload),
            )

    def get_upload(self, upload_id: str) -> Upload | None:
        uploads = self._uploads("WHERE upload_id = ?", (upload_id,))
        return uploads[0] if uploads else None

    def list_uploads(self) -> list[Upload]:
        return self._uploads("ORDER BY created_at DESC, rowid DESC")

    def delete_upload(self, upload_id: str) -> bool:
        with closing(self._connect()) as db, db:
            return db.execute("DELETE FROM uploads WHERE upload_id = ?", (upload_id,)).rowcount > 0

    def clear_finished_uploads(self) -> int:
        with closing(self._connect()) as db, db:
            return db.execute(
                "DELETE FROM uploads WHERE status IN (?, ?)", (UploadStatus.DONE.value, UploadStatus.FAILED.value)
            ).rowcount

    def _uploads(self, where: str, params: tuple = ()) -> list[Upload]:
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT {', '.join(_UPLOAD_COLUMNS)} FROM uploads {where}", params).fetchall()
        return [
            Upload.model_validate({**row, "tags": json.loads(row["tags"]), "automatic": bool(row["automatic"])})
            for row in map(dict, rows)
        ]


def _upload_row(upload: Upload) -> dict:
    row = upload.model_dump(mode="json", include=set(_UPLOAD_COLUMNS))
    row["tags"] = json.dumps(row["tags"])
    row["automatic"] = int(row["automatic"])
    return row


def _run_row(run: Run) -> dict:
    row = run.model_dump(mode="json", include=set(_RUN_COLUMNS))
    row["steps"] = json.dumps(row["steps"])
    return row
