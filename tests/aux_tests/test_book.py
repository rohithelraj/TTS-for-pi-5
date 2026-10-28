import os
import re
import shutil
import threading
import unittest
import wave
from types import SimpleNamespace

import numpy as np

from tests import get_tests_input_path
from TTS.server.book.parser import BookError, check_speakers, parse_book, strip_inline, summarize
from TTS.server.book.renderer import has_ffmpeg, render_book
from TTS.server.book.watcher import output_stem

FRONT = """---
voices:
  narrator: n
  Alice: a
  Tom: t
---
"""


def segs(body, front=FRONT):
    book = parse_book(front + body)
    return [(s.voice, s.text, s.pause_after) for s in book.segments()]


class TestBookParser(unittest.TestCase):
    def test_sample_book(self):
        with open(os.path.join(get_tests_input_path(), "sample_book.md"), encoding="utf-8") as f:
            book = parse_book(f.read())
        self.assertEqual(book.title, "The Lighthouse")
        self.assertEqual(book.author, "Jane Doe")
        self.assertEqual(book.voices["Old Man"], "p227")
        self.assertEqual(book.pauses["paragraph"], 0.8)
        self.assertEqual([c.title for c in book.chapters], ["Chapter 1: The Storm", "Chapter 2: The Letter"])
        voices = [s.voice for s in book.chapters[0].segments]
        self.assertEqual(voices, ["narrator", "narrator", "Alice", "Tom", "narrator", "Old Man"])
        # multi-line dialogue is one utterance
        self.assertEqual(book.chapters[0].segments[-1].text, "No. That's the bell. Nobody has rung it in forty years.")
        self.assertEqual(summarize(book)["voices_unused"], [])

    def test_narration_and_paragraph_pauses(self):
        self.assertEqual(
            segs("First line\ncontinues.\n\nSecond paragraph.\n"),
            [("narrator", "First line continues.", 0.7), ("narrator", "Second paragraph.", 0.7)],
        )

    def test_dialogue(self):
        out = segs("Intro.\n\n> **Alice:** Hi.\n> **Tom**: Hello.\n\nAfter.\n")
        self.assertEqual(
            out,
            [
                ("narrator", "Intro.", 0.7),
                ("Alice", "Hi.", 0.4),
                ("Tom", "Hello.", 0.7),  # end of the dialogue block gets the paragraph pause
                ("narrator", "After.", 0.7),
            ],
        )

    def test_quote_without_speaker_uses_current_voice(self):
        self.assertEqual(segs("> just a quote\n"), [("narrator", "just a quote", 0.7)])

    def test_headings_start_chapters(self):
        book = parse_book(FRONT + "Prologue text.\n\n# One\n\nText.\n\n## Part\n\nMore.\n\n# Two\n\nEnd.\n")
        self.assertEqual([c.title for c in book.chapters], ["Untitled", "One", "Two"])
        one = book.chapters[1].segments
        self.assertEqual(
            [(s.voice, s.text) for s in one],
            [("narrator", "One"), ("narrator", "Text."), ("narrator", "Part"), ("narrator", "More.")],
        )
        # paragraph before a heading is followed by the longer heading pause
        self.assertEqual(book.chapters[0].segments[-1].pause_after, 1.5)
        self.assertEqual(one[1].pause_after, 1.5)

    def test_first_heading_names_first_chapter(self):
        book = parse_book(FRONT + "# Chapter 1\n\nText.\n")
        self.assertEqual([c.title for c in book.chapters], ["Chapter 1"])

    def test_scene_break(self):
        self.assertEqual(
            segs("A.\n\n---\n\nB.\n\n* * *\n\nC.\n")[0:2], [("narrator", "A.", 2.0), ("narrator", "B.", 2.0)]
        )

    def test_pauses(self):
        out = segs("A. [pause 2s] B.\n\n[pause 500ms]\n\nC.\n")
        self.assertEqual(out, [("narrator", "A.", 2.0), ("narrator", "B.", 1.2), ("narrator", "C.", 0.7)])

    def test_leading_pause_is_silence_segment(self):
        self.assertEqual(segs("[pause 1s]\n\nA.\n")[0], (None, "", 1.0))

    def test_voice_block(self):
        out = segs("[voice: Alice]\nDear Tom,\n\nlove.\n> **Tom:** reply\n[/voice]\nAfter.\n")
        self.assertEqual([v for v, _, _ in out], ["Alice", "Alice", "Tom", "narrator"])
        out = segs("[voice: Tom] inline block [/voice]\n")
        self.assertEqual(out, [("Tom", "inline block", 0.7)])

    def test_skipped_content(self):
        body = (
            "Text.\n\n```\ncode\n```\n\n| a | b |\n|---|---|\n\n![img](x.png)\n\n<!-- hidden\nnote -->\n\n[^1]: note\n"
        )
        self.assertEqual(segs(body), [("narrator", "Text.", 0.7)])

    def test_list_items(self):
        self.assertEqual(
            segs("Items:\n- one\n- two\n"),
            [("narrator", "Items:", 0.3), ("narrator", "one", 0.3), ("narrator", "two", 0.7)],
        )

    def test_strip_inline(self):
        self.assertEqual(
            strip_inline("**Bold** and *it* and _it_ [link](http://x) `c` snake_case a*b ~~s~~ \\*x\\* note[^1]"),
            "Bold and it and it link c snake_case a*b s *x* note",
        )

    def test_errors(self):
        with self.assertRaisesRegex(BookError, "front matter"):
            parse_book("no front matter")
        with self.assertRaisesRegex(BookError, "narrator"):
            parse_book("---\nvoices:\n  Alice: a\n---\nText")
        with self.assertRaisesRegex(BookError, r"`Bob` \(line 3\)"):
            parse_book(FRONT + "Hi.\n\n> **Bob:** hey\n")
        with self.assertRaisesRegex(BookError, "Unknown pause"):
            parse_book("---\nvoices: {narrator: n}\npauses: {long: 3}\n---\nText")
        with self.assertRaisesRegex(BookError, "no speakable text"):
            parse_book(FRONT + "---\n")

    def test_check_speakers(self):
        book = parse_book(FRONT + "Hi.\n")
        check_speakers(book, ["n", "a", "t"])
        with self.assertRaisesRegex(BookError, "Tom → t"):
            check_speakers(book, ["n", "a"])

    def test_watch_output_stem(self):
        self.assertEqual(output_stem("/b/ch1.md"), "/b/ch1")
        self.assertEqual(output_stem("/b/ch1.book.md"), "/b/ch1")


