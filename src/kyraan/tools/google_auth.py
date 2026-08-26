"""Shared Google OAuth token refresh — one refresh token in .env covers
every granted scope (calendar.events, gmail.readonly). Plain stdlib POST;
no Google SDK in the service. The one-time consent ceremony is
scripts/setup_google_oauth.py."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from kyraan.tools.registry import ToolError, TransientToolError

_TOKEN_URL = "https://oauth2.googleapis.com/token"

# One inbox check was doing SIX refresh round-trips (external review P2):
# cache the short-lived access token until 60s before expiry.
import threading
import time as _time

_token_lock = threading.Lock()
_cached_token: str | None = None
_cached_until: float = 0.0


def access_token() -> str:
    global _cached_token, _cached_until
    with _token_lock:
        if _cached_token and _time.monotonic() < _cached_until:
            return _cached_token
        token, expires_in = _refresh()
        _cached_token = token
        _cached_until = _time.monotonic() + max(expires_in - 60, 30)
        return token


def _refresh() -> tuple:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and refresh_token):
        raise ToolError(
            "Google OAuth isn't set up — run `python scripts/setup_google_oauth.py` once "
            "(it walks through the GCP credential steps and stores the refresh token in .env)"
        )
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(_TOKEN_URL, data=body), timeout=8) as resp:
            payload = json.loads(resp.read())
            return payload["access_token"], int(payload.get("expires_in", 3600))
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise TransientToolError(f"Google token endpoint returned {exc.code}") from exc
        raise ToolError(
            f"Google refused the OAuth refresh ({exc.code}) — the refresh token may be revoked; "
            "re-run scripts/setup_google_oauth.py"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Google OAuth: {exc}") from exc
