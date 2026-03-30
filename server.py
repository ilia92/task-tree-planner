#!/usr/bin/env python3
"""
Task Tree Planner — local server with multi-user login

User management:
    python server.py --setup          # add first user (alias for --add-user)
    python server.py --add-user       # add another user
    python server.py --update-user    # change a user's password
    python server.py --remove-user    # remove a user
    python server.py --list-users     # show all usernames

Run:
    python server.py [port]           # default port: 8000
"""

import hashlib
import http.cookies
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlparse, parse_qs
from urllib.request import urlopen, Request

_MGMT_FLAGS = {"--setup", "--add-user", "--remove-user", "--list-users", "--update-user"}
PORT               = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] not in _MGMT_FLAGS else 8000
BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR         = os.path.join(BASE_DIR, "images")
DATA_FILE          = os.path.join(BASE_DIR, "data.json")
WEATHER_HISTORY_FILE = os.path.join(BASE_DIR, "weather_history.json")
CONFIG_FILE        = os.path.join(BASE_DIR, "config.json")

SESSION_TTL        = 7 * 24 * 3600
_sessions: dict    = {}


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def get_users() -> dict:
    """Return {username_lower: password_hash}. Migrates old single-user format."""
    cfg = load_config()
    if "users" in cfg:
        return cfg["users"]
    # Migrate old format
    if cfg.get("username") and cfg.get("password_hash"):
        return {cfg["username"].lower(): cfg["password_hash"]}
    return {}


def save_users(users: dict):
    cfg = load_config()
    cfg["users"] = users
    # Remove legacy keys if present
    cfg.pop("username", None)
    cfg.pop("password_hash", None)
    save_config(cfg)


def check_credentials(username: str, password: str) -> bool:
    users = get_users()
    key = username.strip().lower()
    stored_hash = users.get(key)
    if not stored_hash:
        # Still run a dummy comparison to avoid timing attacks
        secrets.compare_digest("x", "y")
        return False
    return secrets.compare_digest(stored_hash, hash_password(password))


def has_any_user() -> bool:
    return bool(get_users())


def setup_user(update: bool = False):
    """Add or update a user interactively."""
    import getpass
    action = "Update" if update else "Add"
    print(f"\n── Task Tree Planner — {action} User ──────────────────")
    users = get_users()

    username = input("Username: ").strip().lower()
    if not username:
        print("Username cannot be empty.")
        return

    if update and username not in users:
        print(f"User '{username}' not found.")
        return

    while True:
        pw  = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Confirm:  ")
        if not pw:
            print("Password cannot be empty.")
            continue
        if pw != pw2:
            print("Passwords do not match. Try again.\n")
            continue
        break

    users[username] = hash_password(pw)
    save_users(users)
    verb = "updated" if update and username in users else "added"
    print(f"✓ User '{username}' {verb}.\n")


