"""Parser for "Book Markdown", see `TTS/server/BOOK_FORMAT.md`.

Turns a markdown file into chapters of `(voice, text, pause_after)` segments that the renderer
synthesizes one by one.
"""

import re
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import yaml

NARRATOR = "narrator"

DEFAULT_PAUSES = {
    "sentence": 0.3,
    "paragraph": 0.7,
    "dialogue": 0.4,
    "heading": 1.5,
    "scene": 2.0,
}

_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_VOICE_OPEN_RE = re.compile(r"\[voice:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_VOICE_CLOSE_RE = re.compile(r"\[/voice\]", re.IGNORECASE)
_PAUSE_RE = re.compile(r"\[pause\s+(\d+(?:\.\d+)?)\s*(ms|s)?\s*\]", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SCENE_BREAK_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
# `**Name:** text` or `**Name**: text`
_SPEAKER_RE = re.compile(r"^\*\*([^*:]+?)(?::\*\*|\*\*:)\s*(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^[^\]]+\]:")
_TABLE_RE = re.compile(r"^\s*\|")

_INLINE_SUBS = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),  # images
    (re.compile(r"\[\^[^\]]+\]"), ""),  # footnote refs
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # links
    (re.compile(r"`([^`]*)`"), r"\1"),  # inline code
    (re.compile(r"<[^>]+>"), ""),  # html tags
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),  # bold
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),  # *italic*
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"), r"\1"),  # _italic_
    (re.compile(r"~~(.+?)~~"), r"\1"),  # strikethrough
]
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>])")
_ESCAPED_RE = re.compile(r"\x00(\d+)\x00")


class BookError(ValueError):
    """Raised when a Book Markdown file is invalid."""


@dataclass
class Segment:
    voice: Optional[str]  # character name from `voices:`; None for pure silence
    text: str
    pause_after: float


@dataclass
class Chapter:
    title: str
    segments: List[Segment] = field(default_factory=list)


@dataclass
class Book:
    title: str
    author: str
    voices: Dict[str, str]  # character name -> model speaker id
    pauses: Dict[str, float]
    chapters: List[Chapter]

    def segments(self) -> Iterable[Segment]:
        for chapter in self.chapters:
            yield from chapter.segments

    def speaker_ids(self) -> List[str]:
        return sorted(set(self.voices.values()))


