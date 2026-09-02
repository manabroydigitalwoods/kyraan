"""One-time Spotify OAuth (2026-09-02). Prereqs:
1. https://developer.spotify.com/dashboard → Create app,
   redirect URI EXACTLY: http://127.0.0.1:8899/callback
2. SPOTIFY_CLIENT_ID=... and SPOTIFY_CLIENT_SECRET=... in .env
3. .venv/bin/python scripts/setup_spotify_oauth.py
Saves the refresh token to data/spotify_token.json (0600). Scopes are
playback-state read/modify only — no library or account writes.
"""
import base64
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REDIRECT = "http://127.0.0.1:8899/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"

if not CLIENT_ID or not CLIENT_SECRET:
    sys.exit("Put SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first "
             "(developer.spotify.com/dashboard → your app).")

state = secrets.token_urlsafe(16)
auth_url = ("https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
    "client_id": CLIENT_ID, "response_type": "code",
    "redirect_uri": REDIRECT, "scope": SCOPES, "state": state}))

code_holder: dict = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if params.get("state", [""])[0] != state:
            self.send_response(400); self.end_headers()
            self.wfile.write(b"state mismatch"); return
        code_holder["code"] = params.get("code", [""])[0]
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Kyraan is connected to Spotify. Close this tab.")

    def log_message(self, *a):
        pass


server = http.server.HTTPServer(("127.0.0.1", 8899), _Handler)
threading.Thread(target=server.handle_request, daemon=True).start()
print("Opening browser for Spotify consent...")
webbrowser.open(auth_url)
print(f"(If nothing opened, visit:\n{auth_url})")
while "code" not in code_holder:
    pass
server.server_close()

basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
body = urllib.parse.urlencode({
    "grant_type": "authorization_code", "code": code_holder["code"],
    "redirect_uri": REDIRECT}).encode()
request = urllib.request.Request(
    "https://accounts.spotify.com/api/token", data=body, method="POST",
    headers={"Authorization": f"Basic {basic}"})
with urllib.request.urlopen(request, timeout=15) as resp:
    tokens = json.loads(resp.read())

target = REPO / "data" / "spotify_token.json"
target.parent.mkdir(exist_ok=True)
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as handle:
    json.dump({"refresh_token": tokens["refresh_token"]}, handle)
print(f"Saved refresh token to {target} — restart Kyraan and say "
      '"play something" in Telegram.')
