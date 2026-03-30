#!/usr/bin/env python3
"""
Task Tree Planner — local server
Serves the HTML and handles data.json + image saves + weather.

Usage:
    python server.py [port]     (default port: 8000)
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")
DATA_FILE = os.path.join(BASE_DIR, "data.json")
WEATHER_HISTORY_FILE = os.path.join(BASE_DIR, "weather_history.json")

_weather_cache = {}
CACHE_TTL = 3600  # 1 hour


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
    0:  ("☀️",  "Clear sky"),
    1:  ("🌤️", "Mainly clear"),
    2:  ("⛅",  "Partly cloudy"),
    3:  ("☁️",  "Overcast"),
    45: ("🌫️", "Fog"),
    48: ("🌫️", "Icy fog"),
    51: ("🌦️", "Light drizzle"),
    53: ("🌦️", "Drizzle"),
    55: ("🌧️", "Heavy drizzle"),
    61: ("🌧️", "Light rain"),
    63: ("🌧️", "Rain"),
    65: ("🌧️", "Heavy rain"),
    71: ("🌨️", "Light snow"),
    73: ("❄️",  "Snow"),
    75: ("❄️",  "Heavy snow"),
    77: ("🌨️", "Snow grains"),
    80: ("🌦️", "Light showers"),
    81: ("🌧️", "Showers"),
    82: ("⛈️",  "Heavy showers"),
    85: ("🌨️", "Snow showers"),
    86: ("❄️",  "Heavy snow showers"),
    95: ("⛈️",  "Thunderstorm"),
    96: ("⛈️",  "Thunderstorm + hail"),
    99: ("⛈️",  "Thunderstorm + heavy hail"),
}


def fetch_weather(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&timezone=auto&forecast_days=14"
    )
    raw = fetch_url(url)
    daily    = raw.get("daily", {})
    dates    = daily.get("time", [])
    codes    = daily.get("weathercode", [])
    temp_max = daily.get("temperature_2m_max", [])
    temp_min = daily.get("temperature_2m_min", [])
    precip   = daily.get("precipitation_sum", [])

    result = []
    for i, date in enumerate(dates):
        code = codes[i] if i < len(codes) else 0
        emoji, label = WMO_CODES.get(code, ("🌡️", f"Code {code}"))
        result.append({
            "date":     date,
            "code":     code,
            "emoji":    emoji,
            "label":    label,
            "temp_max": round(temp_max[i], 1) if i < len(temp_max) and temp_max[i] is not None else None,
            "temp_min": round(temp_min[i], 1) if i < len(temp_min) and temp_min[i] is not None else None,
            "precip":   round(precip[i],   1) if i < len(precip)   and precip[i]   is not None else None,
        })
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
    history = load_weather_history()
    today = time.strftime("%Y-%m-%d")
    for day in forecast:
        d = day["date"]
        if d <= today:
            history[d] = {k: v for k, v in day.items() if k != "date"}
    save_weather_history(history)


def parse_multipart(data: bytes, boundary: bytes):
    parts = {}
    delimiter = b"--" + boundary
    for part in data.split(delimiter):
        if not part or part in (b"", b"--\r\n", b"--"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        headers_raw, body = part.split(b"\r\n\r\n", 1)
        body = body.rstrip(b"\r\n")
        headers = headers_raw.decode(errors="replace")
        name, filename = None, None
        for line in headers.splitlines():
            if "Content-Disposition" in line:
                for token in line.split(";"):
                    token = token.strip()
                    if token.startswith('name="'):
                        name = token[6:-1]
                    elif token.startswith('filename="'):
                        filename = token[10:-1]
        if name:
            parts[name] = (filename, body)
    return parts


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/geolocate":
            geo = geolocate_by_ip()
            if geo:
                self._json(200, {"ok": True, **geo})
            else:
                self._json(500, {"ok": False, "error": "Geolocation failed"})
            return

        if path == "/weather":
            lat = qs.get("lat", [None])[0]
            lon = qs.get("lon", [None])[0]
            if not lat or not lon:
                self._json(400, {"ok": False, "error": "lat/lon required"})
                return
            try:
                lat, lon = float(lat), float(lon)
            except ValueError:
                self._json(400, {"ok": False, "error": "Invalid lat/lon"})
                return

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
                        self._json(500, {"ok": False, "error": str(e)})
                        return
            else:
                age = int(now - _weather_cache["fetched_at"])
                print(f"  [weather] Cache hit (age {age}s)")

            history = load_weather_history()
            self._json(200, {
                "ok":       True,
                "forecast": _weather_cache["forecast"],
                "history":  history,
                "cached":   cache_hit,
            })
            return

        if path == "/" or path == "/index.html":
            path = "/task_tree_planner.html"

        path     = unquote(path)
        filepath = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
        if not filepath.startswith(BASE_DIR):
            self.send_error(403)
            return
        if not os.path.isfile(filepath):
            self.send_error(404, f"Not found: {path}")
            return

        ext  = os.path.splitext(filepath)[1].lower()
        mime = {
            ".html": "text/html",
            ".json": "application/json",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif":  "image/gif",
            ".webp": "image/webp",
            ".svg":  "image/svg+xml",
        }.get(ext, "application/octet-stream")

        with open(filepath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

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
                self._json(400, {"ok": False, "error": "Missing boundary"})
                return
            boundary   = ct.split("boundary=")[-1].strip().encode()
            parts      = parse_multipart(body, boundary)
            if "file" not in parts:
                self._json(400, {"ok": False, "error": "No file field"})
                return
            filename, file_data = parts["file"]
            if not filename:
                self._json(400, {"ok": False, "error": "No filename"})
                return
            filename = os.path.basename(filename)
            os.makedirs(IMAGES_DIR, exist_ok=True)
            dest = os.path.join(IMAGES_DIR, filename)
            with open(dest, "wb") as f:
                f.write(file_data)
            self._json(200, {"ok": True, "path": f"images/{filename}"})
            return

        self.send_error(404)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.makedirs(IMAGES_DIR, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Task Tree Planner  →  http://0.0.0.0:{PORT}  (also reachable on your LAN)")
    print(f"  data.json        : {DATA_FILE}")
    print(f"  weather_history  : {WEATHER_HISTORY_FILE}")
    print(f"  images/          : {IMAGES_DIR}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
