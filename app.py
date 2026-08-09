# -*- coding: utf-8 -*-
"""
PRISM // Render Engine
A single-file Flask app: OpenRouter-powered image & video generation studio
with tiered subscriptions (Free / Pro $19.99 / Enterprise $29.99), a 3D UI,
and a full admin control room.

Run:
    pip install flask requests
    python app.py
Then open http://127.0.0.1:5000

First account you register becomes the admin automatically.
Set your OpenRouter API key from Admin -> Settings before generating.
"""

import os
import io
import json
import time
import uuid
import base64
import threading
import hmac
import hashlib
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    send_from_directory, render_template_string, flash
)
from markupsafe import Markup
from werkzeug.security import generate_password_hash, check_password_hash

# --------------------------------------------------------------------------
# Paths / app setup
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MEDIA_DIR = os.path.join(BASE_DIR, "static", "generated")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, "users.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
TIERS_FILE = os.path.join(DATA_DIR, "tiers.json")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions.json")
GENS_FILE = os.path.join(DATA_DIR, "generations.json")
WEBHOOK_LOG_FILE = os.path.join(DATA_DIR, "webhook_log.json")
CLAIMS_FILE = os.path.join(DATA_DIR, "claims.json")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "prism-dev-secret-change-me")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_lock = threading.Lock()

PRICING_VERSION = 2  # bump this whenever intentionally repricing tiers in DEFAULT_TIERS

DEFAULT_SETTINGS = {
    "site_name": "PRISM",
    "openrouter_api_key": "",
    "image_model": "google/gemini-2.5-flash-image",
    "video_model": "google/veo-3.1-fast",
    "maintenance_mode": False,
    "payment_webhook_secret": "",
    "pay_link_pro": "",
    "pay_link_enterprise": "",
    "orange_money_number": "",
    "orange_money_name": "",
    "pricing_version": PRICING_VERSION,
}

DEFAULT_TIERS = {
    "free": {"label": "Free", "price": 0.0, "image_quota": 8, "video_seconds_quota": 0},
    "pro": {"label": "Pro", "price": 29.99, "image_quota": 200, "video_seconds_quota": 50},
    "enterprise": {"label": "Enterprise", "price": 49.99, "image_quota": 350, "video_seconds_quota": 85},
}

QUOTA_PERIOD_DAYS = 30


# --------------------------------------------------------------------------
# JSON flat-file storage helpers
# --------------------------------------------------------------------------
def _load(path, default):
    if not os.path.exists(path):
        _save(path, default)
        return json.loads(json.dumps(default))
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return json.loads(json.dumps(default))


def _save(path, data):
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)


def load_users():
    return _load(USERS_FILE, [])


def save_users(users):
    _save(USERS_FILE, users)


def load_settings():
    s = _load(SETTINGS_FILE, DEFAULT_SETTINGS)
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    return s


def save_settings(s):
    _save(SETTINGS_FILE, s)


def load_tiers():
    t = _load(TIERS_FILE, DEFAULT_TIERS)
    changed = False
    for k, v in DEFAULT_TIERS.items():
        if k not in t:
            t[k] = v
            changed = True
            continue
        if "video_seconds_quota" not in t[k]:
            # Legacy migration: this tier predates the seconds-based video quota.
            t[k]["image_quota"] = v["image_quota"]
            t[k]["video_seconds_quota"] = v["video_seconds_quota"]
            t[k].pop("video_quota", None)
            changed = True

    # Versioned repricing: settings.json (read raw, not via load_settings(), so a
    # truly-missing key isn't masked by in-memory defaulting) tracks which pricing
    # generation is currently applied. Bumping PRICING_VERSION + DEFAULT_TIERS
    # above makes every deployment pick up the new price/quota numbers exactly
    # once, without clobbering any other admin-configured settings.
    raw_settings = _load(SETTINGS_FILE, DEFAULT_SETTINGS)
    applied_version = raw_settings.get("pricing_version", 1)
    if applied_version < PRICING_VERSION:
        for k, v in DEFAULT_TIERS.items():
            t[k]["price"] = v["price"]
            t[k]["image_quota"] = v["image_quota"]
            t[k]["video_seconds_quota"] = v["video_seconds_quota"]
            t[k]["label"] = v["label"]
        changed = True
        for k, v in DEFAULT_SETTINGS.items():
            raw_settings.setdefault(k, v)
        raw_settings["pricing_version"] = PRICING_VERSION
        _save(SETTINGS_FILE, raw_settings)

    if changed:
        save_tiers(t)
    return t


def save_tiers(t):
    _save(TIERS_FILE, t)


def load_subs():
    return _load(SUBS_FILE, [])


def save_subs(s):
    _save(SUBS_FILE, s)


def load_gens():
    return _load(GENS_FILE, [])


def save_gens(g):
    _save(GENS_FILE, g)


def load_claims():
    return _load(CLAIMS_FILE, [])


def save_claims(c):
    _save(CLAIMS_FILE, c)


def find_user(user_id):
    for u in load_users():
        if u["id"] == user_id:
            return u
    return None


def find_user_by_email(email):
    for u in load_users():
        if u["email"].lower() == email.lower():
            return u
    return None


def find_user_by_payment_ref(payment_ref):
    if not payment_ref:
        return None
    payment_ref = payment_ref.strip().upper()
    for u in load_users():
        if u.get("payment_ref") == payment_ref:
            return u
    return None


def update_user(user_id, **fields):
    users = load_users()
    for u in users:
        if u["id"] == user_id:
            u.update(fields)
            break
    save_users(users)


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return find_user(uid)


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or not u.get("is_admin"):
            return redirect(url_for("index"))
        return fn(*a, **kw)
    return wrapper


def ensure_quota_period(user):
    """Roll the user's quota window if 30 days have elapsed. Returns fresh user dict."""
    start = user.get("quota_period_start")
    reset = False
    if not start:
        reset = True
    else:
        try:
            start_dt = datetime.fromisoformat(start)
        except ValueError:
            reset = True
        else:
            if datetime.utcnow() - start_dt > timedelta(days=QUOTA_PERIOD_DAYS):
                reset = True
    if reset:
        update_user(user["id"], quota_period_start=datetime.utcnow().isoformat(),
                    images_used=0, video_seconds_used=0)
        return find_user(user["id"])
    if "video_seconds_used" not in user:
        # Migrating from the old per-clip video_quota scheme: start the seconds
        # counter fresh rather than guessing past usage.
        update_user(user["id"], video_seconds_used=0)
        return find_user(user["id"])
    return user


def tier_for(user):
    tiers = load_tiers()
    return tiers.get(user.get("tier", "free"), tiers["free"])


def ensure_payment_ref(user):
    """Backfill a payment_ref for accounts created before this field existed."""
    if user.get("payment_ref"):
        return user
    ref = uuid.uuid4().hex[:8].upper()
    update_user(user["id"], payment_ref=ref)
    return find_user(user["id"])


# --------------------------------------------------------------------------
# Bootstrap default data
# --------------------------------------------------------------------------
load_settings()
load_tiers()
load_users()
load_subs()
load_gens()


