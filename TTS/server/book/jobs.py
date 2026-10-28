"""Background job queue that renders books one at a time."""

import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from typing import Dict, Iterable, List, Optional

from TTS.server.book.parser import check_speakers, parse_book, summarize
from TTS.server.book.renderer import RenderCancelled, render_book

_PUBLIC_FIELDS = (
    "id",
    "name",
    "source",
    "status",
    "progress",
    "error",
    "outputs",
    "created",
    "started",
    "finished",
    "stats",
    "summary",
)


def safe_name(name: str) -> str:
    name = os.path.splitext(os.path.basename(name or ""))[0]
    name = re.sub(r"[^\w.-]+", "_", name).strip("._")
    return name or "book"


class JobQueue:
    """Renders books on a single worker thread; job state is persisted to `<data_dir>/jobs.json`.

    Args:
        synthesizer: loaded `Synthesizer`.
        lock: lock shared with the other server endpoints that use `synthesizer`.
        data_dir: where uploaded books, outputs and `jobs.json` are stored.
        formats: output formats, see `renderer.FORMATS`.
        speaker_ids: speaker ids the model supports, used to validate books before queueing.
    """

    def __init__(self, synthesizer, lock, data_dir: str, formats: Iterable[str], speaker_ids: Iterable[str]):
        self.synthesizer = synthesizer
        self.lock = lock
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        self.formats = list(formats)
        self.speaker_ids = set(speaker_ids)
        self.jobs: Dict[str, dict] = {}
        self._jobs_lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._cancel: Dict[str, threading.Event] = {}
        os.makedirs(os.path.join(self.data_dir, "jobs"), exist_ok=True)
        self._load()
        self._worker = threading.Thread(target=self._run, name="book-renderer", daemon=True)
        self._worker.start()

    # ---- public API -------------------------------------------------------------------------

    def validate(self, md: str):
        """Parse and check a book; raises `BookError`. Returns the parsed `Book`."""
        book = parse_book(md)
        if self.speaker_ids:
            check_speakers(book, self.speaker_ids)
        return book

    def submit(self, md: str, name: str, source: str = "upload", out_stem: Optional[str] = None) -> dict:
        """Validate and queue a book. Raises `BookError` if it is invalid.

        `out_stem` is the output path without extension; defaults to a file in the job directory.
        """
        book = self.validate(md)
        job_id = uuid.uuid4().hex[:12]
        job_dir = self._job_dir(job_id)
        os.makedirs(job_dir)
        with open(os.path.join(job_dir, "source.md"), "w", encoding="utf-8") as f:
            f.write(md)
        job = {
            "id": job_id,
            "name": name,
            "source": source,
            "status": "queued",
            "progress": 0.0,
            "error": None,
            "outputs": {},
            "created": time.time(),
            "started": None,
            "finished": None,
            "stats": None,
            "summary": summarize(book),
            "out_stem": out_stem or os.path.join(job_dir, safe_name(name)),
        }
        with self._jobs_lock:
            self.jobs[job_id] = job
        self._save()
        self._queue.put(job_id)
        return self._public(job)

    def get(self, job_id: str) -> Optional[dict]:
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            return self._public(job) if job else None

    def list_jobs(self) -> List[dict]:
        with self._jobs_lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)
            return [self._public(j) for j in jobs]

    def output_path(self, job_id: str, fmt: str) -> Optional[str]:
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            path = job and job["outputs"].get(fmt)
        return path if path and os.path.isfile(path) else None

    def delete(self, job_id: str) -> bool:
        """Cancel the job if it is running and delete it. Outputs written outside the data dir
        (watch folder) are left alone."""
        with self._jobs_lock:
            job = self.jobs.pop(job_id, None)
            if job is None:
                return False
            if job_id in self._cancel:
                self._cancel[job_id].set()
        shutil.rmtree(self._job_dir(job_id), ignore_errors=True)
        self._save()
        return True

    # ---- internals --------------------------------------------------------------------------

    def _job_dir(self, job_id: str) -> str:
        return os.path.join(self.data_dir, "jobs", job_id)

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: job[k] for k in _PUBLIC_FIELDS}

    def _update(self, job_id: str, save: bool = True, **fields) -> Optional[dict]:
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:  # deleted meanwhile
                return None
            job.update(fields)
        if save:
            self._save()
        return job

    def _save(self):
        with self._jobs_lock:
            data = json.dumps(list(self.jobs.values()), indent=1)
        path = os.path.join(self.data_dir, "jobs.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(path + ".tmp", path)

    def _load(self):
        path = os.path.join(self.data_dir, "jobs.json")
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        for job in sorted(jobs, key=lambda j: j["created"]):
            if not os.path.isdir(self._job_dir(job["id"])):
                continue
            # jobs interrupted by a restart start over
            if job["status"] in ("queued", "running"):
                job.update(status="queued", progress=0.0, started=None)
                self._queue.put(job["id"])
            self.jobs[job["id"]] = job

    def _run(self):
        while True:
            job_id = self._queue.get()
            try:
                self._render(job_id)
            except Exception:  # pylint: disable=broad-except
                traceback.print_exc()

    def _render(self, job_id: str):
        cancel = threading.Event()
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            self._cancel[job_id] = cancel
            out_stem = job["out_stem"]
        self._update(job_id, status="running", started=time.time())
        print(f" > Rendering book {job['name']} ({job_id})")
        try:
            with open(os.path.join(self._job_dir(job_id), "source.md"), "r", encoding="utf-8") as f:
                book = self.validate(f.read())
            result = render_book(
                book,
                self.synthesizer,
                out_stem,
                formats=self.formats,
                lock=self.lock,
                progress_cb=lambda done, total: self._update(job_id, save=False, progress=round(done / total, 4)),
                cancel_event=cancel,
            )
            stats = {k: result[k] for k in ("audio_seconds", "render_seconds", "rtf")}
            self._update(
                job_id, status="done", progress=1.0, outputs=result["outputs"], stats=stats, finished=time.time()
            )
            print(f" > Book {job['name']} done: {stats}")
            _remove(out_stem + ".error.txt")
        except RenderCancelled:
            print(f" > Book {job['name']} cancelled")
        except Exception as e:  # pylint: disable=broad-except
            traceback.print_exc()
            self._update(job_id, status="error", error=str(e), finished=time.time())
            if job["source"] == "watch":
                with open(out_stem + ".error.txt", "w", encoding="utf-8") as f:
                    f.write(f"{e}\n")
        finally:
            with self._jobs_lock:
                self._cancel.pop(job_id, None)


def _remove(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
