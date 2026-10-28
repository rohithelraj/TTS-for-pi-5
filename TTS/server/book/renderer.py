"""Render a parsed `Book` to audio files (wav/mp3/m4b) with a loaded `Synthesizer`."""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from contextlib import nullcontext
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np

from TTS.server.book.parser import Book

FORMATS = ("mp3", "m4b", "wav")

_FFMPEG_CODECS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "4"],
    "m4b": ["-c:a", "aac", "-b:a", "64k", "-f", "mp4"],
}


class RenderCancelled(Exception):
    pass


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _synthesize(synthesizer, text: str, speaker_id: str, lock) -> np.ndarray:
    with lock or nullcontext():
        wav = synthesizer.tts(text, speaker_name=speaker_id, split_sentences=False)
    # `Synthesizer.tts` appends a fixed silence after every sentence; we control pauses ourselves
    return np.trim_zeros(np.asarray(wav, dtype=np.float32), "b")


def _split_sentences(synthesizer, text: str) -> List[str]:
    return [s.strip() for s in synthesizer.split_into_sentences(text) if s.strip()]


def _ffmetadata(book: Book, durations: List[float]) -> str:
    def esc(value: str) -> str:
        for ch in "\\=;#\n":
            value = value.replace(ch, "\\" + ch)
        return value

    lines = [";FFMETADATA1", f"title={esc(book.title)}", f"album={esc(book.title)}", "genre=Audiobook"]
    if book.author:
        lines += [f"artist={esc(book.author)}", f"album_artist={esc(book.author)}"]
    start = 0
    for chapter, duration in zip(book.chapters, durations):
        end = start + int(round(duration * 1000))
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={esc(chapter.title)}"]
        start = end
    return "\n".join(lines) + "\n"


def _concat_wavs(paths: List[str], out_path: str):
    with wave.open(out_path, "wb") as out:
        for i, path in enumerate(paths):
            with wave.open(path, "rb") as w:
                if i == 0:
                    out.setparams(w.getparams())
                out.writeframes(w.readframes(w.getnframes()))


def _encode(fmt: str, chapter_wavs: List[str], metadata_path: str, out_path: str, work_dir: str):
    list_path = os.path.join(work_dir, "chapters.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for path in chapter_wavs:
            escaped = path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_path]
    cmd += ["-i", metadata_path, "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    cmd += _FFMPEG_CODECS[fmt] + [out_path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to create {fmt}: {result.stderr.strip()}")


def render_book(
    book: Book,
    synthesizer,
    out_stem: str,
    formats: Iterable[str] = ("mp3", "m4b"),
    lock: Optional[threading.Lock] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict:
    """Synthesize every segment of `book` and write `<out_stem>.<fmt>` for each format.

    `lock` is taken per sentence so other requests (e.g. `/api/tts`) can interleave with a long render.

    Returns:
        dict with `outputs` (format -> path), `audio_seconds`, `render_seconds` and `rtf`.
    """
    formats = list(dict.fromkeys(formats))
    for fmt in formats:
        if fmt not in FORMATS:
            raise ValueError(f"Unknown format {fmt!r}, expected one of {FORMATS}.")
    if any(f in _FFMPEG_CODECS for f in formats) and not has_ffmpeg():
        raise RuntimeError("ffmpeg is required for mp3/m4b output but was not found on PATH.")

    sr = synthesizer.output_sample_rate
    sentence_gap = np.zeros(int(book.pauses["sentence"] * sr), dtype=np.float32)

    # split up front so progress is per sentence, which is what actually takes time
    plan = [
        [(seg, _split_sentences(synthesizer, seg.text) if seg.text else []) for seg in chapter.segments]
        for chapter in book.chapters
    ]
    total = sum(len(sentences) for chapter in plan for _, sentences in chapter) or 1
    done = 0
    start_time = time.time()

    out_dir = os.path.dirname(os.path.abspath(out_stem))
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".render-") as work_dir:
        chapter_wavs, durations = [], []
        for idx, chapter in enumerate(plan):
            pieces = []
            for seg, sentences in chapter:
                for i, sentence in enumerate(sentences):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RenderCancelled()
                    if i:
                        pieces.append(sentence_gap)
                    pieces.append(_synthesize(synthesizer, sentence, book.voices[seg.voice], lock))
                    done += 1
                    if progress_cb:
                        progress_cb(done, total)
                pieces.append(np.zeros(int(seg.pause_after * sr), dtype=np.float32))
            wav = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
            path = os.path.join(work_dir, f"chapter_{idx:04d}.wav")
            synthesizer.save_wav(wav, path)
            chapter_wavs.append(path)
            durations.append(len(wav) / sr)

        metadata_path = os.path.join(work_dir, "metadata.txt")
        with open(metadata_path, "w", encoding="utf-8") as f:
            f.write(_ffmetadata(book, durations))

        outputs = {}
        for fmt in formats:
            out_path = f"{out_stem}.{fmt}"
            tmp_path = os.path.join(work_dir, f"out.{fmt}")
            if fmt == "wav":
                _concat_wavs(chapter_wavs, tmp_path)
            else:
                _encode(fmt, chapter_wavs, metadata_path, tmp_path, work_dir)
            # move into place only when complete, so watchers never see half-written files
            os.replace(tmp_path, out_path)
            outputs[fmt] = out_path

    audio_seconds = sum(durations)
    render_seconds = time.time() - start_time
    return {
        "outputs": outputs,
        "audio_seconds": round(audio_seconds, 2),
        "render_seconds": round(render_seconds, 2),
        "rtf": round(render_seconds / audio_seconds, 3) if audio_seconds else None,
    }