class FakeSynthesizer:
    """Returns one second of signal plus trailing zeros, like `Synthesizer.tts` does."""

    output_sample_rate = 100

    def __init__(self):
        self.calls = []

    def split_into_sentences(self, text):
        return re.findall(r"[^.]+\.?", text)

    def tts(self, text, speaker_name="", split_sentences=True):
        self.calls.append((text, speaker_name, split_sentences))
        return [0.5] * 100 + [0] * 30

    def save_wav(self, wav, path):
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.output_sample_rate)
            w.writeframes((np.asarray(wav) * 32767).astype(np.int16).tobytes())


class TestBookRenderer(unittest.TestCase):
    MD = FRONT + "# One\n\nA. B.\n\n> **Alice:** Hi.\n\n# Two\n\nEnd.\n"

    def render(self, tmp, formats):
        synth = FakeSynthesizer()
        progress = []
        result = render_book(
            parse_book(self.MD),
            synth,
            os.path.join(tmp, "out"),
            formats=formats,
            lock=threading.Lock(),
            progress_cb=lambda d, t: progress.append((d, t)),
        )
        return synth, progress, result

    def test_wav_timing(self):
        import tempfile  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            synth, progress, result = self.render(tmp, ["wav"])
            self.assertEqual(
                synth.calls,
                [
                    ("One", "n", False),
                    ("A.", "n", False),
                    ("B.", "n", False),
                    ("Hi.", "a", False),
                    ("Two", "n", False),
                    ("End.", "n", False),
                ],
            )
            self.assertEqual(progress[-1], (6, 6))
            # chapter 1: One(1s)+1.5 | A(1)+0.3+B(1)+0.7 | Hi(1)+1.5 = 8.0s, trailing zeros trimmed
            # chapter 2: Two(1)+1.5 | End(1)+0.7 = 4.2s
            self.assertAlmostEqual(result["audio_seconds"], 12.2)
            with wave.open(result["outputs"]["wav"]) as w:
                self.assertEqual(w.getnframes(), 1220)
            self.assertEqual(os.listdir(tmp), ["out.wav"])  # work dir cleaned up

    @unittest.skipUnless(has_ffmpeg() and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
    def test_m4b_chapters(self):
        import json  # pylint: disable=import-outside-toplevel
        import subprocess  # pylint: disable=import-outside-toplevel
        import tempfile  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            _, _, result = self.render(tmp, ["mp3", "m4b"])
            for fmt in ("mp3", "m4b"):
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", result["outputs"][fmt]],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                chapters = json.loads(probe.stdout)["chapters"]
                self.assertEqual([c["tags"]["title"] for c in chapters], ["One", "Two"], fmt)
                self.assertAlmostEqual(float(chapters[1]["start_time"]), 8.0, places=1)


if __name__ == "__main__":
    unittest.main()
