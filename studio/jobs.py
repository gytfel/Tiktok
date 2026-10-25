"""Очередь рендера: ролики собираются по одному в фоновом потоке."""

from __future__ import annotations

import queue
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from . import config
from .pipeline import RenderContext, RenderError, render


@dataclass
class Job:
    id: str
    project: dict
    status: str = "queued"  # queued | running | done | error | cancelled
    stage: str = "В очереди"
    progress: float = 0.0
    log: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    failed_stage: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def public(self, with_log: bool = True) -> dict:
        data = {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress * 100, 1),
            "result": self.result,
            "error": self.error,
            "failed_stage": self.failed_stage,
            "created": self.created,
            "finished": self.finished,
        }
        if with_log:
            data["log"] = self.log[-200:]
        return data


class JobManager:
    def __init__(self, keep_job_dirs: bool = False):
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        self._keep = keep_job_dirs
        self._cleanup_old_dirs()
        self._worker = threading.Thread(target=self._run, daemon=True, name="render-worker")
        self._worker.start()

    @staticmethod
    def _cleanup_old_dirs(max_age: float = 24 * 3600) -> None:
        """Удаляет временные папки неудачных рендеров старше суток."""
        if not config.JOBS_DIR.exists():
            return
        now = time.time()
        for d in config.JOBS_DIR.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > max_age:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

    def submit(self, project: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], project=project)
        with self._lock:
            self.jobs[job.id] = job
            # Старые задачи не копим в памяти бесконечно.
            done = [j for j in self.jobs.values() if j.status in ("done", "error", "cancelled")]
            for old in sorted(done, key=lambda j: j.created)[:-50]:
                self.jobs.pop(old.id, None)
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_event.set()
        if job.status == "queued":
            job.status = "cancelled"
            job.stage = "Отменено"
        return True

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job.cancel_event.is_set():
                continue
            self._process(job)

    def _process(self, job: Job) -> None:
        job.status = "running"
        job_dir = config.JOBS_DIR / job.id

        def on_log(msg: str) -> None:
            job.log.append(msg)

        def on_progress(stage: str, value: float) -> None:
            job.stage = stage
            job.progress = value

        ctx = RenderContext(job_dir=job_dir, on_log=on_log, on_progress=on_progress, cancel_event=job.cancel_event)
        try:
            job.result = render(job.project, ctx)
            job.status = "done"
            job.stage = "Готово"
            job.progress = 1.0
        except RenderError as exc:
            job.failed_stage = job.stage
            cancelled = job.cancel_event.is_set()
            job.status = "cancelled" if cancelled else "error"
            job.error = None if cancelled else (config.disk_full_message() if config.is_disk_full(exc) else str(exc))
            job.stage = "Отменено" if cancelled else "Ошибка"
            on_log(str(exc))
        except Exception as exc:  # неожиданные ошибки тоже показываем пользователю
            job.failed_stage = job.stage
            job.status = "error"
            job.error = config.disk_full_message() if config.is_disk_full(exc) else f"{type(exc).__name__}: {exc}"
            job.stage = "Ошибка"
            on_log(traceback.format_exc())
        finally:
            job.finished = time.time()
            if not self._keep and job.status == "done":
                shutil.rmtree(job_dir, ignore_errors=True)
