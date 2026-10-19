#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ripr. server  —  v3.0  (companion to ripr.html)
================================================
A tiny Flask server that turns the ripr. front-end from "demo mode" into
"SERVER LIVE" mode. It uses the open-source yt-dlp engine to actually pull
media from YouTube, Shorts, YT Music and Instagram reels/videos.

-------------------------------------------------------------------
SETUP  (one time)
-------------------------------------------------------------------
    pip install -r requirements.txt
        # contents of requirements.txt:
        #   flask>=3.0
        #   flask-cors>=4.0
        #   yt-dlp>=2024.1.0

    # ffmpeg is REQUIRED for merging video+audio and converting to mp3.
    #   macOS:    brew install ffmpeg
    #   Ubuntu:   sudo apt install ffmpeg
    #   Windows:  https://ffmpeg.org/download.html  (put in PATH)

-------------------------------------------------------------------
RUN
-------------------------------------------------------------------
    python server.py
        # serves on http://localhost:8000

    # custom port / admin key:
    PORT=9000 RIPR_KEY=mysecret python server.py

-------------------------------------------------------------------
CONNECT THE FRONT-END
-------------------------------------------------------------------
    Open ripr.html with this query string:
        ripr.html?api=http://localhost:8000
    The badge in the header will flip to "SERVER LIVE" and real downloads
    start working. The setting is saved in localStorage so it survives
    reloads. Disconnect with ?api=0.

-------------------------------------------------------------------
ENDPOINTS  (consumed by ripr.html — do not change without editing the UI)
-------------------------------------------------------------------
    GET  /api/health   -> {service:'ripr', mode:'live', ...}
    POST /api/info     body {url}              -> metadata json
    POST /api/rip      body {url,kind,q}      -> binary stream
    POST /api/log      body {session...}      -> {ok:true}
    GET  /api/visits    Header X-Admin-Key     -> [session,...]

    kind: 'video' | 'audio'
    q:    video -> height in px  ('1080','720','480','360')
          audio -> bitrate in kbps ('320','128')

