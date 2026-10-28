# :frog: TTS demo server
Before you use the server, make sure you [install](https://github.com/coqui-ai/TTS/tree/dev#install-tts)) :frog: TTS properly. Then, you can follow the steps below.

**Note:** If you install :frog:TTS using ```pip```, you can also use the ```tts-server``` end point on the terminal.

Examples runs:

List officially released models.
```python TTS/server/server.py  --list_models ```

Run the server with the official models.
```python TTS/server/server.py  --model_name tts_models/en/ljspeech/tacotron2-DCA --vocoder_name vocoder_models/en/ljspeech/multiband-melgan```

Run the server with the official models on a GPU.
```CUDA_VISIBLE_DEVICES="0" python TTS/server/server.py  --model_name tts_models/en/ljspeech/tacotron2-DCA --vocoder_name vocoder_models/en/ljspeech/multiband-melgan --use_cuda True```

Run the server with a custom models.
```python TTS/server/server.py  --tts_checkpoint /path/to/tts/model.pth --tts_config /path/to/tts/config.json --vocoder_checkpoint /path/to/vocoder/model.pth --vocoder_config /path/to/vocoder/config.json```

## Audiobooks from markdown

The server can turn [Book Markdown](BOOK_FORMAT.md) files into narrated audiobooks. A narrator reads the
prose, each character gets their own voice, and pauses fall at paragraphs, dialogue, headings and scene
breaks. Output is MP3 and M4B with chapter markers. Use a multi-speaker model:

```bash
tts-server --model_name tts_models/en/vctk/vits --book_watch_dir ~/books
```

- `http://<host>:5002/book`: upload or paste a book, follow progress, download the result.
- `http://<host>:5002/book/voices`: preview every speaker id.
- Watch folder: drop `story.md` (or `story.book.md`) into `--book_watch_dir`. `story.mp3`/`story.m4b`
  appear next to it, or `story.error.txt` if the file is invalid. Markdown files without front matter
  are ignored, so raw drafts can sit in the same folder.
- REST API:
  ```bash
  curl -F file=@story.book.md http://pi.local:5002/api/book/validate   # check only
  curl -F file=@story.book.md http://pi.local:5002/api/book            # -> {"id": "...", ...}
  curl http://pi.local:5002/api/book/<id>                              # status, progress, stats
  curl -OJ http://pi.local:5002/api/book/<id>/m4b                      # download (mp3|m4b|wav)
  curl http://pi.local:5002/api/books                                  # all jobs
  curl -X DELETE http://pi.local:5002/api/book/<id>                    # cancel / delete
  ```

Options: `--book_formats mp3,m4b` (any of `mp3,m4b,wav`; mp3/m4b need `ffmpeg`),
`--book_data_dir` (uploads and outputs, default `~/.local/share/tts-books`), `--host`.

To convert ordinary markdown into Book Markdown, use the `book-formatter` Claude Code agent in this
repo (`.claude/agents/book-formatter.md`). Ask Claude Code to "format chapter1.md for audiobook".
It writes `chapter1.book.md`, assigns voices, and lists any dialogue it had to guess.

### Raspberry Pi 5

See [RASPBERRY_PI_5.md](../../RASPBERRY_PI_5.md) for the full setup guide: OS, install, systemd,
Samba watch folder, and troubleshooting.
