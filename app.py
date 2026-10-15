import os
import sys
import json
import uuid
import time
import socket
import threading
import webbrowser
from pathlib import Path
from flask import Flask, request, jsonify, Response, send_from_directory, render_template_string

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

app = Flask(__name__)

# Use default User Downloads folder (e.g. C:\Users\<Username>\Downloads)
DOWNLOAD_DIR = Path.home() / "Downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Global dict to track download tasks progress
TASKS = {}
TASKS_LOCK = threading.Lock()

def find_free_port(default_port=5000, max_tries=50):
    env_port = os.environ.get("PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
        
    for port in range(default_port, default_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            res = sock.connect_ex(('127.0.0.1', port))
            if res != 0:
                return port
                
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

def update_task(task_id, **kwargs):
    with TASKS_LOCK:
        if task_id in TASKS:
            TASKS[task_id].update(kwargs)

def format_bytes(bytes_num):
    if not bytes_num:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(bytes_num) < 1024.0:
            return f"{bytes_num:.1f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.1f} TB"

def format_seconds(seconds):
    if not seconds:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/info', methods=['POST'])
def get_info():
    if not yt_dlp:
        return jsonify({"error": "yt-dlp library is not installed on server."}), 500
    
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"error": "Please provide a valid YouTube URL."}), 400

    ydl_opts = {
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        if not info:
            return jsonify({"error": "Could not fetch information for this URL."}), 400

        is_playlist = info.get('_type') == 'playlist' or 'entries' in info

        if is_playlist:
            entries = []
            raw_entries = info.get('entries', [])
            for idx, entry in enumerate(raw_entries, 1):
                if not entry:
                    continue
                video_url = entry.get('url') or entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
                entries.append({
                    'index': idx,
                    'id': entry.get('id'),
                    'title': entry.get('title', f'Video #{idx}'),
                    'duration': format_seconds(entry.get('duration')),
                    'thumbnail': entry.get('thumbnails', [{}])[-1].get('url') if entry.get('thumbnails') else f"https://img.youtube.com/vi/{entry.get('id')}/hqdefault.jpg",
                    'url': video_url,
                    'uploader': entry.get('uploader', 'Unknown')
                })

            return jsonify({
                'type': 'playlist',
                'id': info.get('id'),
                'title': info.get('title', 'YouTube Playlist'),
                'uploader': info.get('uploader') or info.get('playlist_uploader') or 'YouTube Channel',
                'video_count': len(entries),
                'items': entries
            })
        else:
            duration_sec = info.get('duration', 0)
            thumbnails = info.get('thumbnails', [])
            thumb_url = thumbnails[-1].get('url') if thumbnails else f"https://img.youtube.com/vi/{info.get('id')}/hqdefault.jpg"

            return jsonify({
                'type': 'video',
                'id': info.get('id'),
                'title': info.get('title', 'YouTube Video'),
                'uploader': info.get('uploader', 'Unknown Channel'),
                'duration': format_seconds(duration_sec),
                'thumbnail': thumb_url,
                'url': info.get('webpage_url') or url
            })

    except Exception as e:
        return jsonify({"error": f"Failed to extract info: {str(e)}"}), 500

@app.route('/api/download/start', methods=['POST'])
def start_download():
    if not yt_dlp:
        return jsonify({"error": "yt-dlp library is not installed."}), 500

    data = request.json or {}
    url = data.get('url', '').strip()
    fmt_type = data.get('format', 'video').lower()
    quality = data.get('quality', 'best')

    if not url:
        return jsonify({"error": "Missing URL parameter."}), 400

    task_id = str(uuid.uuid4())
    
    with TASKS_LOCK:
        TASKS[task_id] = {
            'status': 'starting',
            'percent': 0,
            'speed': '0 B/s',
            'eta': '00:00',
            'filename': None,
            'error': None,
            'playlist_current': 0,
            'playlist_total': 0,
            'current_title': '',
            'files': []
        }

    thread = threading.Thread(target=run_yt_dlp_task, args=(task_id, url, fmt_type, quality))
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id})