def remove_user():
    """Remove a user interactively."""
    users = get_users()
    if not users:
        print("No users configured.")
        return
    print("\n── Task Tree Planner — Remove User ─────────────────────")
    print("Current users:", ", ".join(sorted(users)))
    username = input("Username to remove: ").strip().lower()
    if username not in users:
        print(f"User '{username}' not found.")
        return
    if len(users) == 1:
        confirm = input("This is the last user — removing it will disable auth. Continue? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return
    del users[username]
    save_users(users)
    print(f"✓ User '{username}' removed.\n")


def list_users():
    users = get_users()
    if not users:
        print("No users configured.")
    else:
        print("\nConfigured users:")
        for u in sorted(users):
            print(f"  • {u}")
        print()


def create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def validate_session(token) -> bool:
    if not token:
        return False
    expires = _sessions.get(token)
    if not expires:
        return False
    if time.time() > expires:
        del _sessions[token]
        return False
    return True


def get_session_token(headers):
    raw = headers.get("Cookie", "")
    if not raw:
        return None
    c = http.cookies.SimpleCookie()
    c.load(raw)
    m = c.get("session")
    return m.value if m else None


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Task Tree Planner — Login</title>
  <style>
    :root { --green: #10b981; --red: #ef4444; --line: #d1d5db; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f3f4f6;
      font-family: Inter, Arial, sans-serif;
    }
    .card {
      background: white;
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 6px 24px rgba(0,0,0,0.08);
      padding: 40px 36px;
      width: min(380px, 92vw);
    }
    h1 { font-size: 22px; margin-bottom: 6px; }
    .sub { font-size: 13px; color: #6b7280; margin-bottom: 28px; }
    .form-field { margin-bottom: 14px; }
    label { font-size: 13px; font-weight: 600; display: block; margin-bottom: 6px; }
    input[type=text], input[type=password] {
      width: 100%;
      padding: 11px 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      font: inherit;
      font-size: 15px;
      outline: none;
      transition: border-color 0.15s;
    }
    input[type=text]:focus, input[type=password]:focus { border-color: var(--green); }
    .error {
      margin-top: 4px;
      padding: 10px 14px;
      background: #fef2f2;
      border: 1px solid #fca5a5;
      border-radius: 10px;
      font-size: 13px;
      color: var(--red);
      display: none;
    }
    .error.show { display: block; }
    button {
      margin-top: 6px;
      width: 100%;
      padding: 12px;
      background: #111827;
      color: white;
      border: none;
      border-radius: 12px;
      font: inherit;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.15s;
    }
    button:hover { opacity: 0.88; }
    .tree-icon { font-size: 36px; margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="tree-icon">🌲</div>
    <h1>Task Tree Planner</h1>
    <p class="sub">Sign in to continue.</p>
    <div class="form-field">
      <label for="un">Username</label>
      <input type="text" id="un" autofocus placeholder="username"
             onkeydown="if(event.key==='Enter')document.getElementById('pw').focus()"/>
    </div>
    <div class="form-field">
      <label for="pw">Password</label>
      <input type="password" id="pw" placeholder="••••••••"
             onkeydown="if(event.key==='Enter')login()"/>
    </div>
    <div class="error" id="err">Incorrect username or password.</div>
    <button onclick="login()">Sign in</button>
  </div>
  <script>
    async function login() {
      const un = document.getElementById('un').value.trim();
      const pw = document.getElementById('pw').value;
      const err = document.getElementById('err');
      if (!un || !pw) { err.classList.add('show'); return; }
      err.classList.remove('show');
      const resp = await fetch('/do-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: un, password: pw })
      });
      if (resp.ok) {
        location.href = '/';
      } else {
        err.classList.add('show');
        document.getElementById('pw').value = '';
        document.getElementById('un').focus();
      }
    }
  </script>
</body>
</html>"""

_weather_cache = {}
CACHE_TTL = 3600

def fetch_url(url, timeout=8):
    req = Request(url, headers={"User-Agent": "TaskTreePlanner/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def geolocate_by_ip():
    try:
        d = fetch_url("http://ip-api.com/json/?fields=lat,lon,city,country")
        if d.get("lat"):
            return {"lat": d["lat"], "lon": d["lon"],
                    "city": d.get("city",""), "country": d.get("country","")}
    except Exception as e:
        print(f"  [geo] IP lookup failed: {e}")
    return None

WMO_CODES = {
    0:("☀️","Clear sky"),1:("🌤️","Mainly clear"),2:("⛅","Partly cloudy"),
    3:("☁️","Overcast"),45:("🌫️","Fog"),48:("🌫️","Icy fog"),
    51:("🌦️","Light drizzle"),53:("🌦️","Drizzle"),55:("🌧️","Heavy drizzle"),
    61:("🌧️","Light rain"),63:("🌧️","Rain"),65:("🌧️","Heavy rain"),
    71:("🌨️","Light snow"),73:("❄️","Snow"),75:("❄️","Heavy snow"),
    77:("🌨️","Snow grains"),80:("🌦️","Light showers"),81:("🌧️","Showers"),
    82:("⛈️","Heavy showers"),85:("🌨️","Snow showers"),
    86:("❄️","Heavy snow showers"),95:("⛈️","Thunderstorm"),
    96:("⛈️","Thunderstorm + hail"),99:("⛈️","Thunderstorm + heavy hail"),
}

def fetch_weather(lat, lon):
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum"
           f"&timezone=auto&forecast_days=14")
    raw=fetch_url(url); daily=raw.get("daily",{})
    dates=daily.get("time",[]); codes=daily.get("weathercode",[])
    temp_max=daily.get("temperature_2m_max",[]); temp_min=daily.get("temperature_2m_min",[])
    precip=daily.get("precipitation_sum",[])
    result=[]
    for i,date in enumerate(dates):
        code=codes[i] if i<len(codes) else 0
        emoji,label=WMO_CODES.get(code,("🌡️",f"Code {code}"))
        result.append({"date":date,"code":code,"emoji":emoji,"label":label,
            "temp_max":round(temp_max[i],1) if i<len(temp_max) and temp_max[i] is not None else None,
            "temp_min":round(temp_min[i],1) if i<len(temp_min) and temp_min[i] is not None else None,
            "precip":round(precip[i],1) if i<len(precip) and precip[i] is not None else None})
    return result

def load_weather_history():
    try:
        with open(WEATHER_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_weather_history(history):
    with open(WEATHER_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def merge_forecast_into_history(forecast):
    history=load_weather_history(); today=time.strftime("%Y-%m-%d")
    for day in forecast:
        d=day["date"]
        if d<=today: history[d]={k:v for k,v in day.items() if k!="date"}
    save_weather_history(history)

def parse_multipart(data: bytes, boundary: bytes):
    parts={}
    for part in data.split(b"--"+boundary):
        if not part or part in (b"",b"--\r\n",b"--"): continue
        if b"\r\n\r\n" not in part: continue
        headers_raw,body=part.split(b"\r\n\r\n",1); body=body.rstrip(b"\r\n")
        headers=headers_raw.decode(errors="replace"); name=filename=None
        for line in headers.splitlines():
            if "Content-Disposition" in line:
                for token in line.split(";"):
                    token=token.strip()
                    if token.startswith('name="'): name=token[6:-1]
                    elif token.startswith('filename="'): filename=token[10:-1]
        if name: parts[name]=(filename,body)
    return parts


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def _authed(self) -> bool:
        if not has_any_user():
            return True   # no users configured — open access
        return validate_session(get_session_token(self.headers))

    def _redirect_login(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()

    def _serve_login(self):
        body = LOGIN_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/login":
            self._serve_login()
            return

        if not self._authed():
            self._redirect_login()
            return

        if path == "/geolocate":
            geo = geolocate_by_ip()
            if geo: self._json(200, {"ok": True, **geo})
            else:   self._json(500, {"ok": False, "error": "Geolocation failed"})
            return

        if path == "/weather":
            lat = qs.get("lat", [None])[0]
            lon = qs.get("lon", [None])[0]
            if not lat or not lon:
                self._json(400, {"ok": False, "error": "lat/lon required"}); return
            try: lat, lon = float(lat), float(lon)
            except ValueError:
                self._json(400, {"ok": False, "error": "Invalid lat/lon"}); return

            global _weather_cache
            now = time.time()
            cache_hit = (
                _weather_cache.get("fetched_at", 0) + CACHE_TTL > now
                and abs(_weather_cache.get("lat", 0) - lat) < 0.1
                and abs(_weather_cache.get("lon", 0) - lon) < 0.1
            )
            if not cache_hit:
                try:
                    forecast = fetch_weather(lat, lon)
                    _weather_cache = {"fetched_at": now, "lat": lat, "lon": lon, "forecast": forecast}
                    merge_forecast_into_history(forecast)
                    print(f"  [weather] Fetched {len(forecast)} days for {lat:.2f},{lon:.2f}")
                except Exception as e:
                    print(f"  [weather] Fetch failed: {e}")
                    if "forecast" not in _weather_cache:
                        self._json(500, {"ok": False, "error": str(e)}); return
            else:
                print(f"  [weather] Cache hit (age {int(now-_weather_cache['fetched_at'])}s)")

            history = load_weather_history()
            self._json(200, {"ok": True, "forecast": _weather_cache["forecast"],
                             "history": history, "cached": cache_hit})
            return

        if path == "/" or path == "/index.html":
            path = "/task_tree_planner.html"

        path     = unquote(path)
        filepath = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
        if not filepath.startswith(BASE_DIR):
            self.send_error(403); return
        if not os.path.isfile(filepath):
            self.send_error(404, f"Not found: {path}"); return

        ext  = os.path.splitext(filepath)[1].lower()
        mime = {".html":"text/html",".json":"application/json",".png":"image/png",
                ".jpg":"image/jpeg",".jpeg":"image/jpeg",".gif":"image/gif",
                ".webp":"image/webp",".svg":"image/svg+xml"}.get(ext,"application/octet-stream")

        with open(filepath, "rb") as f: body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if self.path == "/do-login":
            try:
                payload  = json.loads(body)
                username = payload.get("username", "")
                pw       = payload.get("password", "")
            except Exception:
                self._json(400, {"ok": False, "error": "Bad request"}); return

            if check_credentials(username, pw):
                token  = create_session()
                cookie = http.cookies.SimpleCookie()
                cookie["session"] = token
                cookie["session"]["httponly"] = True
                cookie["session"]["samesite"] = "Strict"
                cookie["session"]["max-age"]  = SESSION_TTL
                cookie["session"]["path"]     = "/"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", cookie["session"].OutputString())
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                print(f"  [auth] Login successful: '{username}' from {self.address_string()}")
            else:
                print(f"  [auth] Failed login for '{username}' from {self.address_string()}")
                self._json(401, {"ok": False, "error": "Incorrect username or password"})
            return

        if not self._authed():
            self._json(401, {"ok": False, "error": "Unauthorized"}); return

        if self.path == "/save":
            try:
                parsed = json.loads(body)
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, indent=2, ensure_ascii=False)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
            return

        if self.path == "/upload-image":
            ct = self.headers.get("Content-Type", "")
            if "boundary=" not in ct:
                self._json(400, {"ok": False, "error": "Missing boundary"}); return
            boundary   = ct.split("boundary=")[-1].strip().encode()
            parts      = parse_multipart(body, boundary)
            if "file" not in parts:
                self._json(400, {"ok": False, "error": "No file field"}); return
            filename, file_data = parts["file"]
            if not filename:
                self._json(400, {"ok": False, "error": "No filename"}); return
            filename = os.path.basename(filename)
            os.makedirs(IMAGES_DIR, exist_ok=True)
            dest = os.path.join(IMAGES_DIR, filename)
            with open(dest, "wb") as f: f.write(file_data)
            self._json(200, {"ok": True, "path": f"images/{filename}"})
            return

        self.send_error(404)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--setup" in args or "--add-user" in args:
        setup_user(update=False)
        sys.exit(0)

    if "--update-user" in args:
        setup_user(update=True)
        sys.exit(0)

    if "--remove-user" in args:
        remove_user()
        sys.exit(0)

    if "--list-users" in args:
        list_users()
        sys.exit(0)

    if not has_any_user():
        print("⚠  No users configured.")
        print("   Run `python server.py --setup` to add the first user.")
        print("   Starting anyway — all requests will be allowed.\n")

    os.makedirs(IMAGES_DIR, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Task Tree Planner  ->  http://0.0.0.0:{PORT}")
    print(f"  data.json   : {DATA_FILE}")
    print(f"  config.json : {CONFIG_FILE}")
    print(f"  images/     : {IMAGES_DIR}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
