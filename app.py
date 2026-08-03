"""
DEVELOP — an AI image & video generator powered by the OpenRouter API.

Single-file Flask app. Runs fine under Pydroid3.

Setup:
    pip install flask requests

Run:
    python app.py
    -> open http://127.0.0.1:5000 in a browser (or Pydroid3's built-in browser)

The first time you open the app, click the key icon top-right and paste your
OpenRouter API key (https://openrouter.ai/keys). It's saved locally to
or_config.json next to this script — never sent anywhere but openrouter.ai.

What it talks to:
    Images  -> POST https://openrouter.ai/api/v1/images        (sync, base64 back)
    Video   -> POST https://openrouter.ai/api/v1/videos        (async job, poll + download)
    Models  -> GET  https://openrouter.ai/api/v1/images/models
               GET  https://openrouter.ai/api/v1/videos/models
"""

import os
import json
import time
import uuid
import base64
import threading
from datetime import datetime

import requests
from flask import Flask, request, jsonify, send_from_directory, Response

# --------------------------------------------------------------------------
# Config / storage
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "or_config.json")
GENERATED_DIR = os.path.join(BASE_DIR, "generated")
META_FILE = os.path.join(GENERATED_DIR, "_meta.json")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

os.makedirs(GENERATED_DIR, exist_ok=True)

_lock = threading.Lock()
JOBS = {}  # in-memory video job cache: job_id -> last known poll response


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)


def get_api_key():
    return load_config().get("api_key", "").strip()


def auth_headers():
    key = get_api_key()
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost",
        "X-Title": "DEVELOP",
    }


def load_meta():
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def append_meta(entry):
    with _lock:
        items = load_meta()
        items.insert(0, entry)
        items = items[:200]
        with open(META_FILE, "w") as f:
            json.dump(items, f)


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/generated/<path:filename>")
def generated_file(filename):
    return send_from_directory(GENERATED_DIR, filename)


# ---- API key -------------------------------------------------------------

@app.route("/api/key", methods=["GET"])
def api_key_status():
    key = get_api_key()
    masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("set" if key else "")
    return jsonify({"has_key": bool(key), "masked": masked})


@app.route("/api/key", methods=["POST"])
def api_key_save():
    data = request.get_json(force=True) or {}
    key = (data.get("api_key") or "").strip()
    if not key:
        return jsonify({"error": "Empty key"}), 400
    cfg = load_config()
    cfg["api_key"] = key
    save_config(cfg)
    return jsonify({"ok": True})


# ---- Model discovery -------------------------------------------------------