def run_yt_dlp_task(task_id, url, fmt_type, quality):
    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total > 0 else 0
            
            speed = format_bytes(d.get('speed', 0)) + "/s" if d.get('speed') else "N/A"
            eta = format_seconds(d.get('eta', 0))

            filename = os.path.basename(d.get('filename', ''))
            
            info_dict = d.get('info_dict', {})
            current_title = info_dict.get('title', '')
            playlist_index = info_dict.get('playlist_index')
            playlist_count = info_dict.get('n_entries')

            update_task(task_id,
                status='downloading',
                percent=round(percent, 1),
                speed=speed,
                eta=eta,
                current_title=current_title,
                playlist_current=playlist_index or TASKS[task_id].get('playlist_current', 1),
                playlist_total=playlist_count or TASKS[task_id].get('playlist_total', 1)
            )

        elif d['status'] == 'finished':
            filename = os.path.basename(d.get('filename', ''))
            update_task(task_id, status='processing', percent=100.0, speed='Processing...')
            with TASKS_LOCK:
                if filename and filename not in TASKS[task_id]['files']:
                    TASKS[task_id]['files'].append(filename)

    out_template = str(DOWNLOAD_DIR / "%(title)s [%(id)s].%(ext)s")
    
    ydl_opts = {
        'outtmpl': out_template,
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_warnings': True,
    }

    if fmt_type == 'audio':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3' if quality == 'mp3' else ('m4a' if quality == 'm4a' else 'mp3'),
                'preferredquality': '192',
            }],
        })
    else:
        if quality == '1080p':
            ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
        elif quality == '720p':
            ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
        elif quality == '480p':
            ydl_opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
        else:
            ydl_opts['format'] = 'bestvideo+bestaudio/best'
        
        ydl_opts['merge_output_format'] = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            update_task(task_id, status='fetching_metadata')
            ydl.download([url])
            
        with TASKS_LOCK:
            TASKS[task_id]['status'] = 'completed'
            TASKS[task_id]['percent'] = 100.0

    except Exception as e:
        err_msg = str(e)
        if 'ffmpeg' in err_msg.lower() or 'ffprobe' in err_msg.lower():
            try:
                fallback_opts = {
                    'outtmpl': out_template,
                    'progress_hooks': [progress_hook],
                    'format': 'best[ext=mp4]/best' if fmt_type != 'audio' else 'bestaudio/best',
                    'quiet': True,
                }
                with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                    ydl_fb.download([url])
                with TASKS_LOCK:
                    TASKS[task_id]['status'] = 'completed'
                    TASKS[task_id]['percent'] = 100.0
                return
            except Exception as fb_err:
                err_msg = str(fb_err)

        update_task(task_id, status='error', error=err_msg)

@app.route('/api/progress/<task_id>')
def task_progress(task_id):
    def event_stream():
        while True:
            with TASKS_LOCK:
                task = TASKS.get(task_id)
                if not task:
                    yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                    break
                
                payload = json.dumps(task)
                yield f"data: {payload}\n\n"
                
                if task['status'] in ['completed', 'error']:
                    break

            time.sleep(0.5)

    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/api/file/<path:filename>')
