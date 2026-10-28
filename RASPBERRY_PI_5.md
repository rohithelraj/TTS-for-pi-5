# Running TTS-for-pi-5 on a Raspberry Pi 5

This guide sets up the Coqui TTS server on a Raspberry Pi 5 so any device on your home network can
turn Markdown books into narrated, multi-voice audiobooks (MP3/M4B with chapters).

What you get once it is running:

| URL / place | What it does |
|---|---|
| `http://<pi>.local:5002/book` | Upload a book, follow progress, download MP3/M4B |
| `http://<pi>.local:5002/book/voices` | Preview the 109 voices |
| `http://<pi>.local:5002/` | Quick text-to-speech box |
| `\\<pi>\books` (optional Samba share) | Drop a `.md` file in, get `.mp3`/`.m4b` back next to it |

The book format is described in [`TTS/server/BOOK_FORMAT.md`](TTS/server/BOOK_FORMAT.md).

---

## 1. What you need

- **Raspberry Pi 5, 8 GB recommended.** 4 GB works for the VITS model used here, but leaves little headroom.
- **Active cooler.** Rendering keeps all four cores at 100% for as long as the book takes. Without
  cooling the Pi throttles and gets much slower.
- **Official 27 W USB-C power supply.**
- **Storage:** 32 GB+ microSD (A2 class) or, better, an NVMe/USB SSD. The install is a few GB.
- **Network:** ethernet or Wi-Fi on the same network as the devices you want to use it from.

## 2. Install the OS

1. Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to flash
   **Raspberry Pi OS Lite (64-bit)**. The desktop version works too, but isn't needed.
2. In the imager's settings (⚙️ / "Edit settings"), set:
   - hostname, e.g. `tts-pi` (the server will then be at `http://tts-pi.local:5002`)
   - username and password
   - Wi-Fi (if not using ethernet)
   - **Enable SSH**
3. Boot the Pi and log in from your computer:
   ```bash
   ssh <user>@tts-pi.local
   ```
4. Update it:
   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```

## Quick start with Docker (instead of steps 3–6)

If you'd rather not install Python and packages on the Pi itself, run the server in Docker. The
image uses Python 3.11 and CPU-only PyTorch, and includes `espeak-ng` and `ffmpeg`, so no host
packages are needed beyond Docker.

```bash
# Docker, plus permission to use it without sudo (log out and back in afterwards)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# get the code
git clone https://github.com/rohithelraj/TTS-for-pi-5.git ~/TTS-for-pi-5
cd ~/TTS-for-pi-5
git checkout feat/markdown-audiobooks   # until this branch is merged into dev

# build and start (the first build takes a while)
mkdir -p ~/books ~/tts-data
docker compose -f docker-compose.pi.yml up -d --build
docker compose -f docker-compose.pi.yml logs -f      # Ctrl+C to leave
```

- The server is at `http://tts-pi.local:5002/book`, the same as with the manual install.
- `~/books` is the watch folder, and `~/tts-data` holds the downloaded model and the book jobs.
  Both are kept when you rebuild the image.
- The container restarts on its own after a crash or a reboot (`restart: unless-stopped`), so you
  don't need the systemd step.
- It uses host networking, so it also works on IPv6-only networks, where Docker's default
  bridge network has no internet access.
- To update: `git pull`, then run the same `up -d --build` command again.
- To stop: `docker compose -f docker-compose.pi.yml down`.

Step 7 (Samba) works the same way, because the watch folder is still `~/books` on the Pi.

## 3. Install system packages

```bash
sudo apt install -y git curl build-essential espeak-ng ffmpeg libsndfile1 avahi-daemon
```

| Package | Why |
|---|---|
| `build-essential` | compiles a small Cython extension and some Python packages without ARM wheels |
| `espeak-ng` | turns text into phonemes for the VITS model (required) |
| `ffmpeg` | encodes MP3 and M4B with chapter markers |
| `libsndfile1` | audio file I/O |
| `avahi-daemon` | makes `tts-pi.local` resolve on your network (usually already installed) |

## 4. Install Python 3.11 and the project

