#!/usr/bin/env python3
# ripr. backend v3 — by Puneeth Acharya
# setup :  pip install flask flask-cors yt-dlp     (plus ffmpeg for MP3/merging)
# run   :  RIPR_KEY=your-secret python server.py
# connect from GitHub Pages site:  open  https://puneethacharya71.github.io/ripr/?api=http://localhost:8000
# lock CORS to your site:          RIPR_ORIGIN=https://puneethacharya71.github.io python server.py
import json, os, re, tempfile, threading, time
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
from yt_dlp import YoutubeDL

ADMIN_KEY = os.environ.get("RIPR_KEY", "ripr-owner")          # CHANGE THIS
DB_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "visits.json")
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
INDEX     = os.environ.get("RIPR_INDEX", os.path.join(BASE_DIR, "index.html"))
COOKIES   = os.environ.get("RIPR_COOKIES")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.environ.get("RIPR_ORIGIN", "*")},
                     "allow_headers": ["Content-Type", "X-Admin-Key"]})
_lock = threading.Lock()

def _load():
    try:
        with open(DB_FILE) as f: return json.load(f)
    except Exception: return []

def _save(rows):
    with open(DB_FILE, "w") as f: json.dump(rows[-500:], f)

def _base_opts():
    o = {"quiet": True, "noplaylist": True, "noprogress": True}
    if COOKIES: o["cookiefile"] = COOKIES
    return o

def _fmt_opts(kind, q):
    o = _base_opts()
    if kind == "audio":
        o["format"] = "bestaudio/best"
        o["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        h = int(q or 1080)
        o["format"] = f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/b"
        o["merge_output_format"] = "mp4"
    return o

@app.get("/api/health")
def health():
    return jsonify(ok=True, service="ripr", version="3.0")

@app.post("/api/info")
def info():
    url = (request.get_json(force=True) or {}).get("url", "")
    try:
        with YoutubeDL(_base_opts()) as y:
            m = y.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=str(e)[:300]), 502
    fmts = []
    for f in m.get("formats") or []:
        if f.get("vcodec") != "none" and f.get("height"):
            fmts.append({"kind": "video", "q": f["height"]})
        elif f.get("acodec") != "none" and f.get("vcodec") == "none":
            fmts.append({"kind": "audio", "q": int(f.get("abr") or 128)})
    seen, uniq = set(), []
    for f in sorted(fmts, key=lambda x: -x["q"]):
        k = (f["kind"], f["q"])
        if k not in seen: seen.add(k); uniq.append(f)
    return jsonify(id=m.get("id"), title=m.get("title"), duration=m.get("duration"),
                   thumbnail=m.get("thumbnail"), uploader=m.get("uploader"),
                   extractor=m.get("extractor_key"), formats=uniq[:10])

@app.post("/api/rip")
def rip():
    b = request.get_json(force=True) or {}
    url, kind, q = b.get("url", ""), b.get("kind", "video"), b.get("q", 1080)
    if not re.match(r"^https?://", url):
        return jsonify(error="invalid url"), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin"); tmp.close()
    try:
        o = _fmt_opts(kind, q)
        o["outtmpl"] = tmp.name + ".%(ext)s"
        with YoutubeDL(o) as y:
            m = y.extract_info(url, download=True)
        produced = tmp.name + (".mp3" if kind == "audio" else ".mp4")
        if not os.path.exists(produced):
            produced = next((os.path.join(BASE_DIR, f) for f in os.listdir(BASE_DIR)
                             if f.startswith(os.path.basename(tmp.name))), tmp.name)
        name = re.sub(r"[^\w\- ]", "", (m.get("title") or "ripr"))[:60].strip() or "ripr"
        name += ".mp3" if kind == "audio" else ".mp4"
        size = os.path.getsize(produced)

        def stream(path=produced):
            try:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(64 * 1024)
                        if not chunk: break
                        yield chunk
            finally:
                try: os.unlink(path)
                except OSError: pass

        return Response(stream(), headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Rip-Size": str(size),
            "Content-Type": "audio/mpeg" if kind == "audio" else "video/mp4"})
    except Exception as e:
        for f in os.listdir(BASE_DIR):
            if f.startswith(os.path.basename(tmp.name)):
                try: os.unlink(os.path.join(BASE_DIR, f))
                except OSError: pass
        return jsonify(error=str(e)[:300]), 502

@app.post("/api/log")
def log():
    rec = request.get_json(force=True) or {}
    if not rec.get("id"): return jsonify(error="id required"), 400
    rec["seen"] = time.time()
    with _lock:
        rows = [r for r in _load() if r.get("id") != rec["id"]] + [rec]
        _save(rows)
    return jsonify(ok=True)

@app.get("/api/visits")
def visits():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(sorted(_load(), key=lambda r: r.get("t0", 0)))

@app.get("/")
def index():
    return send_file(INDEX)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