@app.route("/api/models/<kind>", methods=["GET"])
def list_models(kind):
    if not get_api_key():
        return jsonify({"error": "No API key set"}), 401

    endpoint = "images/models" if kind == "image" else "videos/models"
    try:
        r = requests.get(f"{OPENROUTER_BASE}/{endpoint}", headers=auth_headers(), timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    trimmed = []
    for m in data:
        trimmed.append({
            "id": m.get("id"),
            "name": m.get("name", m.get("id")),
            "aspect_ratios": m.get("supported_aspect_ratios", []),
            "resolutions": m.get("supported_resolutions", []),
        })
    return jsonify({"data": trimmed})


# ---- Image generation ------------------------------------------------------

@app.route("/api/generate/image", methods=["POST"])
def generate_image():
    if not get_api_key():
        return jsonify({"error": "No API key set"}), 401

    body = request.get_json(force=True) or {}
    payload = {"model": body.get("model"), "prompt": body.get("prompt", "")}
    if body.get("aspect_ratio"):
        payload["aspect_ratio"] = body["aspect_ratio"]
    if body.get("resolution"):
        payload["resolution"] = body["resolution"]
    if body.get("n"):
        payload["n"] = int(body["n"])

    if not payload["model"] or not payload["prompt"].strip():
        return jsonify({"error": "Model and prompt are required"}), 400

    try:
        r = requests.post(f"{OPENROUTER_BASE}/images", headers=auth_headers(),
                           json=payload, timeout=120)
        if r.status_code >= 400:
            return jsonify({"error": _extract_error(r)}), r.status_code
        result = r.json()
    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    urls = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for i, img in enumerate(result.get("data", [])):
        b64 = img.get("b64_json")
        if not b64:
            continue
        fname = f"img_{stamp}_{uuid.uuid4().hex[:6]}_{i}.png"
        path = os.path.join(GENERATED_DIR, fname)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        urls.append(f"/generated/{fname}")

    entry = {
        "type": "image",
        "model": payload["model"],
        "prompt": payload["prompt"],
        "urls": urls,
        "cost": result.get("usage", {}).get("cost"),
        "created": datetime.now().isoformat(),
    }
    append_meta(entry)
    return jsonify(entry)


# ---- Video generation -------------------------------------------------------

@app.route("/api/generate/video", methods=["POST"])
def generate_video():
    if not get_api_key():
        return jsonify({"error": "No API key set"}), 401

    body = request.get_json(force=True) or {}
    payload = {"model": body.get("model"), "prompt": body.get("prompt", "")}
    for key in ("aspect_ratio", "resolution", "duration"):
        if body.get(key):
            payload[key] = body[key]

    if not payload["model"] or not payload["prompt"].strip():
        return jsonify({"error": "Model and prompt are required"}), 400

    try:
        r = requests.post(f"{OPENROUTER_BASE}/videos", headers=auth_headers(),
                           json=payload, timeout=30)
        if r.status_code >= 400:
            return jsonify({"error": _extract_error(r)}), r.status_code
        result = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    job_id = result.get("id")
    JOBS[job_id] = {
        "status": result.get("status", "pending"),
        "model": payload["model"],
        "prompt": payload["prompt"],
        "local_url": None,
    }
    return jsonify({"job_id": job_id, "status": result.get("status", "pending")})


@app.route("/api/video/status/<job_id>", methods=["GET"])
def video_status(job_id):
    if not get_api_key():
        return jsonify({"error": "No API key set"}), 401

    cached = JOBS.get(job_id, {})
    if cached.get("local_url"):
        return jsonify({"status": "completed", "url": cached["local_url"]})

    try:
        r = requests.get(f"{OPENROUTER_BASE}/videos/{job_id}", headers=auth_headers(), timeout=20)
        if r.status_code >= 400:
            return jsonify({"error": _extract_error(r)}), r.status_code
        status = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    state = status.get("status")

    if state == "completed":
        content_urls = status.get("unsigned_urls", [])
        local_url = None
        if content_urls:
            try:
                vid = requests.get(content_urls[0], headers=auth_headers(), timeout=180)
                vid.raise_for_status()
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"vid_{stamp}_{uuid.uuid4().hex[:6]}.mp4"
                path = os.path.join(GENERATED_DIR, fname)
                with open(path, "wb") as f:
                    f.write(vid.content)
                local_url = f"/generated/{fname}"
            except Exception as e:
                return jsonify({"error": f"Downloaded job failed: {e}"}), 502

        entry = {
            "type": "video",
            "model": cached.get("model"),
            "prompt": cached.get("prompt"),
            "urls": [local_url] if local_url else [],
            "cost": status.get("usage", {}).get("cost"),
            "created": datetime.now().isoformat(),
        }
        append_meta(entry)
        JOBS[job_id] = {**cached, "status": "completed", "local_url": local_url}
        return jsonify({"status": "completed", "url": local_url})

    if state == "failed":
        JOBS[job_id] = {**cached, "status": "failed"}
        return jsonify({"status": "failed", "error": status.get("error", "Generation failed")})

    JOBS[job_id] = {**cached, "status": state}
    return jsonify({"status": state})


# ---- Gallery ---------------------------------------------------------------

@app.route("/api/gallery", methods=["GET"])
def gallery():
    return jsonify({"items": load_meta()})


def _extract_error(resp):
    try:
        data = resp.json()
        return data.get("error", {}).get("message") or data.get("error") or resp.text[:300]
    except Exception:
        return resp.text[:300] or f"HTTP {resp.status_code}"


# --------------------------------------------------------------------------
# Frontend (single page, inline CSS/JS)
# --------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DEVELOP — AI Media Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg: #0d0c0a;
    --panel: #17140f;
    --panel-2: #1e1a13;
    --line: #2c261c;
    --text: #ede6da;
    --muted: #8a8275;
    --amber: #ff7a33;
    --amber-dim: #6b3a1c;
    --cyan: #4fd1c5;
    --cyan-dim: #1c4a46;
    --danger: #e0553f;
  }
  *{box-sizing:border-box;}
  body{margin:0;}
  html,body{
    background: radial-gradient(1200px 600px at 15% -10%, #1a140c 0%, var(--bg) 55%);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
  }
  .mono{font-family:'JetBrains Mono', monospace;}
  ::selection{ background: var(--amber-dim); }

  header{
    display:flex; align-items:center; justify-content:space-between;
    padding: 20px clamp(16px, 4vw, 48px);
    border-bottom: 1px solid var(--line);
    position: sticky; top:0; z-index: 20;
    background: rgba(13,12,10,0.9); backdrop-filter: blur(10px);
  }
  .brand{ display:flex; align-items:center; gap:12px; }
  .brand .dot{
    width:10px; height:10px; border-radius:50%;
    background: var(--amber); box-shadow: 0 0 12px 2px var(--amber);
  }
  .brand h1{
    font-family:'Space Grotesk', sans-serif; font-size: 19px; font-weight:700;
    letter-spacing: 0.06em; margin:0; text-transform: uppercase;
  }
  .brand span{ color: var(--muted); font-size:11px; letter-spacing:0.1em; font-family:'JetBrains Mono',monospace; }

  .keybtn{
    display:flex; align-items:center; gap:8px;
    background: var(--panel); border:1px solid var(--line); color:var(--muted);
    padding: 8px 14px; border-radius: 999px; cursor:pointer; font-size:12px;
    font-family:'JetBrains Mono',monospace;
  }
  .keybtn.set{ color: var(--amber); border-color: var(--amber-dim); }

  main{ max-width: 1100px; margin: 0 auto; padding: clamp(20px,4vw,48px); }

  .tabs{ display:flex; gap:2px; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:4px; width:fit-content; margin-bottom:28px;}
  .tab{
    padding:9px 22px; border-radius:9px; cursor:pointer; font-family:'JetBrains Mono',monospace;
    font-size:13px; letter-spacing:0.04em; color:var(--muted); user-select:none; text-transform:uppercase;
  }
  .tab.active[data-tab="image"]{ background: var(--amber-dim); color: var(--amber); }
  .tab.active[data-tab="video"]{ background: var(--cyan-dim); color: var(--cyan); }

  .panel{
    background: var(--panel); border:1px solid var(--line); border-radius:16px;
    padding: clamp(18px,3vw,28px); margin-bottom: 28px;
  }
  label{ display:block; font-size:11px; text-transform:uppercase; letter-spacing:0.1em; color:var(--muted); margin-bottom:8px; font-family:'JetBrains Mono',monospace;}
  textarea, select, input[type=text], input[type=password]{
    width:100%; background: var(--panel-2); border:1px solid var(--line); color:var(--text);
    border-radius:10px; padding:12px 14px; font-family:'Inter',sans-serif; font-size:14px;
    outline:none;
  }
  textarea{ min-height: 92px; resize: vertical; line-height:1.5; }
  textarea:focus, select:focus, input:focus{ border-color: var(--amber-dim); }
  .video-mode textarea:focus, .video-mode select:focus{ border-color: var(--cyan-dim); }

  .row{ display:grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap:14px; margin-top:16px;}
  .field{ margin-bottom: 0; }

  .genbtn{
    margin-top:20px; width:100%; padding:14px; border-radius:12px; border:none; cursor:pointer;
    font-family:'Space Grotesk',sans-serif; font-weight:600; font-size:15px; letter-spacing:0.03em;
    background: linear-gradient(135deg, var(--amber), #c9531c); color:#180d05;
    transition: opacity .15s;
  }
  .video-mode .genbtn{ background: linear-gradient(135deg, var(--cyan), #1f8f83); color:#04211e;}
  .genbtn:disabled{ opacity:.5; cursor:not-allowed; }
  .genbtn:not(:disabled):hover{ opacity:.9; }

  .status-line{
    margin-top:14px; font-family:'JetBrains Mono',monospace; font-size:12px; color:var(--muted);
    display:none; align-items:center; gap:10px;
  }
  .status-line.show{ display:flex; }
  .scan{
    width:80px; height:4px; background:var(--line); border-radius:2px; overflow:hidden; position:relative;
  }
  .scan::after{
    content:''; position:absolute; top:0; left:-40%; width:40%; height:100%;
    background: var(--amber); animation: scan 1.1s linear infinite;
  }
  .video-mode .scan::after{ background: var(--cyan); }
  @keyframes scan{ 0%{left:-40%;} 100%{left:100%;} }

  .error-box{
    margin-top:14px; padding:10px 14px; border-radius:10px; background: rgba(224,85,63,0.1);
    border:1px solid rgba(224,85,63,0.4); color:#f2a190; font-size:13px; display:none;
  }
  .error-box.show{ display:block; }

  .gallery-head{ display:flex; align-items:baseline; justify-content:space-between; margin: 8px 0 16px;}
  .gallery-head h2{ font-family:'Space Grotesk',sans-serif; font-size:14px; text-transform:uppercase; letter-spacing:0.1em; color:var(--muted); margin:0; font-weight:600;}
  .grid{ display:grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap:16px; }
  .card{
    background: var(--panel); border:1px solid var(--line); border-radius:14px; overflow:hidden;
  }
  .card .media{ width:100%; aspect-ratio: 1/1; object-fit:cover; background:#000; display:block; }
  .card video.media{ aspect-ratio: 16/9; }
  .card .meta{ padding:12px 14px; }
  .card .prompt{ font-size:12.5px; line-height:1.4; color:var(--text); margin:0 0 6px; max-height:56px; overflow:hidden;}
  .card .tag{ font-family:'JetBrains Mono',monospace; font-size:10.5px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em;}
  .empty{ color:var(--muted); font-size:13px; font-family:'JetBrains Mono',monospace; padding: 30px 0; text-align:center; border:1px dashed var(--line); border-radius:14px;}

  .modal-bg{
    position:fixed; inset:0; background:rgba(0,0,0,0.6); display:none; align-items:center; justify-content:center; z-index:50; padding:20px;
  }
  .modal-bg.show{ display:flex; }
  .modal{ background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:26px; width:100%; max-width:420px;}
  .modal h3{ font-family:'Space Grotesk',sans-serif; margin:0 0 6px; font-size:16px;}
  .modal p{ color:var(--muted); font-size:12.5px; line-height:1.5; margin:0 0 16px;}
  .modal .actions{ display:flex; gap:10px; margin-top:16px; }
  .modal button{ flex:1; padding:11px; border-radius:9px; border:none; cursor:pointer; font-family:'JetBrains Mono',monospace; font-size:12.5px;}
  .modal .save{ background: var(--amber); color:#180d05; font-weight:600;}
  .modal .cancel{ background: var(--panel-2); color:var(--muted); }

  footer{ text-align:center; color:var(--muted); font-size:11px; padding: 20px; font-family:'JetBrains Mono',monospace;}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="dot"></div>
    <div>
      <h1>Develop</h1>
      <span>image &amp; video · openrouter</span>
    </div>
  </div>
  <button class="keybtn" id="keyBtn">⚿ <span id="keyLabel">Set API key</span></button>
</header>

<main>
  <div class="tabs">
    <div class="tab active" data-tab="image" onclick="switchTab('image')">Image</div>
    <div class="tab" data-tab="video" onclick="switchTab('video')">Video</div>
  </div>

  <div class="panel" id="formPanel">
    <label>Model</label>
    <select id="modelSelect"></select>

    <div style="margin-top:16px;">
      <label>Prompt</label>
      <textarea id="promptInput" placeholder="Describe what you want to see move or appear…"></textarea>
    </div>

    <div class="row" id="optionsRow"></div>

    <button class="genbtn" id="genBtn" onclick="generate()">Generate</button>
    <div class="status-line" id="statusLine"><div class="scan"></div><span id="statusText">Working…</span></div>
    <div class="error-box" id="errorBox"></div>
  </div>

  <div class="gallery-head">
    <h2>Gallery</h2>
    <span class="mono" style="font-size:11px;color:var(--muted);" id="galleryCount"></span>
  </div>
  <div id="galleryGrid" class="grid"></div>
</main>

<footer>local console · your OpenRouter key never leaves this machine except to openrouter.ai</footer>

<div class="modal-bg" id="keyModal">
  <div class="modal">
    <h3>OpenRouter API key</h3>
    <p>Paste a key from openrouter.ai/keys. It's stored in <span class="mono">or_config.json</span> next to app.py.</p>
    <input type="password" id="keyInput" placeholder="sk-or-v1-…">
    <div class="actions">
      <button class="cancel" onclick="closeModal()">Cancel</button>
      <button class="save" onclick="saveKey()">Save</button>
    </div>
  </div>
</div>

<script>
let currentTab = 'image';
let modelsCache = { image: [], video: [] };

const ASPECELCT = ['1:1','16:9','9:16','4:3','3:4','3:2','2:3','21:9'];
const IMG_RES = ['1K','2K','4K'];
const VID_RES = ['480p','720p','1080p'];
const VID_DUR = [4,5,6,8,10];

async function refreshKeyStatus(){
  const r = await fetch('/api/key');
  const d = await r.json();
  const btn = document.getElementById('keyBtn');
  const label = document.getElementById('keyLabel');
  if(d.has_key){ btn.classList.add('set'); label.textContent = d.masked; }
  else { btn.classList.remove('set'); label.textContent = 'Set API key'; }
  return d.has_key;
}

document.getElementById('keyBtn').onclick = () => {
  document.getElementById('keyModal').classList.add('show');
};
function closeModal(){ document.getElementById('keyModal').classList.remove('show'); }
async function saveKey(){
  const val = document.getElementById('keyInput').value.trim();
  if(!val) return;
  await fetch('/api/key', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({api_key: val})});
  document.getElementById('keyInput').value = '';
  closeModal();
  await refreshKeyStatus();
  loadModels(currentTab);
}

function switchTab(tab){
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.getElementById('formPanel').classList.toggle('video-mode', tab === 'video');
  renderOptions();
  loadModels(tab);
  hideError();
}

function renderOptions(){
  const row = document.getElementById('optionsRow');
  row.innerHTML = '';
  row.appendChild(buildSelect('aspectSelect', 'Aspect ratio', ASPECELCT));
  if(currentTab === 'image'){
    row.appendChild(buildSelect('resSelect', 'Resolution', IMG_RES));
    row.appendChild(buildSelect('nSelect', 'Count', ['1','2','3','4']));
  } else {
    row.appendChild(buildSelect('resSelect', 'Resolution', VID_RES));
    row.appendChild(buildSelect('durSelect', 'Duration (s)', VID_DUR.map(String)));
  }
}

function buildSelect(id, labelText, options){
  const wrap = document.createElement('div');
  wrap.className = 'field';
  const label = document.createElement('label');
  label.textContent = labelText;
  const sel = document.createElement('select');
  sel.id = id;
  options.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o;
    sel.appendChild(opt);
  });
  wrap.appendChild(label); wrap.appendChild(sel);
  return wrap;
}

async function loadModels(tab){
  const select = document.getElementById('modelSelect');
  select.innerHTML = '<option>Loading models…</option>';
  const hasKey = await refreshKeyStatus();
  if(!hasKey){
    select.innerHTML = '<option>Set your API key first</option>';
    return;
  }
  try{
    const r = await fetch(`/api/models/${tab}`);
    const d = await r.json();
    if(d.error){ select.innerHTML = `<option>${d.error}</option>`; return; }
    modelsCache[tab] = d.data;
    select.innerHTML = '';
    d.data.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id; opt.textContent = m.name;
      select.appendChild(opt);
    });
  } catch(e){
    select.innerHTML = '<option>Could not load models</option>';
  }
}

function showError(msg){
  const box = document.getElementById('errorBox');
  box.textContent = msg;
  box.classList.add('show');
}
function hideError(){ document.getElementById('errorBox').classList.remove('show'); }

async function generate(){
  hideError();
  const model = document.getElementById('modelSelect').value;
  const prompt = document.getElementById('promptInput').value.trim();
  if(!model || !prompt){ showError('Pick a model and write a prompt.'); return; }

  const btn = document.getElementById('genBtn');
  const statusLine = document.getElementById('statusLine');
  const statusText = document.getElementById('statusText');
  btn.disabled = true;
  statusLine.classList.add('show');

  const aspect = document.getElementById('aspectSelect').value;
  const res = document.getElementById('resSelect').value;

  try{
    if(currentTab === 'image'){
      statusText.textContent = 'Rendering image…';
      const n = document.getElementById('nSelect').value;
      const r = await fetch('/api/generate/image', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model, prompt, aspect_ratio: aspect, resolution: res, n})
      });
      const d = await r.json();
      if(!r.ok){ showError(d.error || 'Generation failed'); }
      else { loadGallery(); }
    } else {
      const dur = document.getElementById('durSelect').value;
      statusText.textContent = 'Submitting job…';
      const r = await fetch('/api/generate/video', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model, prompt, aspect_ratio: aspect, resolution: res, duration: dur})
      });
      const d = await r.json();
      if(!r.ok){ showError(d.error || 'Generation failed'); }
      else { await pollVideo(d.job_id, statusText); loadGallery(); }
    }
  } catch(e){
    showError(String(e));
  } finally {
    btn.disabled = false;
    statusLine.classList.remove('show');
  }
}