def strip_inline(text: str) -> str:
    """Remove inline markdown so only speakable text is left."""
    # hide escaped characters (`\*`) from the emphasis patterns, restore them at the end
    text = _ESCAPE_RE.sub(lambda m: f"\x00{ord(m.group(1))}\x00", text)
    for pattern, repl in _INLINE_SUBS:
        text = pattern.sub(repl, text)
    text = _ESCAPED_RE.sub(lambda m: chr(int(m.group(1))), text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_front_matter(md: str):
    match = _FRONT_MATTER_RE.match(md)
    if not match:
        raise BookError("Missing front matter: the file must start with a `---` block that defines `voices:`.")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        raise BookError(f"Invalid YAML in front matter: {e}") from e
    if not isinstance(meta, dict):
        raise BookError("Front matter must be a YAML mapping.")
    return meta, md[match.end() :]


def _parse_voices(meta) -> Dict[str, str]:
    voices = meta.get("voices")
    if not isinstance(voices, dict) or not voices:
        raise BookError("Front matter must define `voices:` as a mapping of character name to speaker id.")
    voices = {str(k).strip(): str(v).strip() for k, v in voices.items()}
    if NARRATOR not in voices:
        raise BookError("`voices:` must include a `narrator` entry.")
    return voices


def _parse_pauses(meta) -> Dict[str, float]:
    pauses = dict(DEFAULT_PAUSES)
    custom = meta.get("pauses") or {}
    if not isinstance(custom, dict):
        raise BookError("`pauses:` must be a mapping, e.g. `paragraph: 0.7`.")
    for key, value in custom.items():
        if key not in DEFAULT_PAUSES:
            raise BookError(f"Unknown pause `{key}`. Valid keys: {', '.join(DEFAULT_PAUSES)}.")
        try:
            pauses[key] = float(value)
        except (TypeError, ValueError) as e:
            raise BookError(f"Pause `{key}` must be a number of seconds, got {value!r}.") from e
    return pauses


class _Builder:
    def __init__(self, title: str, voices: Dict[str, str], pauses: Dict[str, float]):
        self.voices = voices
        self.pauses = pauses
        self.chapters = [Chapter(title=title)]
        self.unknown = {}  # name -> first line number
        self.block_voice = NARRATOR
        self.buffer: List[str] = []
        self.buffer_voice = NARRATOR
        self.buffer_line = 0
        self.quote_open = False  # last emitted segment came from a blockquote

    @property
    def segments(self) -> List[Segment]:
        return self.chapters[-1].segments

    def _last_segment(self) -> Optional[Segment]:
        for chapter in reversed(self.chapters):
            if chapter.segments:
                return chapter.segments[-1]
        return None

    def check_voice(self, name: str, line_no: int) -> str:
        if name not in self.voices:
            self.unknown.setdefault(name, line_no)
        return name

    def emit(self, voice: str, text: str, pause: float):
        text = strip_inline(text)
        if text:
            self.segments.append(Segment(voice=voice, text=text, pause_after=pause))

    def add_silence(self, seconds: float):
        last = self._last_segment()
        if last is not None:
            last.pause_after += seconds
        else:
            self.segments.append(Segment(voice=None, text="", pause_after=seconds))

    def extend_pause(self, seconds: float):
        """Make sure the last segment is followed by at least `seconds` of silence."""
        last = self._last_segment()
        if last is not None:
            last.pause_after = max(last.pause_after, seconds)

    def add_text(self, text: str, line_no: int):
        self.end_quote()
        if not self.buffer:
            self.buffer_voice = self.block_voice
            self.buffer_line = line_no
        self.buffer.append(text)

    def flush(self, pause: Optional[float] = None):
        if self.buffer:
            self.emit(self.buffer_voice, " ".join(self.buffer), self.pauses["paragraph"] if pause is None else pause)
            self.buffer = []

    def end_quote(self):
        if self.quote_open:
            self.extend_pause(self.pauses["paragraph"])
            self.quote_open = False

    def boundary(self):
        self.flush()
        self.end_quote()


def parse_book(md: str) -> Book:
    """Parse Book Markdown into a `Book`. Raises `BookError` with a readable message on invalid input."""
    md = md.replace("\r\n", "\n").lstrip("﻿")
    meta, body = _parse_front_matter(md)
    voices = _parse_voices(meta)
    pauses = _parse_pauses(meta)
    title = str(meta.get("title") or "Untitled")
    author = str(meta.get("author") or "")

    # comments may span lines; keep line numbers stable by preserving newlines
    body = _COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), body)
    # put voice tags on their own lines so `[voice: X] text [/voice]` works inline too
    body = _VOICE_OPEN_RE.sub(lambda m: f"\n[voice: {m.group(1)}]\n", body)
    body = _VOICE_CLOSE_RE.sub("\n[/voice]\n", body)

    b = _Builder(title, voices, pauses)
    in_fence = False
    quote_voice: Optional[str] = None  # speaker of the current blockquote utterance
    quote_lines: List[str] = []

    def flush_quote():
        nonlocal quote_voice, quote_lines
        if quote_lines:
            b.emit(quote_voice or b.block_voice, " ".join(quote_lines), pauses["dialogue"])
            b.quote_open = True
        quote_voice, quote_lines = None, []

    for line_no, line in enumerate(body.split("\n"), start=1):
        if _FENCE_RE.match(line):
            if not in_fence:
                flush_quote()
                b.boundary()
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            b.flush()
            content = quote.group(1).strip()
            speaker = _SPEAKER_RE.match(content)
            if speaker:
                flush_quote()
                quote_voice = b.check_voice(speaker.group(1).strip(), line_no)
                content = speaker.group(2)
            elif not content:
                flush_quote()
                continue
            quote_lines.append(content)
            continue
        flush_quote()

        stripped = line.strip()
        if not stripped:
            b.flush()
            continue

        voice_open = _VOICE_OPEN_RE.fullmatch(stripped)
        if voice_open:
            b.boundary()
            b.block_voice = b.check_voice(voice_open.group(1), line_no)
            continue
        if _VOICE_CLOSE_RE.fullmatch(stripped):
            b.boundary()
            b.block_voice = NARRATOR
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            b.boundary()
            text = strip_inline(heading.group(2))
            if len(heading.group(1)) == 1:
                if b.segments or b.chapters[-1].title != title:
                    b.chapters.append(Chapter(title=text))
                else:
                    b.chapters[-1].title = text
            b.extend_pause(pauses["heading"])
            b.emit(NARRATOR, text, pauses["heading"])
            continue

        if _SCENE_BREAK_RE.match(stripped):
            b.boundary()
            b.extend_pause(pauses["scene"])
            continue

        if _TABLE_RE.match(stripped) or _FOOTNOTE_DEF_RE.match(stripped):
            b.boundary()
            continue

        list_item = _LIST_ITEM_RE.match(line)
        if list_item:
            b.flush(pauses["sentence"])
            stripped = list_item.group(1)

        # explicit pauses, possibly inline: `text [pause 2s] more text`
        parts = _PAUSE_RE.split(stripped)
        if parts[0].strip():
            b.add_text(parts[0], line_no)
        for i in range(1, len(parts), 3):
            seconds = float(parts[i]) / (1000 if (parts[i + 1] or "s").lower() == "ms" else 1)
            b.flush(pause=0.0)
            b.end_quote()
            b.add_silence(seconds)
            if parts[i + 2].strip():
                b.add_text(parts[i + 2], line_no)

    flush_quote()
    b.boundary()

    if b.unknown:
        names = ", ".join(f"`{n}` (line {ln})" for n, ln in b.unknown.items())
        raise BookError(f"Voices used but not defined in front matter `voices:`: {names}.")

    chapters = [c for c in b.chapters if c.segments]
    if not any(s.text for c in chapters for s in c.segments):
        raise BookError("The book has no speakable text.")
    return Book(title=title, author=author, voices=voices, pauses=pauses, chapters=chapters)


