#!flask/bin/python
import argparse
import io
import json
import os
import sys
from pathlib import Path
from threading import Lock
from typing import Union
from urllib.parse import parse_qs

from flask import Flask, abort, jsonify, render_template, render_template_string, request, send_file

from TTS.config import load_config
from TTS.server.book.jobs import JobQueue, safe_name
from TTS.server.book.parser import BookError, summarize
from TTS.server.book.renderer import FORMATS, has_ffmpeg
from TTS.server.book.watcher import FolderWatcher
from TTS.utils.manage import ModelManager
from TTS.utils.synthesizer import Synthesizer


def create_argparser():
    def convert_boolean(x):
        return x.lower() in ["true", "1", "yes"]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--list_models",
        type=convert_boolean,
        nargs="?",
        const=True,
        default=False,
        help="list available pre-trained tts and vocoder models.",
    )
    parser.add_argument(
        "--model_name",
        type=str,
        default="tts_models/en/ljspeech/tacotron2-DDC",
        help="Name of one of the pre-trained tts models in format <language>/<dataset>/<model_name>",
    )
    parser.add_argument("--vocoder_name", type=str, default=None, help="name of one of the released vocoder models.")

    # Args for running custom models
    parser.add_argument("--config_path", default=None, type=str, help="Path to model config file.")
    parser.add_argument(
        "--model_path",
        type=str,
        default=None,
        help="Path to model file.",
    )
    parser.add_argument(
        "--vocoder_path",
        type=str,
        help="Path to vocoder model file. If it is not defined, model uses GL as vocoder. Please make sure that you installed vocoder library before (WaveRNN).",
        default=None,
    )
    parser.add_argument("--vocoder_config_path", type=str, help="Path to vocoder model config file.", default=None)
    parser.add_argument("--speakers_file_path", type=str, help="JSON file for multi-speaker model.", default=None)
    parser.add_argument("--port", type=int, default=5002, help="port to listen on.")
    parser.add_argument("--host", type=str, default="::", help="address to listen on.")
    parser.add_argument(
        "--book_data_dir",
        type=str,
        default="~/.local/share/tts-books",
        help="where uploaded books, rendered audiobooks and the job list are stored.",
    )
    parser.add_argument(
        "--book_watch_dir",
        type=str,
        default=None,
        help="folder to watch for Book Markdown files; audio is written next to each file.",
    )
    parser.add_argument(
        "--book_formats",
        type=str,
        default="mp3,m4b",
        help=f"comma separated audiobook output formats, any of: {','.join(FORMATS)}.",
    )
    parser.add_argument("--use_cuda", type=convert_boolean, default=False, help="true to use CUDA.")
    parser.add_argument("--debug", type=convert_boolean, default=False, help="true to enable Flask debug mode.")
    parser.add_argument("--show_details", type=convert_boolean, default=False, help="Generate model detail page.")
    return parser


# parse the args
args = create_argparser().parse_args()

path = Path(__file__).parent / "../.models.json"
manager = ModelManager(path)

if args.list_models:
    manager.list_models()
    sys.exit()

# update in-use models to the specified released models.
model_path = None
config_path = None
speakers_file_path = None
vocoder_path = None
vocoder_config_path = None

# CASE1: list pre-trained TTS models
if args.list_models:
    manager.list_models()
    sys.exit()

# CASE2: load pre-trained model paths
if args.model_name is not None and not args.model_path:
    model_path, config_path, model_item = manager.download_model(args.model_name)
    args.vocoder_name = model_item["default_vocoder"] if args.vocoder_name is None else args.vocoder_name

if args.vocoder_name is not None and not args.vocoder_path:
    vocoder_path, vocoder_config_path, _ = manager.download_model(args.vocoder_name)

# CASE3: set custom model paths
if args.model_path is not None:
    model_path = args.model_path
    config_path = args.config_path
    speakers_file_path = args.speakers_file_path

if args.vocoder_path is not None:
    vocoder_path = args.vocoder_path
    vocoder_config_path = args.vocoder_config_path

