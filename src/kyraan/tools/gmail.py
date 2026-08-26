"""Gmail adapter — tool #3, read-only METADATA by deliberate scope
decision (2026-08-25): unread count + sender/subject/date lines, never
message bodies. Owner's §3a resolution: work tools in, Portalapp out —
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
    # labelIds=UNREAD, not q=is:unread — the gmail.metadata scope (the
    # boundary-enforcing scope adopted in the security review) rejects
    # the q parameter outright; label filtering is permitted.
    listing = _api(f"/messages?labelIds=UNREAD&maxResults={int(limit)}")
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


_BODY_CHAR_CAP = 6000  # per message, before local summarization


def bodies_enabled() -> bool:
    import os
    return os.environ.get("KYRAAN_EMAIL_BODIES", "").strip() == "local"


def _decode_part(data: str) -> str:
    import base64
    try:
        return base64.urlsafe_b64decode(data + "===").decode(errors="replace")
    except Exception:
        return ""


def _extract_text(payload: dict) -> str:
    """Prefer text/plain anywhere in the MIME tree; fall back to
    tag-stripped text/html."""
    import re as _re
    plain, html = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime.startswith("text/plain"):
            plain.append(_decode_part(data))
        elif data and mime.startswith("text/html"):
            html.append(_decode_part(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)
    import html as _html
    return _html.unescape(_re.sub(r"<[^>]+>", " ", " ".join(html)))


def _read(query: str, limit: int) -> list:
    if not bodies_enabled():
        # Defense in depth: the flag gates the adapter too, not just the
        # menu — and the error tells the owner the exact opt-in path.
        raise ToolError(
            "email bodies are disabled — to enable local-only reading, set "
            "KYRAAN_EMAIL_BODIES=local in .env, re-run "
            "scripts/setup_google_oauth.py (grants gmail.readonly), restart"
        )
    listing = _api(f"/messages?labelIds=UNREAD&maxResults=15")
    wanted = [w for w in str(query or "").lower().split() if w]
    out = []
    for ref in listing.get("messages", []):
        if len(out) >= limit:
            break
        msg = _api(f"/messages/{ref['id']}?format=full")
        sender = _header(msg, "From")
        subject = _header(msg, "Subject") or "(no subject)"
        if wanted and not any(w in f"{sender} {subject}".lower() for w in wanted):
            continue
        body = _extract_text(msg.get("payload") or {}).strip()
        out.append({
            "from": sender, "subject": subject, "date": _header(msg, "Date"),
            "body": body[:_BODY_CHAR_CAP],
        })
    return out


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "email.unread":
        return await asyncio.to_thread(_unread, args.get("limit", 5))
    if tool_name == "email.read":
        return await asyncio.to_thread(
            _read, str(args.get("query", "") or ""),
            max(1, min(int(args.get("limit", 2) or 2), 3)))
    raise ToolError(f"gmail adapter does not provide {tool_name!r}")