# --------------------------------------------------------------------------
# Design system shell (CSS + nav + 3D prism background)
# --------------------------------------------------------------------------
SHELL = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} :: {{ settings.site_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;600;800;900&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<style>
:root{
  --bg:#0B0B14; --surface:#15151F; --surface2:#1C1C2A; --line:#2A2A3B;
  --violet:#7C5CFF; --cyan:#33E6CC; --magenta:#FF3D7F; --gold:#FFB020;
  --text:#F2F0FA; --muted:#8E8AA3;
  --grad: linear-gradient(115deg,var(--violet),var(--cyan) 55%,var(--magenta));
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;}
h1,h2,h3,.display{font-family:'Unbounded',sans-serif;letter-spacing:-0.01em;}
.mono{font-family:'JetBrains Mono',monospace;}
a{color:inherit;text-decoration:none;}
::selection{background:var(--violet);color:#fff;}
#prism-bg{position:fixed;inset:0;z-index:0;opacity:.55;pointer-events:none;}
.wrap{position:relative;z-index:1;}
.container{max-width:1180px;margin:0 auto;padding:0 28px;}

/* nav */
nav{display:flex;align-items:center;justify-content:space-between;padding:20px 28px;position:relative;z-index:10;border-bottom:1px solid var(--line);backdrop-filter:blur(6px);}
.brand{display:flex;align-items:center;gap:10px;font-family:'Unbounded',sans-serif;font-weight:800;font-size:19px;}
.brand .swatch{width:16px;height:16px;background:var(--grad);clip-path:polygon(50% 0,100% 38%,82% 100%,18% 100%,0 38%);}
.navlinks{display:flex;gap:26px;align-items:center;font-size:14px;color:var(--muted);}
.navlinks a{transition:color .15s;}
.navlinks a:hover,.navlinks a.active{color:var(--text);}
.pill{padding:9px 18px;border-radius:3px;font-size:13px;font-weight:600;border:1px solid var(--line);}
.pill.solid{background:var(--violet);border-color:var(--violet);color:#fff;}
.pill.solid:hover{background:#8f6bff;}
.pill:hover{border-color:var(--muted);}
.tierbadge{font-size:11px;padding:3px 9px;border-radius:2px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;}
.tierbadge.free{background:var(--surface2);color:var(--muted);}
.tierbadge.pro{background:rgba(124,92,255,.15);color:var(--violet);}
.tierbadge.enterprise{background:rgba(255,176,32,.15);color:var(--gold);}

/* facets */
.facet{clip-path:polygon(18px 0,100% 0,100% calc(100% - 18px),calc(100% - 18px) 100%,0 100%,0 18px);}
.card{background:var(--surface);border:1px solid var(--line);padding:26px;}
.card:hover{border-color:#3a3a52;}

/* flash */
.flash{max-width:1180px;margin:16px auto 0;padding:0 28px;}
.flash .msg{background:var(--surface2);border:1px solid var(--line);border-left:3px solid var(--violet);padding:12px 16px;font-size:14px;border-radius:2px;margin-bottom:8px;}

footer{border-top:1px solid var(--line);padding:26px 28px;color:var(--muted);font-size:13px;display:flex;justify-content:space-between;position:relative;z-index:1;}

/* forms */
input,select,textarea{background:var(--surface2);border:1px solid var(--line);color:var(--text);padding:11px 13px;font-family:'Inter',sans-serif;font-size:14px;border-radius:2px;width:100%;}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--violet);}
label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:6px;}
button{cursor:pointer;font-family:'Inter',sans-serif;}
.btn{display:inline-block;padding:13px 26px;background:var(--violet);color:#fff;border:none;font-weight:600;font-size:14px;border-radius:2px;transition:background .15s;}
.btn:hover{background:#8f6bff;}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--text);}
.btn.ghost:hover{border-color:var(--muted);}
.btn.danger{background:var(--magenta);}
.btn.danger:hover{background:#ff5c8f;}
.btn:disabled{opacity:.4;cursor:not-allowed;}
table{width:100%;border-collapse:collapse;font-size:14px;}
th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:10px 12px;border-bottom:1px solid var(--line);}
td{padding:12px;border-bottom:1px solid var(--line);}
tr:hover td{background:rgba(255,255,255,.02);}
.bar{height:5px;background:var(--surface2);border-radius:3px;overflow:hidden;}
.bar > div{height:100%;background:var(--grad);}

/* ---- mobile only: keep every row of buttons inside the viewport, no side-scroll ---- */
@media (max-width:768px){
  nav{flex-wrap:wrap;gap:12px;padding:16px;}
  .navlinks{width:100%;flex-wrap:wrap;gap:10px 14px;font-size:13px;}
  .navlinks a,.navlinks .pill{white-space:nowrap;}
  .pill{padding:8px 14px;}

  .container{padding:0 16px;}
  section{padding-left:0;padding-right:0;}

  h1.display{font-size:36px !important;}
  h2.display{font-size:28px !important;}

  div[style*="grid-template-columns:repeat(3,1fr)"]{grid-template-columns:1fr !important;}
  div[style*="grid-template-columns:1fr 1fr"]{grid-template-columns:1fr !important;}
  div[style*="grid-template-columns:repeat(4,1fr)"]{grid-template-columns:1fr 1fr !important;}
  div[style*="grid-template-columns:repeat(auto-fill"]{grid-template-columns:1fr 1fr !important;}

  /* any inline flex row of buttons: wrap and let each button size to content, never overflow */
  div[style*="display:flex"],td[style*="display:flex"]{flex-wrap:wrap;}
  .btn,.pill,button{max-width:100%;}
  section > div[style*="display:flex"]{width:100%;}

  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;}
  td form{display:flex;flex-wrap:wrap;gap:6px;}
}
</style>
</head>
<body>
<canvas id="prism-bg"></canvas>
<div class="wrap">
<nav>
  <a class="brand" href="{{ url_for('index') }}"><span class="swatch"></span>{{ settings.site_name }}</a>
  <div class="navlinks">
    <a href="{{ url_for('index') }}" class="{{ 'active' if active=='home' }}">Studio</a>
    <a href="{{ url_for('upgrade') }}" class="{{ 'active' if active=='pricing' }}">Pricing</a>
    {% if user %}
      <a href="{{ url_for('studio') }}" class="{{ 'active' if active=='studio' }}">Generate</a>
      <a href="{{ url_for('gallery') }}" class="{{ 'active' if active=='gallery' }}">Gallery</a>
      {% if user.is_admin %}<a href="{{ url_for('admin') }}" class="{{ 'active' if active=='admin' }}">Admin</a>{% endif %}
      <span class="tierbadge {{ user.tier }}">{{ user.tier }}</span>
      <a href="{{ url_for('logout') }}" class="pill">Log out</a>
    {% else %}
      <a href="{{ url_for('login') }}" class="pill">Log in</a>
      <a href="{{ url_for('register') }}" class="pill solid">Sign up</a>
    {% endif %}
  </div>
</nav>
{% with msgs = get_flashed_messages() %}
{% if msgs %}<div class="flash">{% for m in msgs %}<div class="msg">{{ m }}</div>{% endfor %}</div>{% endif %}
{% endwith %}
{{ body }}
<footer>
  <span>{{ settings.site_name }} &mdash; render engine</span>
  {% if user and user.is_admin %}<span class="mono">OpenRouter proxy &middot; {{ 'live' if settings.openrouter_api_key else 'no api key set' }}</span>{% endif %}
</footer>
</div>
<script>
// ambient rotating prism, drawn once behind everything
(function(){
  var canvas = document.getElementById('prism-bg');
  var renderer = new THREE.WebGLRenderer({canvas:canvas, alpha:true, antialias:true});
  function size(){renderer.setSize(window.innerWidth, window.innerHeight); renderer.setPixelRatio(Math.min(devicePixelRatio,2));}
  size(); window.addEventListener('resize', size);
  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(45, window.innerWidth/window.innerHeight, 0.1, 100);
  camera.position.set(0,0,9);
  var geo = new THREE.IcosahedronGeometry(2.6, 0);
  var wire = new THREE.EdgesGeometry(geo);
  var colors = [0x7C5CFF, 0x33E6CC, 0xFF3D7F];
  var group = new THREE.Group();
  for(var i=0;i<3;i++){
    var mat = new THREE.LineBasicMaterial({color: colors[i], transparent:true, opacity:0.35});
    var mesh = new THREE.LineSegments(wire, mat);
    mesh.scale.setScalar(1 + i*0.14);
    mesh.rotation.set(i*0.4, i*0.6, 0);
    group.add(mesh);
  }
  group.position.set(3.4, 1.2, 0);
  scene.add(group);
  function animate(){
    requestAnimationFrame(animate);
    group.rotation.y += 0.0022;
    group.rotation.x += 0.0009;
    renderer.render(scene, camera);
  }
  animate();
})();
</script>
</body>
</html>
"""


def render_page(inner_tpl, title, active="", **ctx):
    body_html = render_template_string(inner_tpl, **ctx)
    return render_template_string(
        SHELL, body=Markup(body_html), title=title, active=active,
        user=current_user(), settings=load_settings()
    )


# --------------------------------------------------------------------------
# OpenRouter helpers
# --------------------------------------------------------------------------
def openrouter_headers():
    settings = load_settings()
    return {
        "Authorization": "Bearer " + settings["openrouter_api_key"],
        "Content-Type": "application/json",
        "HTTP-Referer": "https://prism.local",
        "X-Title": settings["site_name"],
    }


def generate_image_via_openrouter(prompt, aspect_ratio="1:1"):
    settings = load_settings()
    if not settings["openrouter_api_key"]:
        raise RuntimeError("OpenRouter API key is not configured. Ask an admin to set it.")
    payload = {
        "model": settings["image_model"],
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(OPENROUTER_BASE + "/chat/completions",
                          headers=openrouter_headers(), json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError("OpenRouter error ({}): {}".format(resp.status_code, resp.text[:300]))
    data = resp.json()
    message = data["choices"][0]["message"]
    images = message.get("images") or []
    if not images:
        raise RuntimeError("Model returned no image. Try a different prompt or model.")
    data_url = images[0]["image_url"]["url"]
    header, b64 = data_url.split(",", 1)
    ext = "png" if "png" in header else "jpg"
    raw = base64.b64decode(b64)
    fname = "img_{}.{}".format(uuid.uuid4().hex, ext)
    with open(os.path.join(MEDIA_DIR, fname), "wb") as f:
        f.write(raw)
    return fname


def start_video_via_openrouter(prompt, aspect_ratio="16:9", duration=5):
    settings = load_settings()
    if not settings["openrouter_api_key"]:
        raise RuntimeError("OpenRouter API key is not configured. Ask an admin to set it.")
    payload = {
        "model": settings["video_model"],
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "duration": duration,
    }
    resp = requests.post(OPENROUTER_BASE + "/videos",
                          headers=openrouter_headers(), json=payload, timeout=60)
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError("OpenRouter error ({}): {}".format(resp.status_code, resp.text[:300]))
    data = resp.json()
    job_id = data.get("id") or data.get("job_id")
    if not job_id:
        raise RuntimeError("OpenRouter did not return a job id for this video request.")
    return job_id


def poll_video_via_openrouter(job_id):
    """Returns ('processing', None) | ('complete', filename) | ('failed', error_str)"""
    resp = requests.get(OPENROUTER_BASE + "/videos/" + job_id,
                         headers=openrouter_headers(), timeout=60)
    if resp.status_code != 200:
        return "failed", "OpenRouter error ({}): {}".format(resp.status_code, resp.text[:300])
    data = resp.json()
    status = data.get("status", "processing")
    if status in ("processing", "queued", "pending", "running"):
        return "processing", None
    if status in ("failed", "error"):
        return "failed", data.get("error", "Video generation failed.")
    urls = data.get("unsigned_urls") or data.get("urls") or []
    video_url = data.get("video_url") or (urls[0] if urls else None)
    if not video_url:
        return "failed", "OpenRouter marked the job complete but returned no video URL."
    video_resp = requests.get(video_url, timeout=120)
    fname = "vid_{}.mp4".format(uuid.uuid4().hex)
    with open(os.path.join(MEDIA_DIR, fname), "wb") as f:
        f.write(video_resp.content)
    return "complete", fname


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------
HOME_TPL = """
<section style="padding:90px 28px 60px;">
  <div class="container" style="max-width:840px;">
    <div class="mono" style="color:var(--cyan);font-size:13px;letter-spacing:.1em;margin-bottom:18px;">OPENROUTER-POWERED &middot; IMAGE + VIDEO</div>
    <h1 class="display" style="font-size:58px;line-height:1.03;margin:0 0 22px;font-weight:900;">
      One prompt.<br>Split into <span style="background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;">every wavelength</span> of media.
    </h1>
    <p style="color:var(--muted);font-size:17px;line-height:1.6;max-width:560px;margin:0 0 34px;">
      PRISM refracts a single prompt through OpenRouter's model catalog &mdash; stills and motion,
      routed through one API key, metered by subscription tier.
    </p>
    <div style="display:flex;gap:14px;">
      {% if user %}
        <a href="{{ url_for('studio') }}" class="btn">Open the studio</a>
      {% else %}
        <a href="{{ url_for('register') }}" class="btn">Create a free account</a>
      {% endif %}
      <a href="{{ url_for('upgrade') }}" class="btn ghost">See plans</a>
    </div>
  </div>
</section>

<section class="container" style="padding-bottom:90px;">
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:18px;">
    <div class="card facet">
      <div class="mono" style="color:var(--violet);font-size:12px;">01 &middot; INPUT</div>
      <h3 style="margin:10px 0 8px;">One prompt</h3>
      <p style="color:var(--muted);font-size:14px;line-height:1.6;margin:0;">Describe the shot once. PRISM handles the request shape for whichever OpenRouter model is behind it.</p>
    </div>
    <div class="card facet">
      <div class="mono" style="color:var(--cyan);font-size:12px;">02 &middot; REFRACT</div>
      <h3 style="margin:10px 0 8px;">Image or video</h3>
      <p style="color:var(--muted);font-size:14px;line-height:1.6;margin:0;">Switch modality without switching tools. Stills return in seconds; motion renders async with live status.</p>
    </div>
    <div class="card facet">
      <div class="mono" style="color:var(--magenta);font-size:12px;">03 &middot; METER</div>
      <h3 style="margin:10px 0 8px;">Quota-aware</h3>
      <p style="color:var(--muted);font-size:14px;line-height:1.6;margin:0;">Every render checks your plan's quota first, so usage always lines up with what you're paying for.</p>
    </div>
  </div>
</section>
"""


@app.route("/")
def index():
    return render_page(HOME_TPL, title="Studio", active="home")


REGISTER_TPL = """
<section class="container" style="max-width:420px;padding:70px 28px;">
  <h2 class="display" style="margin-bottom:6px;">Create account</h2>
  <p style="color:var(--muted);font-size:14px;margin-bottom:26px;">Create an account, then upgrade to Pro or Enterprise to start generating.</p>
  <form method="post" style="display:flex;flex-direction:column;gap:16px;">
    <div><label>Email</label><input type="email" name="email" required></div>
    <div><label>Password</label><input type="password" name="password" required minlength="6"></div>
    <button class="btn" type="submit" style="margin-top:6px;">Sign up</button>
  </form>
  <p style="color:var(--muted);font-size:13px;margin-top:18px;">Already have an account? <a href="{{ url_for('login') }}" style="color:var(--cyan);">Log in</a></p>
</section>
"""


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or len(password) < 6:
            flash("Enter a valid email and a password of at least 6 characters.")
            return redirect(url_for("register"))
        if find_user_by_email(email):
            flash("An account with that email already exists.")
            return redirect(url_for("register"))
        users = load_users()
        if ADMIN_EMAIL:
            # Locked to a specific email set via the ADMIN_EMAIL env var — nobody else
            # can ever become admin through registration, even if user data resets.
            grant_admin = (email == ADMIN_EMAIL)
        else:
            # No ADMIN_EMAIL configured (local/dev use only): first account becomes admin.
            grant_admin = len(users) == 0
        user = {
            "id": uuid.uuid4().hex,
            "email": email,
            "password_hash": generate_password_hash(password),
            "tier": "free",
            "is_admin": grant_admin,
            "banned": False,
            "created_at": datetime.utcnow().isoformat(),
            "quota_period_start": datetime.utcnow().isoformat(),
            "images_used": 0,
            "video_seconds_used": 0,
            "payment_ref": uuid.uuid4().hex[:8].upper(),
        }
        users.append(user)
        save_users(users)
        session["user_id"] = user["id"]
        flash("Welcome to PRISM." + (" You're the admin." if grant_admin else ""))
        return redirect(url_for("studio"))
    return render_page(REGISTER_TPL, title="Sign up", active="")


LOGIN_TPL = """
<section class="container" style="max-width:420px;padding:70px 28px;">
  <h2 class="display" style="margin-bottom:6px;">Log in</h2>
  <p style="color:var(--muted);font-size:14px;margin-bottom:26px;">Welcome back.</p>
  <form method="post" style="display:flex;flex-direction:column;gap:16px;">
    <div><label>Email</label><input type="email" name="email" required></div>
    <div><label>Password</label><input type="password" name="password" required></div>
    <button class="btn" type="submit" style="margin-top:6px;">Log in</button>
  </form>
  <p style="color:var(--muted);font-size:13px;margin-top:18px;">No account? <a href="{{ url_for('register') }}" style="color:var(--cyan);">Sign up</a></p>
</section>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = find_user_by_email(email)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.")
            return redirect(url_for("login"))
        if user.get("banned"):
            flash("This account has been suspended. Contact an admin.")
            return redirect(url_for("login"))
        session["user_id"] = user["id"]
        return redirect(request.args.get("next") or url_for("studio"))
    return render_page(LOGIN_TPL, title="Log in", active="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Studio (generation) + gallery
# --------------------------------------------------------------------------
STUDIO_TPL = """
<section class="container" style="padding:44px 28px 80px;">
  <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:26px;flex-wrap:wrap;gap:14px;">
    <div>
      <div class="mono" style="color:var(--cyan);font-size:12px;letter-spacing:.08em;">GENERATION STUDIO</div>
      <h2 class="display" style="margin:6px 0 0;">Render something</h2>
    </div>
    {% if tier.price > 0 %}
    <div class="card facet" style="padding:14px 20px;min-width:230px;">
      <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:6px;"><span>Images this period</span><span class="mono">{{ user.images_used }}/{{ tier.image_quota }}</span></div>
      <div class="bar"><div style="width:{{ (user.images_used / tier.image_quota * 100) if tier.image_quota else 0 }}%;"></div></div>
      <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin:10px 0 6px;"><span>Video seconds this period</span><span class="mono">{{ user.video_seconds_used }}/{{ tier.video_seconds_quota }}s</span></div>
      <div class="bar"><div style="width:{{ (user.video_seconds_used / tier.video_seconds_quota * 100) if tier.video_seconds_quota else 0 }}%;"></div></div>
    </div>
    {% endif %}
  </div>

  {% if tier.price <= 0 and not user.is_admin %}
  <div class="card facet" style="max-width:560px;margin:0 auto;text-align:center;padding:50px 40px;">
    <div class="mono" style="color:var(--magenta);font-size:12px;letter-spacing:.08em;margin-bottom:14px;">PAID FEATURE</div>
    <h3 class="display" style="margin:0 0 12px;font-size:26px;">Upgrade to start generating</h3>
    <p style="color:var(--muted);font-size:14px;line-height:1.7;margin:0 0 26px;">
      Image and video generation are available on Pro and Enterprise. Pick a plan to unlock the studio.
    </p>
    <a href="{{ url_for('upgrade') }}" class="btn" style="padding:14px 32px;">See plans</a>
  </div>
  {% else %}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:22px;">
    <div class="card facet">
      <div style="display:flex;gap:8px;margin-bottom:18px;">
        <button id="modeImg" class="btn" style="flex:1;" onclick="setMode('image')">Image</button>
        <button id="modeVid" class="btn ghost" style="flex:1;" onclick="setMode('video')">Video</button>
      </div>
      <label>Prompt</label>
      <textarea id="prompt" rows="5" placeholder="A chrome prism suspended above a rain-slicked street, neon refractions, cinematic..."></textarea>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px;">
        <div><label>Aspect ratio</label>
          <select id="aspect"><option value="1:1">1:1 square</option><option value="16:9" selected>16:9 wide</option><option value="9:16">9:16 tall</option></select>
        </div>
        <div id="durationWrap" style="display:none;"><label>Duration (sec)</label>
          <select id="duration"><option>4</option><option selected>5</option><option>8</option></select>
        </div>
      </div>
      <button id="genBtn" class="btn" style="width:100%;margin-top:18px;" onclick="generate()">Generate</button>
      <div id="genStatus" class="mono" style="font-size:12px;color:var(--muted);margin-top:12px;min-height:16px;"></div>

      <div id="resultWrap" style="display:none;margin-top:20px;">
        <div class="facet" style="overflow:hidden;border:1px solid var(--line);background:var(--surface2);">
          <div id="resultMedia"></div>
        </div>
        <a id="resultDownload" href="#" download class="btn ghost" style="width:100%;text-align:center;display:block;margin-top:10px;">Download</a>
      </div>
    </div>

    <div class="card facet" style="display:flex;align-items:center;justify-content:center;min-height:420px;position:relative;overflow:hidden;">
      <canvas id="stage" style="position:absolute;inset:0;"></canvas>
      <div id="stagePlaceholder" style="color:var(--muted);font-size:13px;position:relative;z-index:2;text-align:center;padding:0 30px;">Your render appears here, mounted on a floating 3D plane.</div>
    </div>
  </div>
  {% endif %}
</section>

{% if tier.price > 0 or user.is_admin %}
<script>
var mode = 'image';
function setMode(m){
  mode = m;
  document.getElementById('modeImg').className = m==='image' ? 'btn' : 'btn ghost';
  document.getElementById('modeVid').className = m==='video' ? 'btn' : 'btn ghost';
  document.getElementById('durationWrap').style.display = m==='video' ? 'block' : 'none';
}

// 3D preview stage
var stageCanvas = document.getElementById('stage');
var stageRenderer = new THREE.WebGLRenderer({canvas:stageCanvas, alpha:true, antialias:true});
function stageSize(){
  var w = stageCanvas.parentElement.clientWidth, h = stageCanvas.parentElement.clientHeight;
  stageRenderer.setSize(w,h); stageRenderer.setPixelRatio(Math.min(devicePixelRatio,2));
  stageCamera.aspect = w/h; stageCamera.updateProjectionMatrix();
}
var stageScene = new THREE.Scene();
var stageCamera = new THREE.PerspectiveCamera(40, 1, 0.1, 50);
stageCamera.position.set(0,0,4.2);
var planeGeo = new THREE.PlaneGeometry(3,3);
var planeMat = new THREE.MeshBasicMaterial({color:0x1C1C2A, transparent:true, opacity:0.001});
var plane = new THREE.Mesh(planeGeo, planeMat);
stageScene.add(plane);
var light = new THREE.PointLight(0x7C5CFF, 1.2); light.position.set(2,2,3); stageScene.add(light);
window.addEventListener('resize', stageSize); stageSize();
var mouseX=0, mouseY=0;
stageCanvas.parentElement.addEventListener('mousemove', function(e){
  var r = stageCanvas.getBoundingClientRect();
  mouseX = ((e.clientX - r.left)/r.width - 0.5) * 2;
  mouseY = ((e.clientY - r.top)/r.height - 0.5) * 2;
});
function animateStage(){
  requestAnimationFrame(animateStage);
  plane.rotation.y += (mouseX*0.5 - plane.rotation.y) * 0.06;
  plane.rotation.x += (-mouseY*0.35 - plane.rotation.x) * 0.06;
  stageRenderer.render(stageScene, stageCamera);
}
animateStage();

function mountMedia(url, isVideo){
  document.getElementById('stagePlaceholder').style.display='none';
  var tex;
  if(isVideo){
    var vid = document.createElement('video');
    vid.src = url; vid.crossOrigin='anonymous'; vid.loop=true; vid.muted=true; vid.autoplay=true; vid.play();
    tex = new THREE.VideoTexture(vid);
  } else {
    tex = new THREE.TextureLoader().load(url);
  }
  planeMat.map = tex; planeMat.opacity = 1; planeMat.needsUpdate = true;

  // full-size result, shown right below the Generate button
  var resultWrap = document.getElementById('resultWrap');
  var resultMedia = document.getElementById('resultMedia');
  resultMedia.innerHTML = '';
  var el;
  if(isVideo){
    el = document.createElement('video');
    el.src = url; el.controls = true; el.loop = true; el.autoplay = true; el.muted = true;
  } else {
    el = document.createElement('img');
    el.src = url;
  }
  el.style.width = '100%';
  el.style.display = 'block';
  resultMedia.appendChild(el);
  document.getElementById('resultDownload').href = url;
  resultWrap.style.display = 'block';
}

function generate(){
  var prompt = document.getElementById('prompt').value.trim();
  if(!prompt){ alert('Write a prompt first.'); return; }
  var btn = document.getElementById('genBtn'); btn.disabled = true;
  var status = document.getElementById('genStatus');
  var aspect = document.getElementById('aspect').value;
  if(mode === 'image'){
    status.textContent = 'Rendering image...';
    fetch('/api/generate/image', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt: prompt, aspect_ratio: aspect})})
      .then(r => r.json()).then(d => {
        btn.disabled = false;
        if(d.error){ status.textContent = 'Error: ' + d.error; return; }
        status.textContent = 'Done.';
        mountMedia(d.url, false);
      }).catch(e => { btn.disabled=false; status.textContent='Network error.'; });
  } else {
    status.textContent = 'Queuing video job...';
    var duration = document.getElementById('duration').value;
    fetch('/api/generate/video/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt: prompt, aspect_ratio: aspect, duration: parseInt(duration)})})
      .then(r => r.json()).then(d => {
        if(d.error){ btn.disabled=false; status.textContent = 'Error: ' + d.error; return; }
        poll(d.gen_id, status, btn);
      }).catch(e => { btn.disabled=false; status.textContent='Network error.'; });
  }
}
function poll(genId, status, btn){
  status.textContent = 'Rendering video, this can take a minute...';
  var iv = setInterval(function(){
    fetch('/api/generate/video/status/' + genId).then(r=>r.json()).then(d=>{
      if(d.status === 'complete'){
        clearInterval(iv); btn.disabled=false; status.textContent='Done.';
        mountMedia(d.url, true);
      } else if(d.status === 'failed'){
        clearInterval(iv); btn.disabled=false; status.textContent = 'Error: ' + d.error;
      }
    }).catch(e=>{});
  }, 3500);
}
</script>
{% endif %}
"""


@app.route("/studio")
@login_required
def studio():
    user = ensure_quota_period(current_user())
    tier = tier_for(user)
    return render_page(STUDIO_TPL, title="Generate", active="studio", user=user, tier=tier)


GALLERY_TPL = """
<section class="container" style="padding:44px 28px 80px;">
  <div class="mono" style="color:var(--cyan);font-size:12px;letter-spacing:.08em;">YOUR RENDERS</div>
  <h2 class="display" style="margin:6px 0 26px;">Gallery</h2>
  {% if gens %}
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;">
    {% for g in gens %}
    <div class="card facet" style="padding:0;overflow:hidden;">
      {% if g.type == 'image' %}
        <img src="{{ url_for('static', filename='generated/' + g.filename) }}" style="width:100%;display:block;aspect-ratio:1;object-fit:cover;">
      {% else %}
        <video src="{{ url_for('static', filename='generated/' + g.filename) }}" style="width:100%;display:block;aspect-ratio:1;object-fit:cover;" muted loop autoplay></video>
      {% endif %}
      <div style="padding:12px;">
        <div style="font-size:12px;color:var(--muted);">{{ g.prompt[:70] }}</div>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <p style="color:var(--muted);">Nothing rendered yet. <a href="{{ url_for('studio') }}" style="color:var(--cyan);">Open the studio</a> to make your first piece.</p>
  {% endif %}
</section>
"""


@app.route("/gallery")
@login_required
def gallery():
    user = current_user()
    all_gens = [g for g in load_gens() if g["user_id"] == user["id"] and g.get("status") == "complete"]
    all_gens.sort(key=lambda g: g["created_at"], reverse=True)
    return render_page(GALLERY_TPL, title="Gallery", active="gallery", gens=all_gens)


# --------------------------------------------------------------------------
# Generation API
# --------------------------------------------------------------------------
@app.route("/api/generate/image", methods=["POST"])
@login_required
def api_generate_image():
    user = ensure_quota_period(current_user())
    settings = load_settings()
    if settings["maintenance_mode"] and not user["is_admin"]:
        return jsonify(error="PRISM is in maintenance mode. Try again shortly."), 503
    tier = tier_for(user)
    if tier["price"] <= 0 and not user["is_admin"]:
        return jsonify(error="Generating requires a paid plan. Upgrade to Pro or Enterprise to continue."), 402
    if user["images_used"] >= tier["image_quota"]:
        return jsonify(error="Image quota reached for this billing period. Upgrade your plan for more."), 403
    body = request.get_json(force=True, silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify(error="Prompt is required."), 400
    gen_id = uuid.uuid4().hex
    try:
        fname = generate_image_via_openrouter(prompt, body.get("aspect_ratio", "1:1"))
    except Exception as e:
        gens = load_gens()
        gens.append({"id": gen_id, "user_id": user["id"], "type": "image", "prompt": prompt,
                     "status": "failed", "error": str(e), "created_at": datetime.utcnow().isoformat()})
        save_gens(gens)
        return jsonify(error=str(e)), 502
    gens = load_gens()
    gens.append({"id": gen_id, "user_id": user["id"], "type": "image", "prompt": prompt,
                "status": "complete", "filename": fname, "created_at": datetime.utcnow().isoformat()})
    save_gens(gens)
    update_user(user["id"], images_used=user["images_used"] + 1)
    return jsonify(url=url_for("static", filename="generated/" + fname), gen_id=gen_id)


@app.route("/api/generate/video/start", methods=["POST"])
@login_required
def api_generate_video_start():
    user = ensure_quota_period(current_user())
    settings = load_settings()
    if settings["maintenance_mode"] and not user["is_admin"]:
        return jsonify(error="PRISM is in maintenance mode. Try again shortly."), 503
    tier = tier_for(user)
    if tier["price"] <= 0 and not user["is_admin"]:
        return jsonify(error="Generating requires a paid plan. Upgrade to Pro or Enterprise to continue."), 402
    body = request.get_json(force=True, silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify(error="Prompt is required."), 400
    try:
        duration = int(body.get("duration", 5))
    except (TypeError, ValueError):
        duration = 5
    duration = max(1, min(duration, 8))
    remaining = tier["video_seconds_quota"] - user["video_seconds_used"]
    if duration > remaining and not user["is_admin"]:
        return jsonify(error="Only {} video second(s) left this period (this clip needs {}). "
                             "Upgrade your plan or pick a shorter duration.".format(remaining, duration)), 403
    gen_id = uuid.uuid4().hex
    gens = load_gens()
    try:
        job_id = start_video_via_openrouter(prompt, body.get("aspect_ratio", "16:9"), duration)
    except Exception as e:
        gens.append({"id": gen_id, "user_id": user["id"], "type": "video", "prompt": prompt, "duration": duration,
                     "status": "failed", "error": str(e), "created_at": datetime.utcnow().isoformat()})
        save_gens(gens)
        return jsonify(error=str(e)), 502
    gens.append({"id": gen_id, "user_id": user["id"], "type": "video", "prompt": prompt, "job_id": job_id,
                "duration": duration, "status": "processing", "created_at": datetime.utcnow().isoformat()})
    save_gens(gens)
    return jsonify(gen_id=gen_id)


@app.route("/api/generate/video/status/<gen_id>")
@login_required
def api_generate_video_status(gen_id):
    user = current_user()
    gens = load_gens()
    record = next((g for g in gens if g["id"] == gen_id and g["user_id"] == user["id"]), None)
    if not record:
        return jsonify(error="Unknown job."), 404
    if record["status"] != "processing":
        if record["status"] == "complete":
            return jsonify(status="complete", url=url_for("static", filename="generated/" + record["filename"]))
        return jsonify(status="failed", error=record.get("error", "Generation failed."))
    try:
        status, result = poll_video_via_openrouter(record["job_id"])
    except Exception as e:
        status, result = "failed", str(e)
    if status == "processing":
        return jsonify(status="processing")
    for g in gens:
        if g["id"] == gen_id:
            if status == "complete":
                g["status"] = "complete"
                g["filename"] = result
            else:
                g["status"] = "failed"
                g["error"] = result
            break
    save_gens(gens)
    if status == "complete":
        update_user(user["id"], video_seconds_used=user["video_seconds_used"] + record.get("duration", 5))
        return jsonify(status="complete", url=url_for("static", filename="generated/" + result))
    return jsonify(status="failed", error=result)


# --------------------------------------------------------------------------
# Pricing / mock subscriptions
# --------------------------------------------------------------------------
PRICING_TPL = """
<section class="container" style="padding:60px 28px 90px;">
  <div class="mono" style="color:var(--cyan);font-size:12px;letter-spacing:.08em;">PLANS</div>
  <h2 class="display" style="margin:6px 0 10px;">Pick a wavelength</h2>
  <p style="color:var(--muted);max-width:520px;margin-bottom:18px;">Every plan shares the same studio. Higher tiers unlock more renders per 30-day period and priority video quota.</p>
  {% if user %}
  <div class="card facet" style="max-width:520px;margin-bottom:34px;padding:16px 20px;">
    <div style="font-size:12px;color:var(--muted);">Your payment reference &mdash; include this when you pay so we can match it to your account</div>
    <div class="display mono" style="font-size:22px;color:var(--cyan);letter-spacing:.04em;margin-top:4px;">{{ user.payment_ref }}</div>
  </div>
  {% endif %}
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:18px;">
    {% for key, t in tiers.items() %}
    <div class="card facet" style="{{ 'border-color:var(--gold);' if key=='enterprise' else '' }}{{ 'border-color:var(--violet);' if key=='pro' else '' }}">
      <div class="mono" style="font-size:12px;color:{{ 'var(--gold)' if key=='enterprise' else ('var(--violet)' if key=='pro' else 'var(--muted)') }};text-transform:uppercase;letter-spacing:.08em;">{{ t.label }}</div>
      <div style="display:flex;align-items:baseline;gap:6px;margin:14px 0 20px;">
        <span class="display" style="font-size:38px;font-weight:800;">${{ '%.2f'|format(t.price) }}</span>
        {% if t.price > 0 %}<span style="color:var(--muted);font-size:13px;">/ month</span>{% endif %}
      </div>
      <ul style="list-style:none;padding:0;margin:0 0 24px;color:var(--muted);font-size:14px;line-height:2;">
        {% if key == 'free' %}
          <li>Account &amp; gallery access</li>
          <li style="color:#5c5870;">No image or video generation</li>
        {% else %}
          <li>{{ t.image_quota }} image renders / 30 days</li>
          <li>{{ t.video_seconds_quota }} video seconds / 30 days (~{{ (t.video_seconds_quota / 5) | int }} clips @ 5s)</li>
          <li>3D studio + gallery</li>
        {% endif %}
        {% if key == 'enterprise' %}<li>Priority render queue</li>{% endif %}
      </ul>
      {% if user and user.tier == key %}
        <button class="btn ghost" style="width:100%;" disabled>Current plan</button>
      {% elif user and key == 'free' %}
        <button class="btn ghost" style="width:100%;" onclick="downgrade()">Cancel &amp; downgrade to free</button>
      {% elif user %}
        {% set link = pay_links.get(key) %}
        {% set pending = pending_claim_tier == key %}
        {% if link %}
          <a class="btn" style="width:100%;text-align:center;display:block;margin-bottom:8px;" target="_blank" rel="noopener" href="{{ link }}?ref={{ user.payment_ref }}">Pay for {{ t.label }}</a>
        {% endif %}
        {% if om_number %}
          {% if pending %}
            <button class="btn ghost" style="width:100%;" disabled>Orange Money payment pending review</button>
          {% else %}
            <button class="btn {{ 'ghost' if link else '' }}" style="width:100%;" onclick="claimOM('{{ key }}','{{ t.label }}','{{ '%.2f'|format(t.price) }}')">Pay {{ t.label }} with Orange Money</button>
          {% endif %}
        {% endif %}
        {% if not link and not om_number %}
          <button class="btn ghost" style="width:100%;" disabled>Payment not set up yet</button>
        {% endif %}
      {% else %}
        <a href="{{ url_for('register') }}" class="btn" style="width:100%;text-align:center;display:block;">Sign up</a>
      {% endif %}
    </div>
    {% endfor %}
  </div>

  {% if user and om_number %}
  <div class="card facet" style="max-width:600px;margin-top:26px;padding:22px 26px;">
    <div class="mono" style="font-size:12px;color:var(--gold);letter-spacing:.06em;margin-bottom:8px;">ORANGE MONEY</div>
    <p style="color:var(--muted);font-size:14px;line-height:1.7;margin:0;">
      Send the plan amount to <strong style="color:var(--text);">{{ om_number }}</strong>{% if om_name %} ({{ om_name }}){% endif %}
      with reference <strong class="mono" style="color:var(--cyan);">{{ user.payment_ref }}</strong> in the transfer note, then click
      "Pay with Orange Money" above. An admin confirms the transfer and your plan unlocks &mdash; usually within a few hours.
    </p>
  </div>
  {% endif %}

  {% if user %}
  <p style="color:var(--muted);font-size:13px;margin-top:22px;max-width:600px;">
    Card/link payments update your plan automatically within a minute or two. Orange Money payments are confirmed manually
    by an admin, so those can take a little longer.
  </p>
  {% endif %}
</section>
<script>
function downgrade(){
  if(!confirm('This cancels your PRISM plan and drops you to Free immediately. If you still have an active recurring payment set up with your provider, cancel it there too. Continue?')) return;
  fetch('/api/cancel-subscription', {method:'POST'})
    .then(r=>r.json()).then(d=>{ if(d.ok){ location.reload(); } else { alert(d.error||'Could not update plan.'); } });
}
function claimOM(tier, label, price){
  if(!confirm('Confirming you already sent $' + price + ' via Orange Money with your reference in the note. An admin will verify and unlock ' + label + '. Continue?')) return;
  fetch('/api/claim-payment', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({tier:tier})})
    .then(r=>r.json()).then(d=>{ if(d.ok){ location.reload(); } else { alert(d.error||'Could not submit claim.'); } });
}
</script>
"""


@app.route("/upgrade")
def upgrade():
    user = current_user()
    pending_claim_tier = None
    if user:
        user = ensure_payment_ref(user)
        claims = load_claims()
        pending = next((c for c in claims if c["user_id"] == user["id"] and c["status"] == "pending"), None)
        if pending:
            pending_claim_tier = pending["tier"]
    settings = load_settings()
    pay_links = {"pro": settings["pay_link_pro"], "enterprise": settings["pay_link_enterprise"]}
    return render_page(PRICING_TPL, title="Pricing", active="pricing", tiers=load_tiers(),
                        user=user, pay_links=pay_links, pending_claim_tier=pending_claim_tier,
                        om_number=settings["orange_money_number"], om_name=settings["orange_money_name"])


@app.route("/api/claim-payment", methods=["POST"])
@login_required
def api_claim_payment():
    user = current_user()
    body = request.get_json(force=True, silent=True) or {}
    tier = body.get("tier")
    tiers = load_tiers()
    if tier not in tiers or tiers[tier]["price"] <= 0:
        return jsonify(ok=False, error="Invalid plan."), 400
    claims = load_claims()
    if any(c["user_id"] == user["id"] and c["status"] == "pending" for c in claims):
        return jsonify(ok=False, error="You already have a payment pending review."), 400
    claims.append({
        "id": uuid.uuid4().hex, "user_id": user["id"], "email": user["email"], "tier": tier,
        "price": tiers[tier]["price"], "requested_at": datetime.utcnow().isoformat(),
        "status": "pending",
    })
    save_claims(claims)
    return jsonify(ok=True)


@app.route("/api/cancel-subscription", methods=["POST"])
@login_required
def api_cancel_subscription():
    user = current_user()
    apply_cancellation(user)
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Payment webhook (provider-agnostic)
#
# Wire this up to whatever you use to take payments: a payment link tool,
# a serverless function, PayPal IPN relayed through a small script, Zapier/
# Make.com, or a fully custom checkout you build later. PRISM doesn't care
# how the money moved — it just needs a POST to /webhook/payment once it has,
# signed with the shared secret set in Admin -> Payment webhook.
#
# Expected JSON body:
#   {
#     "event": "payment.succeeded",      # or "subscription.cancelled"
#     "tier": "pro",                     # required for payment.succeeded
#     "payment_ref": "AB12CD34",         # preferred way to identify the user
#     "email": "user@example.com",       # fallback if no payment_ref
#     "period_days": 30,                 # optional, defaults to 30
#     "external_id": "txn_123"           # optional, your own record id
#   }
#
# Signature: HMAC-SHA256 of the raw request body, hex-encoded, sent as:
#   X-Prism-Signature: sha256=<hex digest>
# --------------------------------------------------------------------------
def verify_webhook_signature(raw_body, header_value, secret):
    if not secret or not header_value:
        return False
    provided = header_value.split("=", 1)[-1].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


def load_webhook_log():
    return _load(WEBHOOK_LOG_FILE, [])


def save_webhook_log(log):
    log = log[-200:]  # keep it bounded
    _save(WEBHOOK_LOG_FILE, log)


def log_webhook(event, matched_email, ok, detail):
    log = load_webhook_log()
    log.append({
        "at": datetime.utcnow().isoformat(), "event": event or "?",
        "user": matched_email or "no match", "ok": ok, "detail": detail[:200],
    })
    save_webhook_log(log)


def apply_payment_success(user, tier, period_days=30, source="webhook", external_id=None):
    """Upgrade a user to a paid tier and record the subscription. Shared by the
    signed webhook and the manual (e.g. Orange Money) claim-approval path."""
    tiers = load_tiers()
    if tier not in tiers or tiers[tier]["price"] <= 0:
        raise ValueError("Invalid tier '{}'.".format(tier))
    update_user(user["id"], tier=tier)
    subs = load_subs()
    subs.append({
        "id": uuid.uuid4().hex, "user_id": user["id"], "tier": tier,
        "price": tiers[tier]["price"], "started_at": datetime.utcnow().isoformat(),
        "renews_at": (datetime.utcnow() + timedelta(days=period_days)).isoformat(),
        "status": "active", "source": source, "external_id": external_id,
    })
    save_subs(subs)


def apply_cancellation(user):
    update_user(user["id"], tier="free")
    subs = load_subs()
    for s in subs:
        if s["user_id"] == user["id"] and s["status"] == "active":
            s["status"] = "cancelled"
    save_subs(subs)


@app.route("/webhook/payment", methods=["POST"])
def payment_webhook():
    settings = load_settings()
    raw = request.get_data()
    sig = request.headers.get("X-Prism-Signature", "")
    if not verify_webhook_signature(raw, sig, settings["payment_webhook_secret"]):
        log_webhook(None, None, False, "Signature verification failed.")
        return jsonify(error="Invalid signature."), 401

    body = request.get_json(force=True, silent=True) or {}
    event = body.get("event")
    payment_ref = body.get("payment_ref")
    email = body.get("email")

    user = find_user_by_payment_ref(payment_ref) if payment_ref else None
    if not user and email:
        user = find_user_by_email(email)
    if not user:
        log_webhook(event, email, False, "No matching user for payment_ref/email.")
        return jsonify(error="No matching user."), 404

    tiers = load_tiers()

    if event == "payment.succeeded":
        tier = body.get("tier")
        try:
            apply_payment_success(user, tier, int(body.get("period_days", 30)),
                                   source="webhook", external_id=body.get("external_id"))
        except ValueError as e:
            log_webhook(event, user["email"], False, str(e))
            return jsonify(error=str(e)), 400
        log_webhook(event, user["email"], True, "Upgraded to {}.".format(tier))
        return jsonify(ok=True)

    if event == "subscription.cancelled":
        apply_cancellation(user)
        log_webhook(event, user["email"], True, "Downgraded to free.")
        return jsonify(ok=True)

    log_webhook(event, user["email"], False, "Unrecognized event type.")
    return jsonify(error="Unrecognized event type."), 400


# --------------------------------------------------------------------------
# Admin dashboard
# --------------------------------------------------------------------------
ADMIN_TPL = """
<section class="container" style="padding:44px 28px 90px;">
  <div class="mono" style="color:var(--magenta);font-size:12px;letter-spacing:.08em;">CONTROL ROOM</div>
  <h2 class="display" style="margin:6px 0 26px;">Admin</h2>

  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:34px;">
    <div class="card facet"><div style="color:var(--muted);font-size:12px;">Users</div><div class="display" style="font-size:28px;">{{ stats.users }}</div></div>
    <div class="card facet"><div style="color:var(--muted);font-size:12px;">Est. MRR (mock)</div><div class="display" style="font-size:28px;">${{ '%.2f'|format(stats.mrr) }}</div></div>
    <div class="card facet"><div style="color:var(--muted);font-size:12px;">Images rendered</div><div class="display" style="font-size:28px;">{{ stats.images }}</div></div>
    <div class="card facet"><div style="color:var(--muted);font-size:12px;">Videos rendered</div><div class="display" style="font-size:28px;">{{ stats.videos }}</div></div>
  </div>

  <div class="card facet" style="margin-bottom:24px;">
    <h3 style="margin-top:0;">API &amp; system settings</h3>
    <form method="post" action="{{ url_for('admin_settings') }}" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
      <input type="hidden" name="payment_webhook_secret" value="{{ settings.payment_webhook_secret }}">
      <input type="hidden" name="pay_link_pro" value="{{ settings.pay_link_pro }}">
      <input type="hidden" name="pay_link_enterprise" value="{{ settings.pay_link_enterprise }}">
      <input type="hidden" name="orange_money_number" value="{{ settings.orange_money_number }}">
      <input type="hidden" name="orange_money_name" value="{{ settings.orange_money_name }}">
      <div style="grid-column:1/-1;"><label>OpenRouter API key</label><input type="password" name="openrouter_api_key" value="{{ settings.openrouter_api_key }}" placeholder="sk-or-..."></div>
      <div><label>Image model</label><input type="text" name="image_model" value="{{ settings.image_model }}"></div>
      <div><label>Video model</label><input type="text" name="video_model" value="{{ settings.video_model }}"></div>
      <div><label>Site name</label><input type="text" name="site_name" value="{{ settings.site_name }}"></div>
      <div style="display:flex;align-items:center;gap:10px;padding-top:22px;">
        <input type="checkbox" name="maintenance_mode" style="width:auto;" {{ 'checked' if settings.maintenance_mode }}>
        <label style="margin:0;">Maintenance mode (blocks non-admin generation)</label>
      </div>
      <div style="grid-column:1/-1;"><button class="btn" type="submit">Save settings</button></div>
    </form>
  </div>

  <div class="card facet" style="margin-bottom:24px;">
    <h3 style="margin-top:0;">Payment webhook</h3>
    <p style="color:var(--muted);font-size:13px;line-height:1.6;margin-top:0;">
      Point your payment tool (a payment link service, Zapier/Make.com, PayPal IPN relay, or your own script) at the URL
      below once a payment is confirmed. Sign requests with HMAC-SHA256 of the raw body using the secret below, sent as
      header <code class="mono">X-Prism-Signature: sha256=&lt;hex&gt;</code>.
    </p>
    <div style="margin-bottom:16px;">
      <label>Webhook URL</label>
      <input type="text" readonly onclick="this.select()" value="{{ webhook_url }}">
    </div>
    <form method="post" action="{{ url_for('admin_settings') }}" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
      <input type="hidden" name="openrouter_api_key" value="{{ settings.openrouter_api_key }}">
      <input type="hidden" name="image_model" value="{{ settings.image_model }}">
      <input type="hidden" name="video_model" value="{{ settings.video_model }}">
      <input type="hidden" name="site_name" value="{{ settings.site_name }}">
      <input type="hidden" name="orange_money_number" value="{{ settings.orange_money_number }}">
      <input type="hidden" name="orange_money_name" value="{{ settings.orange_money_name }}">
      {% if settings.maintenance_mode %}<input type="hidden" name="maintenance_mode" value="on">{% endif %}
      <div style="grid-column:1/-1;"><label>Webhook secret</label><input type="text" name="payment_webhook_secret" value="{{ settings.payment_webhook_secret }}" placeholder="Paste or generate a long random string"></div>
      <div><label>Pro payment link</label><input type="text" name="pay_link_pro" value="{{ settings.pay_link_pro }}" placeholder="https://your-payment-tool.com/pro"></div>
      <div><label>Enterprise payment link</label><input type="text" name="pay_link_enterprise" value="{{ settings.pay_link_enterprise }}" placeholder="https://your-payment-tool.com/enterprise"></div>
      <div style="grid-column:1/-1;display:flex;gap:10px;">
        <button class="btn" type="submit">Save webhook settings</button>
        <button class="btn ghost" type="button" onclick="genSecret()">Generate random secret</button>
      </div>
    </form>
    <h4 style="margin:26px 0 10px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;">Recent webhook activity</h4>
    <table>
      <tr><th>When</th><th>Event</th><th>User</th><th>Result</th><th>Detail</th></tr>
      {% for w in webhook_log %}
      <tr>
        <td class="mono" style="font-size:12px;">{{ w.at[:16] }}</td>
        <td>{{ w.event or '?' }}</td>
        <td class="mono" style="font-size:12px;">{{ w.user }}</td>
        <td>{{ 'OK' if w.ok else 'Failed' }}</td>
        <td style="font-size:12px;color:var(--muted);">{{ w.detail }}</td>
      </tr>
      {% else %}
      <tr><td colspan="5" style="color:var(--muted);">No webhook calls received yet.</td></tr>
      {% endfor %}
    </table>
  </div>
  <script>
  function genSecret(){
    var arr = new Uint8Array(24);
    crypto.getRandomValues(arr);
    var hex = Array.from(arr).map(b => b.toString(16).padStart(2,'0')).join('');
    document.querySelector('input[name="payment_webhook_secret"]').value = hex;
  }
  </script>

  <div class="card facet" style="margin-bottom:24px;{{ 'border-color:var(--gold);' if pending_claims }}">
    <h3 style="margin-top:0;">Orange Money (manual)</h3>
    <p style="color:var(--muted);font-size:13px;line-height:1.6;margin-top:0;">
      No merchant API required. Users send payment to the number below with their reference in the note, then click
      "Pay with Orange Money" &mdash; it shows up here as a pending claim for you to confirm.
    </p>
    <form method="post" action="{{ url_for('admin_settings') }}" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:22px;">
      <input type="hidden" name="openrouter_api_key" value="{{ settings.openrouter_api_key }}">
      <input type="hidden" name="image_model" value="{{ settings.image_model }}">
      <input type="hidden" name="video_model" value="{{ settings.video_model }}">
      <input type="hidden" name="site_name" value="{{ settings.site_name }}">
      <input type="hidden" name="payment_webhook_secret" value="{{ settings.payment_webhook_secret }}">
      <input type="hidden" name="pay_link_pro" value="{{ settings.pay_link_pro }}">
      <input type="hidden" name="pay_link_enterprise" value="{{ settings.pay_link_enterprise }}">
      {% if settings.maintenance_mode %}<input type="hidden" name="maintenance_mode" value="on">{% endif %}
      <div><label>Orange Money number</label><input type="text" name="orange_money_number" value="{{ settings.orange_money_number }}" placeholder="+225 07 00 00 00 00"></div>
      <div><label>Account name (optional)</label><input type="text" name="orange_money_name" value="{{ settings.orange_money_name }}" placeholder="e.g. Bee K."></div>
      <div style="grid-column:1/-1;"><button class="btn" type="submit">Save Orange Money settings</button></div>
    </form>

    <h4 style="margin:0 0 10px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;">Pending claims{% if pending_claims %} ({{ pending_claims|length }}){% endif %}</h4>
    <table>
      <tr><th>Requested</th><th>User</th><th>Plan</th><th>Amount</th><th>Actions</th></tr>
      {% for c in pending_claims %}
      <tr>
        <td class="mono" style="font-size:12px;">{{ c.requested_at[:16] }}</td>
        <td class="mono" style="font-size:12px;">{{ c.email }}</td>
        <td>{{ c.tier }}</td>
        <td>${{ '%.2f'|format(c.price) }}</td>
        <td style="display:flex;gap:6px;">
          <form method="post" action="{{ url_for('admin_claim_approve', claim_id=c.id) }}"><button class="btn" style="padding:8px 12px;font-size:12px;">Approve</button></form>
          <form method="post" action="{{ url_for('admin_claim_reject', claim_id=c.id) }}"><button class="btn danger" style="padding:8px 12px;font-size:12px;">Reject</button></form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="5" style="color:var(--muted);">No pending claims.</td></tr>
      {% endfor %}
    </table>
  </div>

  <div class="card facet" style="margin-bottom:24px;">
    <h3 style="margin-top:0;">Plan pricing &amp; quotas</h3>
    <form method="post" action="{{ url_for('admin_tiers') }}">
      <table>
        <tr><th>Plan</th><th>Price / mo</th><th>Image quota / 30d</th><th>Video seconds / 30d</th></tr>
        {% for key, t in tiers.items() %}
        <tr>
          <td>{{ t.label }}</td>
          <td><input type="number" step="0.01" name="{{ key }}_price" value="{{ t.price }}" {{ 'disabled' if key=='free' }}></td>
          <td><input type="number" name="{{ key }}_image_quota" value="{{ t.image_quota }}"></td>
          <td><input type="number" name="{{ key }}_video_seconds_quota" value="{{ t.video_seconds_quota }}"></td>
        </tr>
        {% endfor %}
      </table>
      <button class="btn" type="submit" style="margin-top:16px;">Save plans</button>
    </form>
  </div>

  <div class="card facet" style="margin-bottom:24px;">
    <h3 style="margin-top:0;">Users</h3>
    <table>
      <tr><th>Email</th><th>Tier</th><th>Usage</th><th>Status</th><th>Joined</th><th>Actions</th></tr>
      {% for u in users %}
      <tr>
        <td>{{ u.email }}{% if u.is_admin %} <span class="tierbadge enterprise" style="margin-left:6px;">admin</span>{% endif %}</td>
        <td>
          <form method="post" action="{{ url_for('admin_user_update', user_id=u.id) }}" style="display:flex;gap:6px;">
            <select name="tier" style="width:auto;">
              {% for key in tiers %}<option value="{{ key }}" {{ 'selected' if u.tier==key }}>{{ tiers[key].label }}</option>{% endfor %}
            </select>
            <button class="btn ghost" type="submit" style="padding:8px 12px;font-size:12px;">Set</button>
          </form>
        </td>
        <td class="mono" style="font-size:12px;">{{ u.images_used }} img / {{ u.video_seconds_used }}s vid</td>
        <td>{{ 'Banned' if u.banned else 'Active' }}</td>
        <td class="mono" style="font-size:12px;">{{ u.created_at[:10] }}</td>
        <td style="display:flex;gap:6px;">
          <form method="post" action="{{ url_for('admin_user_ban', user_id=u.id) }}"><button class="btn ghost" style="padding:8px 12px;font-size:12px;">{{ 'Unban' if u.banned else 'Ban' }}</button></form>
          <form method="post" action="{{ url_for('admin_user_delete', user_id=u.id) }}" onsubmit="return confirm('Delete this user permanently?');"><button class="btn danger" style="padding:8px 12px;font-size:12px;">Delete</button></form>
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div class="card facet">
    <h3 style="margin-top:0;">Recent generations</h3>
    <table>
      <tr><th>User</th><th>Type</th><th>Prompt</th><th>Status</th><th>When</th></tr>
      {% for g in recent_gens %}
      <tr>
        <td class="mono" style="font-size:12px;">{{ g.email }}</td>
        <td>{{ g.type }}</td>
        <td style="max-width:320px;">{{ g.prompt[:60] }}</td>
        <td>{{ g.status }}</td>
        <td class="mono" style="font-size:12px;">{{ g.created_at[:16] }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</section>
"""


@app.route("/admin")
@admin_required
def admin():
    users = load_users()
    gens = load_gens()
    tiers = load_tiers()
    subs = [s for s in load_subs() if s["status"] == "active"]
    mrr = sum(tiers.get(s["tier"], {}).get("price", 0) for s in subs)
    stats = {
        "users": len(users),
        "mrr": mrr,
        "images": len([g for g in gens if g["type"] == "image" and g["status"] == "complete"]),
        "videos": len([g for g in gens if g["type"] == "video" and g["status"] == "complete"]),
    }
    email_by_id = {u["id"]: u["email"] for u in users}
    recent = sorted(gens, key=lambda g: g["created_at"], reverse=True)[:50]
    for g in recent:
        g["email"] = email_by_id.get(g["user_id"], "?")
    webhook_log = list(reversed(load_webhook_log()))[:30]
    webhook_url = request.url_root.rstrip("/") + url_for("payment_webhook")
    pending_claims = sorted(
        [c for c in load_claims() if c["status"] == "pending"],
        key=lambda c: c["requested_at"]
    )
    return render_page(ADMIN_TPL, title="Admin", active="admin",
                        users=users, tiers=tiers, stats=stats,
                        settings=load_settings(), recent_gens=recent,
                        webhook_log=webhook_log, webhook_url=webhook_url,
                        pending_claims=pending_claims)


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    s = load_settings()
    s["openrouter_api_key"] = request.form.get("openrouter_api_key", s["openrouter_api_key"]).strip()
    s["image_model"] = request.form.get("image_model", s["image_model"]).strip()
    s["video_model"] = request.form.get("video_model", s["video_model"]).strip()
    s["site_name"] = request.form.get("site_name", s["site_name"]).strip() or "PRISM"
    s["maintenance_mode"] = request.form.get("maintenance_mode") == "on"
    s["payment_webhook_secret"] = request.form.get("payment_webhook_secret", s["payment_webhook_secret"]).strip()
    s["pay_link_pro"] = request.form.get("pay_link_pro", s["pay_link_pro"]).strip()
    s["pay_link_enterprise"] = request.form.get("pay_link_enterprise", s["pay_link_enterprise"]).strip()
    s["orange_money_number"] = request.form.get("orange_money_number", s["orange_money_number"]).strip()
    s["orange_money_name"] = request.form.get("orange_money_name", s["orange_money_name"]).strip()
    save_settings(s)
    flash("Settings saved.")
    return redirect(url_for("admin"))


@app.route("/admin/tiers", methods=["POST"])
@admin_required
def admin_tiers():
    tiers = load_tiers()
    for key in tiers:
        if key != "free":
            price = request.form.get(key + "_price")
            if price is not None:
                tiers[key]["price"] = float(price)
        img_q = request.form.get(key + "_image_quota")
        vid_q = request.form.get(key + "_video_seconds_quota")
        if img_q is not None:
            tiers[key]["image_quota"] = int(img_q)
        if vid_q is not None:
            tiers[key]["video_seconds_quota"] = int(vid_q)
    save_tiers(tiers)
    flash("Plan pricing and quotas saved.")
    return redirect(url_for("admin"))


@app.route("/admin/claim/<claim_id>/approve", methods=["POST"])
@admin_required
def admin_claim_approve(claim_id):
    claims = load_claims()
    claim = next((c for c in claims if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        flash("That claim is no longer pending.")
        return redirect(url_for("admin"))
    user = find_user(claim["user_id"])
    if not user:
        flash("The user for that claim no longer exists.")
        return redirect(url_for("admin"))
    try:
        apply_payment_success(user, claim["tier"], 30, source="orange_money", external_id=claim["id"])
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("admin"))
    claim["status"] = "approved"
    claim["reviewed_at"] = datetime.utcnow().isoformat()
    save_claims(claims)
    log_webhook("payment.succeeded", user["email"], True,
                "Approved manually (Orange Money) -> {}.".format(claim["tier"]))
    flash("Approved. {} is now on {}.".format(user["email"], claim["tier"]))
    return redirect(url_for("admin"))


@app.route("/admin/claim/<claim_id>/reject", methods=["POST"])
@admin_required
def admin_claim_reject(claim_id):
    claims = load_claims()
    claim = next((c for c in claims if c["id"] == claim_id), None)
    if claim and claim["status"] == "pending":
        claim["status"] = "rejected"
        claim["reviewed_at"] = datetime.utcnow().isoformat()
        save_claims(claims)
        flash("Claim rejected.")
    return redirect(url_for("admin"))


@app.route("/admin/user/<user_id>/update", methods=["POST"])
@admin_required
def admin_user_update(user_id):
    tier = request.form.get("tier")
    if tier in load_tiers():
        update_user(user_id, tier=tier)
        flash("Updated plan for user.")
    return redirect(url_for("admin"))


@app.route("/admin/user/<user_id>/ban", methods=["POST"])
@admin_required
def admin_user_ban(user_id):
    u = find_user(user_id)
    if u:
        update_user(user_id, banned=not u.get("banned"))
    return redirect(url_for("admin"))


@app.route("/admin/user/<user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    admin_user = current_user()
    users = load_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return redirect(url_for("admin"))
    if target["id"] == admin_user["id"]:
        flash("You can't delete your own account while logged in as it.")
        return redirect(url_for("admin"))
    admins_left = len([u for u in users if u["is_admin"] and u["id"] != user_id])
    if target["is_admin"] and admins_left == 0:
        flash("Can't delete the last remaining admin.")
        return redirect(url_for("admin"))
    users = [u for u in users if u["id"] != user_id]
    save_users(users)
    flash("User deleted.")
    return redirect(url_for("admin"))


# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    print("PRISM running on port {}".format(port))
    print("Register the first account to become admin, then set your OpenRouter key under /admin.")
    app.run(debug=debug, host="0.0.0.0", port=port)