# load models
synthesizer = Synthesizer(
    tts_checkpoint=model_path,
    tts_config_path=config_path,
    tts_speakers_file=speakers_file_path,
    tts_languages_file=None,
    vocoder_checkpoint=vocoder_path,
    vocoder_config=vocoder_config_path,
    encoder_checkpoint="",
    encoder_config="",
    use_cuda=args.use_cuda,
)

use_multi_speaker = hasattr(synthesizer.tts_model, "num_speakers") and (
    synthesizer.tts_model.num_speakers > 1 or synthesizer.tts_speakers_file is not None
)
speaker_manager = getattr(synthesizer.tts_model, "speaker_manager", None)

use_multi_language = hasattr(synthesizer.tts_model, "num_languages") and (
    synthesizer.tts_model.num_languages > 1 or synthesizer.tts_languages_file is not None
)
language_manager = getattr(synthesizer.tts_model, "language_manager", None)

# TODO: set this from SpeakerManager
use_gst = synthesizer.tts_config.get("use_gst", False)
app = Flask(__name__)
lock = Lock()

book_formats = [f.strip() for f in args.book_formats.split(",") if f.strip()]
if not has_ffmpeg() and set(book_formats) - {"wav"}:
    print(" > ffmpeg not found, audiobooks will be rendered as wav only.")
    book_formats = ["wav"]
speaker_ids = sorted(speaker_manager.name_to_id) if use_multi_speaker and speaker_manager is not None else []
# with --debug, Flask's reloader also imports this module in a supervisor process that serves no
# requests; only start the book worker in the serving process so jobs don't render twice
if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    book_jobs = JobQueue(synthesizer, lock, args.book_data_dir, book_formats, speaker_ids)
    book_watcher = FolderWatcher(args.book_watch_dir, book_jobs) if args.book_watch_dir else None


def style_wav_uri_to_dict(style_wav: str) -> Union[str, dict]:
    """Transform an uri style_wav, in either a string (path to wav file to be use for style transfer)
    or a dict (gst tokens/values to be use for styling)

    Args:
        style_wav (str): uri

    Returns:
        Union[str, dict]: path to file (str) or gst style (dict)
    """
    if style_wav:
        if os.path.isfile(style_wav) and style_wav.endswith(".wav"):
            return style_wav  # style_wav is a .wav file located on the server

        style_wav = json.loads(style_wav)
        return style_wav  # style_wav is a gst dictionary with {token1_id : token1_weigth, ...}
    return None


@app.route("/")
def index():
    return render_template(
        "index.html",
        show_details=args.show_details,
        use_multi_speaker=use_multi_speaker,
        use_multi_language=use_multi_language,
        speaker_ids=speaker_manager.name_to_id if speaker_manager is not None else None,
        language_ids=language_manager.name_to_id if language_manager is not None else None,
        use_gst=use_gst,
    )


@app.route("/details")
def details():
    if args.config_path is not None and os.path.isfile(args.config_path):
        model_config = load_config(args.config_path)
    else:
        if args.model_name is not None:
            model_config = load_config(config_path)

    if args.vocoder_config_path is not None and os.path.isfile(args.vocoder_config_path):
        vocoder_config = load_config(args.vocoder_config_path)
    else:
        if args.vocoder_name is not None:
            vocoder_config = load_config(vocoder_config_path)
        else:
            vocoder_config = None

    return render_template(
        "details.html",
        show_details=args.show_details,
        model_config=model_config,
        vocoder_config=vocoder_config,
        args=args.__dict__,
    )


