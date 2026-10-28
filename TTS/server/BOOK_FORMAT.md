# Book Markdown

Markdown with a small set of conventions that the TTS server (`/book`, `/api/book`, watch folder)
turns into a narrated, multi-voice audiobook. A Book Markdown file still reads normally in any
markdown viewer.

Check a file without rendering it:

```bash
python -m TTS.server.book.parser my_book.book.md --segments
```

## Example

```markdown
---
title: The Lighthouse
author: Jane Doe
voices:
  narrator: p230
  Alice: p225
  Tom: p226
  Old Man: p254
pauses:
  paragraph: 0.8
---

# Chapter 1: The Storm

The wind howled against the shutters. Rain hammered the glass.

> **Alice:** Do you hear that?
> **Tom:** It's only the sea.

The old man looked up from the fire.

> **Old Man:** No. That's the bell.

---

[pause 2s]

[voice: Alice]
Dear Tom, by the time you read this, I will be gone.
[/voice]
```

## Front matter (required)

The file must start with a YAML block between `---` lines.

| Key | Required | Meaning |
|---|---|---|
| `voices` | yes | Character name → model speaker id. Must include `narrator`. |
| `title` | no | Book title. Used for audio tags and the first chapter name. |
| `author` | no | Used for the artist/album artist tags. |
| `pauses` | no | Pause lengths in seconds. Unset keys use the defaults below. |

Pause keys and defaults:

| Key | Default | Where it applies |
|---|---|---|
| `sentence` | 0.3 | between sentences inside a paragraph or dialogue line |
| `paragraph` | 0.7 | after a narrated paragraph, and after a block of dialogue |
| `dialogue` | 0.4 | between consecutive dialogue lines |
| `heading` | 1.5 | before and after a heading |
| `scene` | 2.0 | at a `---` scene break |

Every name used in the body must be defined in `voices`. Unknown names are an error, never a
silent fallback. Speaker ids are checked against the loaded model. `/book/voices` on the server
lists them and lets you preview each one.

## Body

| Markdown | Spoken as |
|---|---|
| plain paragraph | narrator |
| `# Heading` | narrator, with `heading` pauses. Each `#` heading starts a new chapter (M4B/MP3 chapter marker). |
| `## Heading` … `######` | narrator, with `heading` pauses, no new chapter |
| `> **Name:** text` | `text` in Name's voice. Each `> **Name:**` starts a new line of dialogue. Following `>` lines without a name continue it. |
| `> text` (no name) | a quotation in the current voice (narrator unless inside `[voice:]`) |
| `---`, `***`, `* * *` | scene break: `scene` seconds of silence, nothing spoken |
| `[pause 2s]`, `[pause 500ms]` | that much extra silence. Allowed on its own line or inside a sentence. |
| `[voice: Name]` … `[/voice]` | everything between the tags is read in Name's voice (letters, diary entries, songs) |
| `- item` / `1. item` | each item is read like a short sentence |
| `**bold**`, `*italic*`, `[link](url)`, `` `code` `` | plain text (the model can't do emphasis) |
| code blocks, tables, images, HTML comments, footnotes | skipped |

Tips for a natural listen:
- Keep each dialogue line to what the character actually says. Put actions ("she said, turning
  away") in a narrator paragraph, or drop them if the line is clear without them.
- Write numbers, abbreviations and symbols the way they should be read ("Dr." is fine, "3:15pm"
  is better as "a quarter past three").
- Use `[pause …]` sparingly for dramatic beats. Paragraph and dialogue pauses already give a
  natural rhythm.

## Voices for `tts_models/en/vctk/vits`

The model has 109 speakers, `p225`…`p376`, all reading in British-Isles accents unless
noted. These come from the VCTK speaker info. Preview them on `/book/voices` before choosing,
because some voices suit narration much better than others.

| Female | Male |
|---|---|
| p225 English, Southern England | p226 English, Surrey |
| p228 English, Southern England | p227 English, Cumbria (older, 38) |
| p229 English, Southern England | p232 English, Southern England |
| p230 English, Stockton-on-Tees | p237 Scottish, Fife |
| p231 English, Southern England | p241 Scottish, Perth |
| p233 English, Staffordshire | p243 English, London |
| p234 Scottish, Dumfries | p245 Irish, Dublin |
| p236 English, Manchester | p246 Scottish, Selkirk |
| p238 Northern Irish, Belfast | p247 Scottish, Argyll |
| p239 English, SW England | p251 Indian |
| p240 English, Southern England | p252 Scottish, Edinburgh |
| p244 English, Manchester | p254 English, Surrey |
| p248 Indian | p256 English, Birmingham |
| p249 Scottish, Aberdeen | p258 English, Southern England |
| p250 English, SE England | p259 English, Nottingham |
| p253 Welsh, Cardiff | p260 Scottish, Orkney |
| p257 English, Southern England | p263 Scottish, Aberdeen |
| p261 Northern Irish, Belfast | p270 English, Yorkshire |
| p262 Scottish, Edinburgh | p271 Scottish, Fife |
| p264 Scottish, West Lothian | p272 Scottish, Edinburgh |
| p266 Irish, Athlone | p273 English, Suffolk |
| p267 English, Yorkshire | p274 English, Essex |
| p268 English, Southern England | p275 Scottish, Midlothian |
| p269 English, Newcastle | p278 English, Cheshire |
| p276 English, Oxford | p279 English, Leicester |

For the narrator, pick a calm, clear voice by previewing a few on `/book/voices`. Give main
characters voices that differ clearly from the narrator and from each other in gender or accent.