def download_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>OmniStream - YouTube Video & Playlist Downloader</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
  
  <style>
    :root {
      --bg-dark: #090d16;
      --card-bg: rgba(18, 26, 43, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --accent-red: #ff2a5f;
      --accent-purple: #7928ca;
      --accent-blue: #0070f3;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --success: #10b981;
      --warning: #f59e0b;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background: var(--bg-dark);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      background-image: 
        radial-gradient(at 0% 0%, rgba(121, 40, 202, 0.25) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(255, 42, 95, 0.2) 0px, transparent 50%),
        radial-gradient(at 50% 50%, rgba(0, 112, 243, 0.15) 0px, transparent 50%);
      background-attachment: fixed;
      overflow-x: hidden;
    }

    header {
      padding: 1.5rem 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(9, 13, 22, 0.6);
      backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--card-border);
      position: sticky;
      top: 0;
      z-index: 100;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      font-family: 'Outfit', sans-serif;
      font-weight: 800;
      font-size: 1.6rem;
      letter-spacing: -0.5px;
    }

    .brand-icon {
      width: 42px;
      height: 42px;
      background: linear-gradient(135deg, var(--accent-red), var(--accent-purple));
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-size: 1.3rem;
      box-shadow: 0 4px 20px rgba(255, 42, 95, 0.4);
    }

    .brand-title {
      background: linear-gradient(135deg, #fff 30%, var(--text-muted));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .badge-pill {
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--card-border);
      padding: 0.35rem 0.85rem;
      border-radius: 50px;
      font-size: 0.8rem;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }

    .badge-dot {
      width: 8px;
      height: 8px;
      background: var(--success);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--success);
    }

    main {
      max-width: 950px;
      width: 100%;
      margin: 2.5rem auto;
      padding: 0 1.5rem;
      flex: 1;
    }

    .hero-text {
      text-align: center;
      margin-bottom: 2.5rem;
    }

    .hero-text h1 {
      font-family: 'Outfit', sans-serif;
      font-size: 2.8rem;
      font-weight: 800;
      line-height: 1.2;
      margin-bottom: 0.75rem;
      background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .hero-text p {
      color: var(--text-muted);
      font-size: 1.1rem;
    }

    .glass-card {
      background: var(--card-bg);
      backdrop-filter: blur(20px);
      border: 1px solid var(--card-border);
      border-radius: 24px;
      padding: 2rem;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
      margin-bottom: 2rem;
      transition: border-color 0.3s;
    }

    .glass-card:focus-within {
      border-color: rgba(255, 42, 95, 0.4);
    }

    .input-group {
      display: flex;
      gap: 0.75rem;
      position: relative;
    }

    .url-input-wrapper {
      position: relative;
      flex: 1;
      display: flex;
      align-items: center;
    }

    .url-input-wrapper i {
      position: absolute;
      left: 1.25rem;
      color: var(--text-muted);
      font-size: 1.2rem;
    }

    .url-input {
      width: 100%;
      padding: 1.1rem 1.25rem 1.1rem 3.2rem;
      background: rgba(9, 13, 22, 0.6);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      color: #fff;
      font-size: 1rem;
      outline: none;
      transition: all 0.3s;
      font-family: inherit;
    }

    .url-input:focus {
      border-color: var(--accent-red);
      box-shadow: 0 0 0 4px rgba(255, 42, 95, 0.15);
    }

    .btn-analyze {
      padding: 0 1.8rem;
      background: linear-gradient(135deg, var(--accent-red), var(--accent-purple));
      border: none;
      border-radius: 16px;
      color: #fff;
      font-weight: 700;
      font-size: 1rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      transition: all 0.3s;
      box-shadow: 0 4px 20px rgba(255, 42, 95, 0.3);
      white-space: nowrap;
    }

    .btn-analyze:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 25px rgba(255, 42, 95, 0.5);
    }

    .btn-analyze:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }

    .options-panel {
      margin-top: 1.5rem;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1.25rem;
      padding-top: 1.5rem;
      border-top: 1px solid var(--card-border);
    }

    @media (max-width: 640px) {
      .options-panel { grid-template-columns: 1fr; }
      .input-group { flex-direction: column; }
      .btn-analyze { width: 100%; justify-content: center; padding: 1rem; }
    }

    .option-box {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }

    .option-label {
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .segmented-control {
      display: flex;
      background: rgba(9, 13, 22, 0.6);
      padding: 0.3rem;
      border-radius: 14px;
      border: 1px solid var(--card-border);
    }

    .segment-btn {
      flex: 1;
      padding: 0.6rem;
      border: none;
      background: transparent;
      color: var(--text-muted);
      font-weight: 600;
      border-radius: 10px;
      cursor: pointer;
      transition: all 0.25s;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      font-size: 0.9rem;
    }

    .segment-btn.active {
      background: linear-gradient(135deg, rgba(255, 42, 95, 0.2), rgba(121, 40, 202, 0.3));
      color: #fff;
      border: 1px solid rgba(255, 42, 95, 0.4);
    }

    .quality-select {
      width: 100%;
      padding: 0.75rem 1rem;
      background: rgba(9, 13, 22, 0.6);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      color: #fff;
      font-size: 0.95rem;
      outline: none;
      cursor: pointer;
      font-family: inherit;
    }

    .result-container {
      display: none;
    }

    .media-card {
      display: flex;
      gap: 1.5rem;
      align-items: center;
      background: rgba(18, 26, 43, 0.5);
      border-radius: 20px;
      padding: 1.25rem;
      border: 1px solid var(--card-border);
    }

    @media (max-width: 640px) {
      .media-card { flex-direction: column; text-align: center; }
    }

    .media-thumb {
      width: 160px;
      height: 100px;
      border-radius: 12px;
      object-fit: cover;
      border: 1px solid var(--card-border);
      box-shadow: 0 8px 20px rgba(0,0,0,0.4);
    }

    .media-details {
      flex: 1;
      min-width: 0;
    }

    .media-type-tag {
      display: inline-block;
      padding: 0.25rem 0.65rem;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      margin-bottom: 0.5rem;
    }

    .tag-video { background: rgba(0, 112, 243, 0.2); color: #60a5fa; border: 1px solid rgba(0, 112, 243, 0.4); }
    .tag-playlist { background: rgba(121, 40, 202, 0.2); color: #c084fc; border: 1px solid rgba(121, 40, 202, 0.4); }

    .media-title {
      font-family: 'Outfit', sans-serif;
      font-size: 1.25rem;
      font-weight: 700;
      margin-bottom: 0.35rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .media-meta {
      color: var(--text-muted);
      font-size: 0.88rem;
      display: flex;
      gap: 1rem;
      align-items: center;
    }

    .btn-download-now {
      padding: 0.85rem 1.75rem;
      background: linear-gradient(135deg, var(--success), #059669);
      border: none;
      border-radius: 14px;
      color: #fff;
      font-weight: 700;
      font-size: 1rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      box-shadow: 0 4px 20px rgba(16, 185, 129, 0.3);
      transition: all 0.3s;
    }

    .btn-download-now:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 25px rgba(16, 185, 129, 0.5);
    }

    .playlist-list {
      margin-top: 1.5rem;
      max-height: 400px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      padding-right: 0.5rem;
    }

    .playlist-list::-webkit-scrollbar {
      width: 6px;
    }

    .playlist-list::-webkit-scrollbar-thumb {
      background: rgba(255, 255, 255, 0.15);
      border-radius: 4px;
    }

    .item-row {
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: 0.75rem 1rem;
      background: rgba(9, 13, 22, 0.4);
      border-radius: 12px;
      border: 1px solid var(--card-border);
    }

    .item-idx {
      font-weight: 700;
      color: var(--text-muted);
      width: 24px;
      text-align: center;
    }

    .item-img {
      width: 64px;
      height: 40px;
      border-radius: 6px;
      object-fit: cover;
    }

    .item-info {
      flex: 1;
      min-width: 0;
    }

    .item-title {
      font-size: 0.92rem;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .item-duration {
      font-size: 0.78rem;
      color: var(--text-muted);
    }

    .progress-card {
      display: none;
      margin-top: 1.5rem;
      background: rgba(9, 13, 22, 0.8);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      padding: 1.5rem;
    }

    .progress-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.75rem;
    }

    .progress-status {
      font-weight: 700;
      font-size: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .progress-percent {
      font-family: 'Outfit', sans-serif;
      font-weight: 800;
      font-size: 1.3rem;
      color: var(--accent-red);
    }

    .bar-bg {
      width: 100%;
      height: 12px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 50px;
      overflow: hidden;
      margin-bottom: 0.75rem;
      position: relative;
    }

    .bar-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--accent-red), var(--accent-purple));
      border-radius: 50px;
      transition: width 0.3s ease;
      box-shadow: 0 0 15px var(--accent-red);
    }

    .progress-meta {
      display: flex;
      justify-content: space-between;
      font-size: 0.85rem;
      color: var(--text-muted);
    }

    .downloaded-files {
      margin-top: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }

    .file-link-btn {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.75rem 1rem;
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid rgba(16, 185, 129, 0.4);
      border-radius: 12px;
      color: #fff;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.9rem;
      transition: all 0.25s;
    }

    .file-link-btn:hover {
      background: rgba(16, 185, 129, 0.25);
    }

    .spinner {
      animation: spin 1s linear infinite;
    }

    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }

    .error-box {
      display: none;
      margin-top: 1rem;
      padding: 1rem;
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.4);
      border-radius: 12px;
      color: #fca5a5;
      font-size: 0.9rem;
    }
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <div class="brand-icon">
        <i class="fa-solid fa-play"></i>
      </div>
      <span class="brand-title">OmniStream</span>
    </div>
    <div class="badge-pill">
      <div class="badge-dot"></div>
      Server Online
    </div>
  </header>

  <main>
    <div class="hero-text">
      <h1>Download YouTube Videos & Playlists</h1>
      <p>Instant high-speed video and audio extraction in MP4, MP3 & M4A formats</p>
    </div>

    <div class="glass-card">
      <div class="input-group">
        <div class="url-input-wrapper">
          <i class="fa-brands fa-youtube"></i>
          <input type="text" id="urlInput" class="url-input" placeholder="Paste YouTube video or playlist link..." />
        </div>
        <button id="btnAnalyze" class="btn-analyze" onclick="fetchInfo()">
          <i class="fa-solid fa-wand-magic-sparkles"></i> Fetch Info
        </button>
      </div>

      <div class="options-panel">
        <div class="option-box">
          <label class="option-label">Download Format</label>
          <div class="segmented-control">
            <button class="segment-btn active" id="btnFmtVideo" onclick="setFormat('video')">
              <i class="fa-solid fa-video"></i> Video (MP4)
            </button>
            <button class="segment-btn" id="btnFmtAudio" onclick="setFormat('audio')">
              <i class="fa-solid fa-music"></i> Audio Only
            </button>
          </div>
        </div>

        <div class="option-box">
          <label class="option-label">Quality Setting</label>
          <select id="qualitySelect" class="quality-select">
          </select>
        </div>
      </div>

      <div id="errorBox" class="error-box"></div>
    </div>

    <div id="resultContainer" class="result-container glass-card">
      <div class="media-card">
        <img id="mediaThumb" class="media-thumb" src="" alt="Thumbnail" />
        <div class="media-details">
          <span id="mediaTag" class="media-type-tag tag-video">Single Video</span>
          <div id="mediaTitle" class="media-title">Video Title</div>
          <div class="media-meta">
            <span id="mediaUploader"><i class="fa-regular fa-user"></i> Channel</span>
            <span id="mediaDuration"><i class="fa-regular fa-clock"></i> 00:00</span>
          </div>
        </div>
        <button class="btn-download-now" onclick="startDownload()">
          <i class="fa-solid fa-download"></i> Start Download
        </button>
      </div>

      <div id="playlistList" class="playlist-list" style="display: none;"></div>
    </div>

    <div id="progressCard" class="progress-card">
      <div class="progress-header">
        <div class="progress-status" id="progressStatus">
          <i class="fa-solid fa-circle-notch spinner"></i> Starting download...
        </div>
        <div class="progress-percent" id="progressPercent">0%</div>
      </div>
      <div class="bar-bg">
        <div class="bar-fill" id="barFill"></div>
      </div>
      <div class="progress-meta">
        <span id="progressSpeed"><i class="fa-solid fa-gauge-high"></i> Speed: 0 B/s</span>
        <span id="progressTitle" style="max-width: 50%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"></span>
        <span id="progressEta"><i class="fa-regular fa-hourglass-half"></i> ETA: --:--</span>
      </div>

      <div id="downloadedFiles" class="downloaded-files"></div>
    </div>
  </main>

  <script>
    let currentFormat = 'video';
    let mediaData = null;
    let eventSource = null;

    const videoQualities = [
      { id: 'best', name: 'Best Available Quality (1080p+)' },
      { id: '1080p', name: 'Full HD (1080p)' },
      { id: '720p', name: 'HD (720p)' },
      { id: '480p', name: 'SD (480p)' }
    ];

    const audioQualities = [
      { id: 'mp3', name: 'MP3 Audio (Highest Quality)' },
      { id: 'm4a', name: 'M4A Audio (AAC)' }
    ];

    function populateQualityOptions() {
      const select = document.getElementById('qualitySelect');
      select.innerHTML = '';
      const list = currentFormat === 'video' ? videoQualities : audioQualities;
      list.forEach(q => {
        const opt = document.createElement('option');
        opt.value = q.id;
        opt.textContent = q.name;
        select.appendChild(opt);
      });
    }
    populateQualityOptions();

    function setFormat(fmt) {
      currentFormat = fmt;
      document.getElementById('btnFmtVideo').classList.toggle('active', fmt === 'video');
      document.getElementById('btnFmtAudio').classList.toggle('active', fmt === 'audio');
      populateQualityOptions();
    }

    function showError(msg) {
      const box = document.getElementById('errorBox');
      if (msg) {
        box.textContent = msg;
        box.style.display = 'block';
      } else {
        box.style.display = 'none';
      }
    }

    async function fetchInfo() {
      const url = document.getElementById('urlInput').value.trim();
      if (!url) {
        showError('Please paste a YouTube URL first.');
        return;
      }

      showError(null);
      const btn = document.getElementById('btnAnalyze');
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-circle-notch spinner"></i> Fetching...';

      document.getElementById('resultContainer').style.display = 'none';

      try {
        const res = await fetch('/api/info', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();

        if (data.error) {
          showError(data.error);
          return;
        }

        mediaData = data;
        displayMediaResult(data);

      } catch (err) {
        showError('Network error while connecting to server.');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> Fetch Info';
      }
    }

    function displayMediaResult(data) {
      const container = document.getElementById('resultContainer');
      const tag = document.getElementById('mediaTag');
      const playlistList = document.getElementById('playlistList');

      document.getElementById('mediaThumb').src = data.thumbnail;
      document.getElementById('mediaTitle').textContent = data.title;
      document.getElementById('mediaUploader').innerHTML = `<i class="fa-regular fa-user"></i> ${data.uploader}`;

      if (data.type === 'playlist') {
        tag.className = 'media-type-tag tag-playlist';
        tag.textContent = `Playlist (${data.video_count} Videos)`;
        document.getElementById('mediaDuration').innerHTML = `<i class="fa-solid fa-list-check"></i> ${data.video_count} Items`;

        playlistList.innerHTML = '';
        data.items.forEach(item => {
          const row = document.createElement('div');
          row.className = 'item-row';
          row.innerHTML = `
            <div class="item-idx">#${item.index}</div>
            <img class="item-img" src="${item.thumbnail}" alt="" />
            <div class="item-info">
              <div class="item-title">${item.title}</div>
              <div class="item-duration">${item.duration} • ${item.uploader}</div>
            </div>
          `;
          playlistList.appendChild(row);
        });
        playlistList.style.display = 'flex';
      } else {
        tag.className = 'media-type-tag tag-video';
        tag.textContent = 'Single Video';
        document.getElementById('mediaDuration').innerHTML = `<i class="fa-regular fa-clock"></i> ${data.duration}`;
        playlistList.style.display = 'none';
      }

      container.style.display = 'block';
    }

    async function startDownload() {
      if (!mediaData) return;

      const url = document.getElementById('urlInput').value.trim();
      const quality = document.getElementById('qualitySelect').value;

      const progressCard = document.getElementById('progressCard');
      progressCard.style.display = 'block';
      document.getElementById('downloadedFiles').innerHTML = '';
      document.getElementById('barFill').style.width = '0%';
      document.getElementById('progressPercent').textContent = '0%';

      try {
        const res = await fetch('/api/download/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: url,
            format: currentFormat,
            quality: quality
          })
        });

        const data = await res.json();
        if (data.error) {
          showError(data.error);
          return;
        }

        listenToProgress(data.task_id);

      } catch (err) {
        showError('Failed to start download.');
      }
    }

    function listenToProgress(taskId) {
      if (eventSource) {
        eventSource.close();
      }

      eventSource = new EventSource(`/api/progress/${taskId}`);

      eventSource.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.error) {
          document.getElementById('progressStatus').innerHTML = `<i class="fa-solid fa-triangle-exclamation" style="color: #ef4444;"></i> Error: ${data.error}`;
          eventSource.close();
          return;
        }

        const percent = data.percent || 0;
        document.getElementById('barFill').style.width = `${percent}%`;
        document.getElementById('progressPercent').textContent = `${percent}%`;

        document.getElementById('progressSpeed').innerHTML = `<i class="fa-solid fa-gauge-high"></i> Speed: ${data.speed || 'N/A'}`;
        document.getElementById('progressEta').innerHTML = `<i class="fa-regular fa-hourglass-half"></i> ETA: ${data.eta || '--:--'}`;

        if (data.playlist_total > 1) {
          document.getElementById('progressTitle').textContent = `[${data.playlist_current}/${data.playlist_total}] ${data.current_title}`;
        } else if (data.current_title) {
          document.getElementById('progressTitle').textContent = data.current_title;
        }

        if (data.status === 'starting' || data.status === 'fetching_metadata') {
          document.getElementById('progressStatus').innerHTML = `<i class="fa-solid fa-circle-notch spinner"></i> Fetching video stream...`;
        } else if (data.status === 'downloading') {
          document.getElementById('progressStatus').innerHTML = `<i class="fa-solid fa-cloud-arrow-down" style="color: var(--accent-blue);"></i> Downloading...`;
        } else if (data.status === 'processing') {
          document.getElementById('progressStatus').innerHTML = `<i class="fa-solid fa-gears spinner" style="color: var(--warning);"></i> Post-processing audio/video...`;
        } else if (data.status === 'completed') {
          document.getElementById('progressStatus').innerHTML = `<i class="fa-solid fa-circle-check" style="color: var(--success);"></i> Download Complete! saved to your Downloads folder.`;
          document.getElementById('barFill').style.width = '100%';
          document.getElementById('progressPercent').textContent = '100%';
          
          eventSource.close();
          renderDownloadFiles(data.files);
        }
      };

      eventSource.onerror = () => {
        eventSource.close();
      };
    }

    function renderDownloadFiles(files) {
      const container = document.getElementById('downloadedFiles');
      container.innerHTML = '';
      if (!files || files.length === 0) return;

      files.forEach(file => {
        const a = document.createElement('a');
        a.className = 'file-link-btn';
        a.href = `/api/file/${encodeURIComponent(file)}`;
        a.download = file;
        a.innerHTML = `
          <span><i class="fa-solid fa-file-arrow-down"></i> ${file}</span>
          <i class="fa-solid fa-download"></i>
        `;
        container.appendChild(a);
      });
    }
  </script>
</body>
</html>
"""

def open_browser(port):
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:{port}")

if __name__ == '__main__':
    port = find_free_port(5000)
    url = f"http://127.0.0.1:{port}"
    
    print("==========================================================")
    print(" OmniStream - YouTube Video & Playlist Downloader App")
    print(f" Server starting on: {url}")
    print(f" Downloads Folder: {DOWNLOAD_DIR}")
    print("==========================================================")
    
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    app.run(host='0.0.0.0', port=port, debug=False)