@app.route("/api/tts", methods=["GET", "POST"])
def tts():
    with lock:
        text = request.headers.get("text") or request.values.get("text", "")
        speaker_idx = request.headers.get("speaker-id") or request.values.get("speaker_id", "")
        language_idx = request.headers.get("language-id") or request.values.get("language_id", "")
        style_wav = request.headers.get("style-wav") or request.values.get("style_wav", "")
        style_wav = style_wav_uri_to_dict(style_wav)

        print(f" > Model input: {text}")
        print(f" > Speaker Idx: {speaker_idx}")
        print(f" > Language Idx: {language_idx}")
        wavs = synthesizer.tts(text, speaker_name=speaker_idx, language_name=language_idx, style_wav=style_wav)
        out = io.BytesIO()
        synthesizer.save_wav(wavs, out)
    return send_file(out, mimetype="audio/wav")


# Basic MaryTTS compatibility layer


@app.route("/locales", methods=["GET"])
def mary_tts_api_locales():
    """MaryTTS-compatible /locales endpoint"""
    # NOTE: We currently assume there is only one model active at the same time
    if args.model_name is not None:
        model_details = args.model_name.split("/")
    else:
        model_details = ["", "en", "", "default"]
    return render_template_string("{{ locale }}\n", locale=model_details[1])


@app.route("/voices", methods=["GET"])
def mary_tts_api_voices():
    """MaryTTS-compatible /voices endpoint"""
    # NOTE: We currently assume there is only one model active at the same time
    if args.model_name is not None:
        model_details = args.model_name.split("/")
    else:
        model_details = ["", "en", "", "default"]
    return render_template_string(
        "{{ name }} {{ locale }} {{ gender }}\n", name=model_details[3], locale=model_details[1], gender="u"
    )


@app.route("/process", methods=["GET", "POST"])
def mary_tts_api_process():
    """MaryTTS-compatible /process endpoint"""
    with lock:
        if request.method == "POST":
            data = parse_qs(request.get_data(as_text=True))
            # NOTE: we ignore param. LOCALE and VOICE for now since we have only one active model
            text = data.get("INPUT_TEXT", [""])[0]
        else:
            text = request.args.get("INPUT_TEXT", "")
        print(f" > Model input: {text}")
        wavs = synthesizer.tts(text)
        out = io.BytesIO()
        synthesizer.save_wav(wavs, out)
    return send_file(out, mimetype="audio/wav")


# Audiobooks from Book Markdown, see BOOK_FORMAT.md


def _book_request_markdown():
    """Book markdown from a multipart `file`, a `text` form field, or the raw request body."""
    upload = request.files.get("file")
    if upload is not None:
        return upload.read().decode("utf-8"), upload.filename
    if "text" in request.form:
        return request.form["text"], request.form.get("name") or "book.md"
    return request.get_data(as_text=True), request.args.get("name") or "book.md"


@app.route("/book")
def book_index():
    return render_template("book.html", formats=book_formats, watch_dir=args.book_watch_dir)


@app.route("/book/voices")
def book_voices():
    return render_template("book_voices.html", speaker_ids=speaker_ids)


@app.route("/api/book/validate", methods=["POST"])
def book_validate():
    md, _ = _book_request_markdown()
    try:
        book = book_jobs.validate(md)
    except BookError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **summarize(book)})


@app.route("/api/book", methods=["POST"])
def book_submit():
    md, name = _book_request_markdown()
    try:
        job = book_jobs.submit(md, name=name)
    except BookError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(job), 202


@app.route("/api/books", methods=["GET"])
def book_list():
    return jsonify(book_jobs.list_jobs())


@app.route("/api/book/<job_id>", methods=["GET", "DELETE"])
def book_job(job_id):
    if request.method == "DELETE":
        if not book_jobs.delete(job_id):
            abort(404)
        return "", 204
    job = book_jobs.get(job_id)
    if job is None:
        abort(404)
    return jsonify(job)


@app.route("/api/book/<job_id>/<fmt>", methods=["GET"])
def book_download(job_id, fmt):
    path = book_jobs.output_path(job_id, fmt)
    if path is None:
        abort(404)
    name = safe_name(book_jobs.get(job_id)["name"])
    return send_file(path, as_attachment=True, download_name=f"{name}.{fmt}")


def main():
    app.run(debug=args.debug, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
