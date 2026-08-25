"""Gmail adapter — tool #3, read-only METADATA by deliberate scope
decision (2026-08-25): unread count + sender/subject/date lines, never
message bodies. Owner's §3a resolution: work tools in, Woodsportal out —
and the data boundary is enforced upstream too (the orchestrator keeps
email summaries out of the conversation history that feeds cloud models).

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import json
import urllib.error
import urllib.request

from kyraan.tools.google_auth import access_token
from kyraan.tools.registry import ToolError, TransientToolError

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _api(path: str) -> dict:
    request = urllib.request.Request(
        f"{_BASE}{path}", headers={"Authorization": f"Bearer {access_token()}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # Google's own message names the actual cause (disabled API vs
            # missing scope vs revoked token) — pass it through instead of
            # guessing; a collapsed message sent the owner to the wrong fix
            # once already.
            detail = ""
            try:
                body = json.loads(exc.read())
                detail = body.get("error", {}).get("message", "")[:300]
            except Exception:
                pass
            raise ToolError(
                f"Gmail refused access ({exc.code}): {detail or 'no detail'} — if it mentions scopes, "
                "re-run scripts/setup_google_oauth.py; if it mentions the API being disabled, enable "
                "the Gmail API in the GCP console"
            ) from exc
        if exc.code >= 500:
            raise TransientToolError(f"Gmail returned {exc.code}") from exc
        raise ToolError(f"Gmail error {exc.code} on {path}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Gmail: {exc}") from exc


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _unread(limit: int) -> dict:
    listing = _api(f"/messages?q=is:unread&maxResults={int(limit)}")
    total = listing.get("resultSizeEstimate", 0)
    items = []
    for ref in listing.get("messages", [])[: int(limit)]:
        msg = _api(
            f"/messages/{ref['id']}?format=metadata"
            "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date"
        )
        items.append({
            "from": _header(msg, "From"),
            "subject": _header(msg, "Subject") or "(no subject)",
            "date": _header(msg, "Date"),
        })
    return {"unread_estimate": total, "messages": items}


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "email.unread":
        return await asyncio.to_thread(_unread, args.get("limit", 5))
    raise ToolError(f"gmail adapter does not provide {tool_name!r}")
