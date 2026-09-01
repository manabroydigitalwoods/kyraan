"""Google Contacts adapter (governance round 2026-09-01, owner's three
decisions):
- names MAY enter cloud prompts, like registry names already do;
- phone numbers and emails are LOCAL-ONLY — stored, shown to the owner
  on request through a direct reply, never in a model prompt (§3a);
- scope: My Contacts (the curated list), read-only.

Sync runs as a NIGHTLY JOB, never as an agent-callable tool (plan §3c
precondition). Enabled by KYRAAN_CONTACTS=on plus a re-run of
scripts/setup_google_oauth.py to grant contacts.readonly.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from kyraan.tools.google_auth import access_token
from kyraan.tools.registry import ToolError, TransientToolError

_BASE = "https://people.googleapis.com/v1"
_FIELDS = "names,phoneNumbers,emailAddresses"


def enabled() -> bool:
    return os.environ.get("KYRAAN_CONTACTS", "").strip() == "on"


def _api(path: str) -> dict:
    request = urllib.request.Request(
        f"{_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token()}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            detail = ""
            try:
                detail = json.loads(exc.read()).get(
                    "error", {}).get("message", "")[:200]
            except Exception:
                pass
            raise ToolError(
                f"Google Contacts refused ({exc.code}): "
                f"{detail or 'scope missing?'} — re-run "
                "scripts/setup_google_oauth.py with KYRAAN_CONTACTS=on"
            ) from exc
        if exc.code >= 500:
            raise TransientToolError(f"Google Contacts returned {exc.code}") from exc
        raise ToolError(f"Google Contacts returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Google Contacts: {exc}") from exc


def fetch_all() -> list:
    """Every My Contacts connection, normalized and provider-neutral:
    [{resource, name, phones, emails}]. Paged; names without a display
    name are skipped (nothing to resolve by)."""
    out, token = [], ""
    while True:
        path = (f"/people/me/connections?personFields={_FIELDS}"
                "&pageSize=200" + (f"&pageToken={token}" if token else ""))
        page = _api(path)
        for person in page.get("connections", []):
            names = person.get("names") or []
            display = next((n.get("displayName") for n in names
                            if n.get("displayName")), "")
            if not display:
                continue
            out.append({
                "resource": person.get("resourceName", ""),
                "name": display.strip()[:120],
                "phones": [p.get("value", "").strip() for p in
                           (person.get("phoneNumbers") or []) if p.get("value")][:5],
                "emails": [e.get("value", "").strip() for e in
                           (person.get("emailAddresses") or []) if e.get("value")][:5],
            })
        token = page.get("nextPageToken", "")
        if not token:
            return out