Author: Puneeth Acharya  ·  plain Python + Flask + yt-dlp  ·  zero ads, forever.
"""

import os
import re
import json
import time
import shutil
import tempfile
import threading
from urllib.parse import quote

from flask import (
    Flask, request, jsonify, Response, send_file, after_this_request
)
from flask_cors import CORS

try:
    import yt_dlp
except ImportError:
    yt_dlp = None
    print("[!] yt-dlp not installed. Run: pip install yt-dlp")


# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
RIPR_KEY   = os.environ.get('RIPR_KEY', 'admin')        # admin key for /api/visits
PORT       = int(os.environ.get('PORT', '8000'))
HOST       = os.environ.get('HOST', '0.0.0.0')
LOG_FILE   = os.environ.get('RIPR_LOG_FILE', 'ripr_visits.json')
MAX_VISITS = 1000

app = Flask(__name__)
# Allow the front-end (file:// or github pages or any origin) to call us.
# Expose X-Rip-Size + Content-Disposition so the JS progress bar works.
CORS(app, expose_headers=['X-Rip-Size', 'Content-Disposition', 'Content-Length'])

VISITS = []
VISITS_LOCK = threading.Lock()


def safe_filename(name: str, maxlen: int = 80) -> str:
    """Strip characters that break filesystems / Content-Disposition."""
    name = (name or 'ripr').strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name)
    name = re.sub(r'\s+', ' ', name)
    name = name.strip('. ')
    if not name:
        name = 'ripr'
    return name[:maxlen]


def load_visits():
    """Optional disk-backed log so visits survive restarts."""
    global VISITS
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            VISITS = json.load(f)[-MAX_VISITS:]
    except Exception:
        VISITS = []


def persist_visits():
    try:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(VISITS, f)
    except Exception:
        pass


# -------------------------------------------------------------------
# HEALTH
# -------------------------------------------------------------------
@app.get('/api/health')
def health():
    """Front-end polls this on load — flips badge to LIVE if service='ripr'."""
    return jsonify({
        'service':  'ripr',
        'mode':     'live',
        'version':  '3.0',
        'engine':   'yt-dlp',
        'ready':    yt_dlp is not None,
        'timestamp': int(time.time()),
    })


# -------------------------------------------------------------------
# INFO  —  metadata only, no download
# -------------------------------------------------------------------
@app.post('/api/info')
def info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'missing url'}), 400
    if yt_dlp is None:
        return jsonify({'error': 'yt-dlp not installed on server'}), 500

    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'nocheckcertificate': True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            raw = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

    # Build the format list the front-end expects:
    #   formats: [{kind:'video', q:'1080'}, {kind:'audio', q:'320'}, ...]
    formats = []
    seen_v, seen_a = set(), set()
    for f in (raw.get('formats') or []):
        # video stream with a real height
        h = f.get('height')
        if h and h not in seen_v and f.get('vcodec') != 'none':
            seen_v.add(h)
            formats.append({'kind': 'video', 'q': str(h)})
        # audio stream with a real bitrate
        abr = f.get('abr')
        if abr and f.get('acodec') != 'none':
            abr_i = round(abr)
            if abr_i not in seen_a:
                seen_a.add(abr_i)
                formats.append({'kind': 'audio', 'q': str(abr_i)})

    # Make sure we have at least *something* sensible to offer in the UI.
    if not any(f['kind'] == 'video' for f in formats):
        formats = [{'kind': 'video', 'q': '1080'},
                   {'kind': 'video', 'q': '720'},
                   {'kind': 'video', 'q': '480'}] + formats
    if not any(f['kind'] == 'audio' for f in formats):
        formats += [{'kind': 'audio', 'q': '320'},
                    {'kind': 'audio', 'q': '128'}]

    uploader = (raw.get('uploader') or raw.get('channel')
                or raw.get('uploader_id') or raw.get('uploader') or 'unknown')

    return jsonify({
        'title':     raw.get('title') or 'untitled',
        'duration':  int(raw.get('duration') or 0),
        'thumbnail': raw.get('thumbnail'),
        'uploader':  uploader,
        'extractor': raw.get('extractor_key') or raw.get('extractor') or 'yt-dlp',
        'formats':   formats,
    })


# -------------------------------------------------------------------
# RIP  —  the real download, streamed back as bytes
# -------------------------------------------------------------------
@app.post('/api/rip')
def rip():
    data = request.get_json(force=True, silent=True) or {}
    url    = (data.get('url') or '').strip()
    kind   = (data.get('kind') or 'video').lower()
    q      = data.get('q')

    if not url:
        return jsonify({'error': 'missing url'}), 400
    if yt_dlp is None:
        return jsonify({'error': 'yt-dlp not installed on server'}), 500

    # ---- 1. quick info pass for filename + duration sanity
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True,
            'skip_download': True, 'noplaylist': True,
            'nocheckcertificate': True,
        }) as ydl:
            meta = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({'error': f'info failed: {e}'}), 500

    base_name = safe_filename(meta.get('title') or 'ripr')
    ext = 'mp3' if kind == 'audio' else 'mp4'

    # ---- 2. choose yt-dlp format string + postprocessors
    if kind == 'audio':
        bitrate = int(q) if q else 320
        ydl_format = 'bestaudio/best'
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': str(bitrate),
        }]
        merge_output_format = None
    else:
        h = int(q) if q else 1080
        # prefer pre-merged mp4, else merge best video<=h with best audio,
        # else fall back to whatever is closest.
        ydl_format = (
            f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/'
            f'bestvideo[height<={h}]+bestaudio/'
            f'best[height<={h}]/best'
        )
        postprocessors = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
        merge_output_format = 'mp4'

    # ---- 3. download into a private temp dir
    workdir = tempfile.mkdtemp(prefix='ripr_')
    outtmpl = os.path.join(workdir, 'out.%(ext)s')

    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'format': ydl_format,
        'outtmpl': outtmpl,
        'postprocessors': postprocessors,
    }
    if merge_output_format:
        opts['merge_output_format'] = merge_output_format

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({'error': f'download failed: {e}'}), 500

    # ---- 4. locate the produced file
    produced = [f for f in os.listdir(workdir)
                if not f.endswith('.part') and not f.endswith('.ytdl')]
    if not produced:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({'error': 'no output file produced'}), 500

    out_path = os.path.join(workdir, produced[0])
    if not os.path.exists(out_path):
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({'error': 'output vanished'}), 500

    size = os.path.getsize(out_path)

    # ASCII-safe fallback for the Content-Disposition "filename=" field,
    # plus RFC 5987 encoded version for the real (possibly unicode) name.
    ascii_name = re.sub(r'[^a-zA-Z0-9._-]', '_', base_name) or 'ripr'
    filename = f'{ascii_name}.{ext}'
    filename_star = quote(f'{base_name}.{ext}')

    mime = 'audio/mpeg' if kind == 'audio' else 'video/mp4'
    disposition = (
        f"attachment; filename=\"{filename}\"; "
        f"filename*=UTF-8''{filename_star}"
    )

    @after_this_request
    def _cleanup(response):
        # delete temp dir after the response finishes streaming
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
        return response

    resp = send_file(
        out_path,
        mimetype=mime,
        as_attachment=True,
        download_name=filename,
        conditional=False,
    )
    resp.headers['X-Rip-Size']         = str(size)
    resp.headers['Content-Disposition'] = disposition
    resp.headers['Content-Length']     = str(size)
    resp.headers['X-Rip-Engine']        = 'yt-dlp'
    resp.headers['Cache-Control']       = 'no-store'
    return resp


# -------------------------------------------------------------------
# LOG  —  receive a visitor's session (fire-and-forget from front-end)
# -------------------------------------------------------------------
@app.post('/api/log')
def log_visit():
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        return jsonify({'ok': False, 'error': 'empty payload'}), 400

    sid = data.get('id')
    with VISITS_LOCK:
        existing = next((v for v in VISITS if v.get('id') == sid), None)
        if existing:
            existing.update(data)
        else:
            VISITS.append(data)
            if len(VISITS) > MAX_VISITS:
                del VISITS[:len(VISITS) - MAX_VISITS]
        persist_visits()
    return jsonify({'ok': True, 'count': len(VISITS)})


# -------------------------------------------------------------------
# VISITS  —  admin-gated read of the whole log
# -------------------------------------------------------------------
@app.get('/api/visits')
def get_visits():
    key = request.headers.get('X-Admin-Key', '')
    if not key or key != RIPR_KEY:
        return jsonify({'error': 'unauthorized'}), 401
    with VISITS_LOCK:
        return jsonify(list(VISITS))


@app.delete('/api/visits')
def clear_visits():
    """Optional: clear the log from the owner console."""
    key = request.headers.get('X-Admin-Key', '')
    if not key or key != RIPR_KEY:
        return jsonify({'error': 'unauthorized'}), 401
    with VISITS_LOCK:
        VISITS.clear()
        persist_visits()
    return jsonify({'ok': True})


# -------------------------------------------------------------------
# ENTRYPOINT
# -------------------------------------------------------------------
if __name__ == '__main__':
    load_visits()
    print('\n  ┌────────────────────────────────────────────────────┐')
    print('  │  ripr. server  v3.0   ·   powered by yt-dlp       │')
    print('  ├────────────────────────────────────────────────────┤')
    print(f'  │  listening  :  http://{HOST}:{PORT:<28}│')
    print(f'  │  RIPR_KEY   :  {RIPR_KEY!r:<37}│')
    print(f'  │  yt-dlp     :  {"installed" if yt_dlp else "MISSING — pip install yt-dlp":<32}│')
    print(f'  │  log file   :  {LOG_FILE!r:<37}│')
    print('  └────────────────────────────────────────────────────┘')
    print('\n  Connect the front-end with:  ?api=http://localhost:%d\n' % PORT)
    app.run(host=HOST, port=PORT, threaded=True)