function pollVideo(jobId, statusText){
  return new Promise((resolve) => {
    const iv = setInterval(async () => {
      try{
        const r = await fetch(`/api/video/status/${jobId}`);
        const d = await r.json();
        if(d.error){ clearInterval(iv); showError(d.error); resolve(); return; }
        statusText.textContent = `Video: ${d.status}…`;
        if(d.status === 'completed'){ clearInterval(iv); resolve(); }
        if(d.status === 'failed'){ clearInterval(iv); showError(d.error || 'Video generation failed'); resolve(); }
      } catch(e){ clearInterval(iv); showError(String(e)); resolve(); }
    }, 4000);
  });
}

async function loadGallery(){
  const r = await fetch('/api/gallery');
  const d = await r.json();
  const grid = document.getElementById('galleryGrid');
  const count = document.getElementById('galleryCount');
  count.textContent = d.items.length ? `${d.items.length} generation${d.items.length===1?'':'s'}` : '';
  if(!d.items.length){
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1;">Nothing generated yet. Write a prompt above to begin.</div>';
    return;
  }
  grid.innerHTML = '';
  d.items.forEach(item => {
    if(!item.urls || !item.urls.length || !item.urls[0]) return;
    const card = document.createElement('div');
    card.className = 'card';
    const media = item.type === 'image'
      ? `<img class="media" src="${item.urls[0]}" loading="lazy">`
      : `<video class="media" src="${item.urls[0]}" controls muted></video>`;
    card.innerHTML = `
      ${media}
      <div class="meta">
        <p class="prompt">${escapeHtml(item.prompt || '')}</p>
        <span class="tag">${item.type} · ${escapeHtml((item.model||'').split('/').pop() || '')}</span>
      </div>`;
    grid.appendChild(card);
  });
}

function escapeHtml(s){
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// init
renderOptions();
refreshKeyStatus().then(hasKey => { if(hasKey) loadModels(currentTab); else document.getElementById('modelSelect').innerHTML = '<option>Set your API key first</option>'; });
loadGallery();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("DEVELOP running at http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
