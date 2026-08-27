"""One-time Google OAuth setup for calendar WRITES (reads use the secret
ICS URL and don't need this).

Before running, do these steps yourself in the Google Cloud console
(https://console.cloud.google.com) — Kyraan can't and shouldn't do them
for you:

  1. Create a project (any name, e.g. "kyraan").
  2. APIs & Services -> Library -> enable "Google Calendar API".
  3. APIs & Services -> OAuth consent screen -> External -> fill the two
     required fields -> add your own Gmail as a Test user. (Stays in
     "Testing" mode — fine for a personal app; refresh tokens in testing
     mode expire after ~7 days of NON-use, but daily Kyraan use keeps
     them alive.)
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app.
  5. Put the client id and secret into .env:
        GOOGLE_OAUTH_CLIENT_ID=...
        GOOGLE_OAUTH_CLIENT_SECRET=...

Then run:  .venv/bin/python scripts/setup_google_oauth.py

A browser opens; approve access for your own account (re-run this whenever
the scope list grows — e.g. the Gmail read-only scope added for email.unread). The refresh token is
written into .env as GOOGLE_OAUTH_REFRESH_TOKEN and the calendar.create
skill starts working (each event still asks for your yes in chat).
"""
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")  # BEFORE SCOPES: the gmail scope reads the env
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    # Default: metadata-ONLY scope — bodies are impossible at Google's
    # level, not just by our code's restraint. Setting
    # KYRAAN_EMAIL_BODIES=local in .env BEFORE running this script
    # requests gmail.readonly instead: bodies become fetchable, and the
    # runtime then processes them exclusively with the LOCAL model
    # (never a cloud tier) — the §3a boundary moves from "unreadable"
    # to "never leaves the machine", by explicit owner choice.
    ("https://www.googleapis.com/auth/gmail.readonly"
     if os.environ.get("KYRAAN_EMAIL_BODIES", "").strip() == "local"
     else "https://www.googleapis.com/auth/gmail.metadata"),
    # KYRAAN_EMAIL_DRAFTS=on (owner, 2026-08-27): Kyraan may CREATE
    # drafts — the owner sends from Gmail. Google offers no drafts-only
    # scope (compose is the narrowest and technically permits sending);
    # the enforced boundary is the codebase: NO send code path exists.
    *(["https://www.googleapis.com/auth/gmail.compose"]
      if os.environ.get("KYRAAN_EMAIL_DRAFTS", "").strip() == "on" else []),
]


def main() -> None:
    load_dotenv(REPO / ".env")
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        sys.exit(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set in .env — "
            "do the GCP console steps in this script's docstring first."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow  # dev extra

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    if not creds.refresh_token:
        sys.exit("Google returned no refresh token — re-run this script (it forces prompt=consent).")

    env_path = REPO / ".env"
    text = env_path.read_text()
    line = f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}"
    if re.search(r"^GOOGLE_OAUTH_REFRESH_TOKEN=", text, flags=re.M):
        text = re.sub(r"^GOOGLE_OAUTH_REFRESH_TOKEN=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    # 0600 BEFORE the credential lands — a crash between write and chmod
    # must never leave a readable window (security round 4)
    if env_path.exists():
        os.chmod(env_path, 0o600)
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    print("Refresh token saved to .env (permissions 0600) — restart the bot service:")
    print("  launchctl kickstart -k gui/$(id -u)/ai.kyraan")


if __name__ == "__main__":
    main()