def check_speakers(book: Book, available: Iterable[str]):
    """Raise `BookError` if the book maps characters to speaker ids the model doesn't have."""
    available = set(available)
    bad = {name: sid for name, sid in book.voices.items() if sid not in available}
    if bad:
        listed = ", ".join(f"{name} → {sid}" for name, sid in bad.items())
        raise BookError(f"Unknown speaker ids: {listed}. See /book/voices for the available voices.")


def summarize(book: Book) -> dict:
    """JSON-friendly overview used by `/api/book/validate` and the CLI."""
    used = sorted({s.voice for s in book.segments() if s.voice})
    words = sum(len(s.text.split()) for s in book.segments())
    return {
        "title": book.title,
        "author": book.author,
        "voices": book.voices,
        "voices_used": used,
        "voices_unused": sorted(set(book.voices) - set(used)),
        "words": words,
        # ~150 spoken words per minute
        "estimated_minutes": round(words / 150, 1),
        "chapters": [
            {"title": c.title, "segments": len(c.segments), "words": sum(len(s.text.split()) for s in c.segments)}
            for c in book.chapters
        ],
    }


def main(argv=None):
    """Validate Book Markdown files: `python -m TTS.server.book.parser file.md [--segments]`."""
    import argparse  # pylint: disable=import-outside-toplevel
    import json  # pylint: disable=import-outside-toplevel

    parser = argparse.ArgumentParser(description="Validate a Book Markdown file.")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--segments", action="store_true", help="print every parsed segment")
    args = parser.parse_args(argv)

    ok = True
    for path in args.files:
        with open(path, "r", encoding="utf-8") as f:
            md = f.read()
        try:
            book = parse_book(md)
        except BookError as e:
            ok = False
            print(f"{path}: ERROR: {e}")
            continue
        print(f"{path}: OK")
        print(json.dumps(summarize(book), indent=2, ensure_ascii=False))
        if args.segments:
            for chapter in book.chapters:
                print(f"\n## {chapter.title}")
                for s in chapter.segments:
                    print(f"  [{s.voice or '-'}] {s.text!r} (+{s.pause_after:g}s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
