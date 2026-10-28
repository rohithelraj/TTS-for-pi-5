"""Polls a folder for Book Markdown files and queues them; audio is written next to each file."""

import os
import threading
import time
from typing import Dict, Tuple

from TTS.server.book.jobs import JobQueue
from TTS.server.book.parser import BookError


def output_stem(md_path: str) -> str:
    """`ch1.md` -> `ch1`, and `ch1.book.md` (book-formatter output) -> `ch1`."""
    stem = md_path[: -len(".md")]
    return stem[: -len(".book")] if stem.endswith(".book") else stem


def _has_front_matter(path: str) -> bool:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readline().lstrip("﻿").rstrip() == "---"


class FolderWatcher:
    """Queues every `*.md` under `watch_dir` that starts with front matter.

    Files without front matter (e.g. raw chapters that still need the book-formatter agent) are
    ignored. A file is queued once its size and mtime were unchanged for one poll, and only if it is
    newer than its existing outputs, so restarts don't re-render everything. Parse errors are
    written to `<name>.error.txt` next to the file.
    """

    def __init__(self, watch_dir: str, jobs: JobQueue, interval: float = 5.0):
        self.watch_dir = os.path.abspath(os.path.expanduser(watch_dir))
        self.jobs = jobs
        self.interval = interval
        self._seen: Dict[str, Tuple[float, int]] = {}  # path -> (mtime, size) at last poll
        self._handled: Dict[str, float] = {}  # path -> mtime that was queued or rejected
        os.makedirs(self.watch_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="book-watcher", daemon=True)
        self._thread.start()

    def _run(self):
        print(f" > Watching {self.watch_dir} for books")
        while True:
            try:
                self.poll()
            except Exception as e:  # pylint: disable=broad-except
                print(f" > Book watcher error: {e}")
            time.sleep(self.interval)

    def _outputs_up_to_date(self, path: str, mtime: float) -> bool:
        stem = output_stem(path)
        outputs = [f"{stem}.{fmt}" for fmt in self.jobs.formats]
        return all(os.path.isfile(p) and os.path.getmtime(p) >= mtime for p in outputs)

    def poll(self):
        for root, dirs, files in os.walk(self.watch_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith(".") or not name.endswith(".md"):
                    continue
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    continue
                state = (stat.st_mtime, stat.st_size)
                stable = self._seen.get(path) == state
                self._seen[path] = state
                if not stable or self._handled.get(path) == stat.st_mtime:
                    continue
                self._handled[path] = stat.st_mtime
                if not _has_front_matter(path) or self._outputs_up_to_date(path, stat.st_mtime):
                    continue
                self._submit(path)

    def _submit(self, path: str):
        stem = output_stem(path)
        with open(path, "r", encoding="utf-8") as f:
            md = f.read()
        try:
            job = self.jobs.submit(md, name=os.path.relpath(path, self.watch_dir), source="watch", out_stem=stem)
        except BookError as e:
            print(f" > {path}: {e}")
            with open(stem + ".error.txt", "w", encoding="utf-8") as f:
                f.write(f"{e}\n")
            return
        print(f" > Queued {path} as job {job['id']}")
