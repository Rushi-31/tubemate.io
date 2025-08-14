# app.py
import os
import re
import json
import math
import shutil
import pathlib
import subprocess
from urllib.parse import unquote
from flask import Flask, request, jsonify, Response, send_from_directory, abort

app = Flask(__name__)

# --- Binaries ---
YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# --- Helpers ---
def run_cmd_once(cmd):
    """Run a command and return (stdout, stderr, returncode)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.stdout, p.stderr, p.returncode
    except FileNotFoundError:
        return "", f"Command not found: {cmd[0]}", 127

def human_bytes(n):
    """Convert bytes -> human-readable string."""
    try:
        n = float(n)
    except Exception:
        return None
    if n == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(n, 1024)))
    p = math.pow(1024, i)
    s = round(n / p, 2)
    return f"{s} {units[i]}"

def safe_path(p):
    """Path safety: expand user, resolve, and create if not exists."""
    if not p:
        return str(pathlib.Path.cwd())
    target = pathlib.Path(p).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    return str(target)

def default_playlist_format():
    """Format string for playlists: <=720p mixed if available, else best <=720."""
    return "bv*[height<=720]+ba/b[height<=720]"

def is_probably_playlist(url: str) -> bool:
    u = (url or "").lower()
    return ("list=" in u) or ("/playlist" in u)

# --- API: Get formats for single video (video-only and audio-only only) ---
@app.route("/get_formats", methods=["GET", "POST"])
def get_formats_api():
    # Accept url via query param or JSON body
    url = request.args.get("url")
    if not url and request.is_json:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url")
    if not url:
        return jsonify({"error": "Missing url"}), 400

    # If clearly a playlist, skip listing formats
    if is_probably_playlist(url):
        return jsonify({
            "title": "Playlist",
            "is_playlist": True,
            "video_formats": [],
            "audio_formats": []
        })

    def fetch_formats_for(given_url, extra_args):
        out, err, code = run_cmd_once([YT_DLP, "-J"] + extra_args + [given_url])
        raw_json = (out or "").strip() or (err or "").strip()
        if not raw_json:
            return None, f"No output from yt-dlp (exit {code})"
        try:
            return json.loads(raw_json), None
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON from yt-dlp: {str(e)}"

    # Try single video first
    data, error = fetch_formats_for(url, ["--no-playlist"])
    # Fallback: without --no-playlist (in case URL is ambiguous)
    if not data or not data.get("formats"):
        data, error = fetch_formats_for(url, [])
        if data and data.get("entries") and not data.get("formats"):
            # Behaves like playlist; we skip format listing for playlists
            return jsonify({
                "title": data.get("title") or "Playlist",
                "is_playlist": True,
                "video_formats": [],
                "audio_formats": []
            })

    if not data or not data.get("formats"):
        return jsonify({"error": error or "No formats found"}), 404

    formats = data.get("formats", [])
    # Only expose video-only and audio-only
    video_formats, audio_formats = [], []
    for f in formats:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        size = f.get("filesize") or f.get("filesize_approx")
        it = {
            "format_id": f.get("format_id"),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "ext": f.get("ext"),
            "abr": f.get("abr"),
            "filesize": size,
            "filesize_hr": human_bytes(size) if size else None,
        }
        if vcodec != "none" and acodec == "none":
            video_formats.append(it)
        elif acodec != "none" and vcodec == "none":
            audio_formats.append(it)

    # Sorts
    video_formats.sort(key=lambda x: (x["height"] or 0, x["fps"] or 0), reverse=True)
    audio_formats.sort(key=lambda x: (x["abr"] or 0), reverse=True)

    return jsonify({
        "title": data.get("title", "video"),
        "is_playlist": False,
        "video_formats": video_formats,
        "audio_formats": audio_formats
    })

# --- API: Download Progress (single or playlist) ---
@app.route("/progress")
def progress():
    url = request.args.get("url", "").strip()
    # 'quality' now carries either "vfmt+afmt" OR just "vfmt" (server will add +ba)
    quality = request.args.get("quality", "").strip()
    download_path = safe_path(unquote(request.args.get("download_path", "").strip()))
    force_playlist = request.args.get("is_playlist", "false").lower() in ("1", "true", "yes")
    playlist_mode = force_playlist or is_probably_playlist(url)

    if not url:
        return jsonify({"error": "Missing url"}), 400

    cmd = [YT_DLP, "--newline"]

    if playlist_mode:
        out_tmpl = os.path.join(download_path, "%(playlist_title,channel)s", "%(title)s.%(ext)s")
        fmt = default_playlist_format()
        cmd += ["-f", fmt, "--yes-playlist"]
    else:
        out_tmpl = os.path.join(download_path, "%(title)s.%(ext)s")
        if not quality:
            return jsonify({"error": "Missing quality (vfmt or vfmt+afmt) for single video"}), 400

        # Enforce "download video and audio then merge":
        # - If quality already contains '+', use it as-is (vfmt+afmt).
        # - If it's only a video format (likely), append +ba to merge with best audio.
        if "+" not in quality:
            # This ensures audio gets downloaded and muxed with the selected video
            quality = f"{quality}+ba"

        cmd += ["-f", quality, "--no-playlist"]

    if FFMPEG:
        cmd += ["--ffmpeg-location", FFMPEG]

    cmd += ["-o", out_tmpl, url]

    # [download]  12.3% of 12.34MiB at 1.23MiB/s ETA 00:11
    progress_re = re.compile(
        r"^\[download\]\s+(?P<pct>\d{1,3}(?:\.\d+)?)%\s+of\s+(?P<size>\S+)\s+at\s+(?P<speed>\S+)\s+ETA\s+(?P<eta>\S+)"
    )

    def sse_stream():
        def send(event_dict):
            yield f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n"

        yield from send({"status": "starting"})
        try:
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            ) as proc:
                for line in proc.stdout:
                    line = line.rstrip()
                    m = progress_re.match(line)
                    if m:
                        try:
                            pct = float(m.group("pct"))
                        except Exception:
                            pct = None
                        yield from send({
                            "status": "downloading",
                            "percent": pct,
                            "size": m.group("size"),
                            "speed": m.group("speed"),
                            "eta": m.group("eta")
                        })
                        continue

                    if line.startswith("[download] Destination:"):
                        yield from send({"status": "destination", "message": line.split("Destination:", 1)[-1].strip()})
                    elif "has already been downloaded" in line:
                        yield from send({"status": "already", "message": line.strip()})
                    elif line.startswith("[Merger]"):
                        yield from send({"status": "merging", "message": line.strip()})
                    elif line.startswith("[ExtractAudio]"):
                        yield from send({"status": "postprocess", "message": line.strip()})
                    elif line.startswith("[youtube]") or line.startswith("[info]"):
                        yield from send({"status": "info", "message": line.strip()})
                    elif line.startswith("ERROR:"):
                        yield from send({"status": "error", "message": line.strip()})

                ret = proc.wait()
                if ret == 0:
                    yield from send({"status": "finished"})
                else:
                    yield from send({"status": "error", "message": f"yt-dlp exited with code {ret}"})
        except FileNotFoundError as e:
            yield f"data: {json.dumps({'status':'error','message': str(e)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'status':'error','message': repr(e)})}\n\n"

    return Response(sse_stream(), mimetype="text/event-stream")

# --- Web pages ---
@app.route("/")
def root():
    idx = pathlib.Path("index.html")
    if idx.exists():
        return idx.read_text(encoding="utf-8")
    return "Place index.html next to app.py or host frontend separately."

@app.route("/static/<path:path>")
def static_files(path):
    base = pathlib.Path("static").resolve()
    file_path = base / path
    if not file_path.exists():
        abort(404)
    return send_from_directory(str(base), path)

if __name__ == "__main__":
    # For production: gunicorn -w 2 -k gevent app:app
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