> **Important:** this project supports **Python 3.9–3.11 only**. Current Raspberry Pi OS versions ship a
> newer Python (3.13 on Trixie), so don't use the system `python3`. The steps below use
> [`uv`](https://docs.astral.sh/uv/) to install a separate Python 3.11. That works on any Pi OS version.

```bash
# uv: Python installer and package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc            # or log out and back in, so `uv` is on PATH

# get the code
git clone https://github.com/rohithelraj/TTS-for-pi-5.git ~/TTS-for-pi-5
cd ~/TTS-for-pi-5
git checkout feat/markdown-audiobooks   # until this branch is merged into dev

# Python 3.11 virtual environment + install
uv venv -p 3.11 ~/tts-venv
VIRTUAL_ENV=~/tts-venv uv pip install -e . "setuptools<81"
```

- `setuptools<81` is required. The librosa version used by this project imports `pkg_resources`,
  which newer setuptools no longer includes.
- The install downloads PyTorch and many language packages. Expect **10–30 minutes**, longer on an
  SD card.

Check it worked:

```bash
~/tts-venv/bin/tts-server --help | head -5
```

## 5. First run

```bash
mkdir -p ~/books
~/tts-venv/bin/tts-server --model_name tts_models/en/vctk/vits --book_watch_dir ~/books
```

- The first start downloads the model (~150 MB) to `~/.local/share/tts/`. Later starts load it from disk.
- Wait until the log shows the server listening on port `5002`, then open
  **`http://tts-pi.local:5002/book`** from another computer or phone on the same network.
- Press `Ctrl+C` to stop it.

### Try it

1. Copy the sample book from the repo to your computer: `tests/inputs/sample_book.md`.
2. Upload it on `/book`, then click **Check** and **Render**.
3. When the job shows `done`, download the MP3/M4B. The `rtf` value is the real-time factor:
   `0.5` means one minute of audio took 30 seconds to render. My rough estimate for a Pi 5 is
   0.5–1.5, depending on cooling and text; it hasn't been measured yet.

Or from a terminal on another machine:

```bash
curl -F file=@sample_book.md http://tts-pi.local:5002/api/book          # returns {"id": "...", ...}
curl http://tts-pi.local:5002/api/book/<id>                             # status / progress
curl -OJ http://tts-pi.local:5002/api/book/<id>/m4b                     # download
```

## 6. Start automatically on boot (systemd)

The repo includes a unit file, [`TTS/server/tts-books.service`](TTS/server/tts-books.service). Copy it
and fill in your username and paths:

```bash
sudo cp ~/TTS-for-pi-5/TTS/server/tts-books.service /etc/systemd/system/
sudo nano /etc/systemd/system/tts-books.service
```

Set these to match your setup (replace `pi` with your username):

```ini
User=pi
WorkingDirectory=/home/pi/TTS-for-pi-5
ExecStart=/home/pi/tts-venv/bin/tts-server --model_name tts_models/en/vctk/vits --book_watch_dir /home/pi/books
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tts-books
systemctl status tts-books          # should say "active (running)"
journalctl -u tts-books -f          # live log, Ctrl+C to leave
```

## 7. Watch folder over the network (optional, Samba)

With a network share you can drop Markdown files onto the Pi from Finder/Explorer and pick up the
audio from the same folder.

```bash
sudo apt install -y samba
sudo nano /etc/samba/smb.conf
```

Add at the end (replace `pi` with your username):

```ini
[books]
   path = /home/pi/books
   browseable = yes
   read only = no
   valid users = pi
```

Set a Samba password and restart:

```bash
sudo smbpasswd -a pi
sudo systemctl restart smbd
```

Connect from:
- **macOS:** Finder → Go → Connect to Server → `smb://tts-pi.local/books`
- **Windows:** Explorer → `\\tts-pi\books`

How the folder behaves:
- `story.md` or `story.book.md` **with** front matter → `story.mp3` and `story.m4b` appear next to it.
- If the file is invalid, `story.error.txt` explains why. Fix the file and save it, and it is picked up again.
- Markdown **without** front matter is ignored, so raw drafts can sit in the same folder.
- A file is re-rendered only when it changes.

## 8. Writing books

- Format reference: [`TTS/server/BOOK_FORMAT.md`](TTS/server/BOOK_FORMAT.md).
- Choose voices on `/book/voices`. Click a voice id to hear it, then use the ids in your book's
  `voices:` front matter.
- **Converting existing text:** open this repo in Claude Code on your computer and ask
  *"format chapter1.md for audiobook"*. The `book-formatter` agent writes `chapter1.book.md`
  with dialogue attributed and voices assigned, and lists any lines whose speaker it had to guess.
  Copy the result into the watch folder or upload it.

## 9. Updating

```bash
cd ~/TTS-for-pi-5
git pull
VIRTUAL_ENV=~/tts-venv uv pip install -e . "setuptools<81"
sudo systemctl restart tts-books
```

## 10. Server options

| Option | Default | Meaning |
|---|---|---|
| `--model_name` | `tts_models/en/ljspeech/tacotron2-DDC` | Use `tts_models/en/vctk/vits` for multiple voices |
| `--port` | `5002` | HTTP port |
| `--host` | `::` | Listen address (all interfaces) |
| `--book_watch_dir` | off | Folder to watch for books |
| `--book_data_dir` | `~/.local/share/tts-books` | Uploaded books, rendered audio, job list |
| `--book_formats` | `mp3,m4b` | Any of `mp3`, `m4b`, `wav` |

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'pkg_resources'` | `VIRTUAL_ENV=~/tts-venv uv pip install "setuptools<81"` |
| Install says Python version not supported | You're using the system Python. Create the venv with `uv venv -p 3.11` as in step 4. |
| `espeak not installed` / phonemizer error | `sudo apt install espeak-ng` |
| `tts-pi.local` doesn't resolve | Use the IP (`hostname -I` on the Pi). Check `avahi-daemon` is running. On Windows, also try `http://tts-pi:5002`. |
| Page doesn't load from other devices | Is the service running (`systemctl status tts-books`)? Are you on the same network/VLAN? If you enabled a firewall: `sudo ufw allow 5002/tcp` |
| Books render as `.wav` only | `ffmpeg` is missing: `sudo apt install ffmpeg`, then restart |
| Rendering is very slow | Check for throttling: `vcgencmd get_throttled` should print `0x0`. Add cooling. Don't run other heavy work at the same time. |
| Killed / out of memory during install | Add temporary swap, then retry: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` (gone after reboot) |
| Model download fails (`coqui.gateway.scarf.sh` / GitHub unreachable) | Your network may be IPv6-only: check that `ip -4 route` shows a `default` route. The model host is IPv4-only, so turn on IPv4 or NAT64 on your router, or download `tts_models--en--vctk--vits.zip` on another computer and unzip it into `~/.local/share/tts/` (Docker: `~/tts-data/tts/`). |
| Docker build: `Temporary failure resolving 'deb.debian.org'` | Same cause: Docker builds use an IPv4-only network. Build while the Pi has IPv4. |
| Unknown speaker id error on upload | The id isn't in the model. Pick one from `/book/voices`. |

## Security

The server has **no login**. Anyone on your network can use it, upload books, and delete jobs.
Keep it on your home network and **don't forward port 5002 on your router**. For remote access, use
a VPN such as Tailscale or WireGuard.
