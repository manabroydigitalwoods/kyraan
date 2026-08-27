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
import urllib.parse
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


def _api_post(path: str, payload: dict, method: str = "POST") -> dict:
    request = urllib.request.Request(
        f"{_BASE}{path}", data=json.dumps(payload).encode(), method=method,
        headers={"Authorization": f"Bearer {access_token()}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ToolError(
                f"Gmail refused the draft ({exc.code}) — drafts need the "
                "gmail.compose scope: set KYRAAN_EMAIL_DRAFTS=on in .env and "
                "re-run scripts/setup_google_oauth.py") from exc
        if exc.code >= 500:
            raise TransientToolError(f"Gmail returned {exc.code}") from exc
        raise ToolError(f"Gmail error {exc.code} on {path}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Gmail: {exc}") from exc


def drafts_enabled() -> bool:
    """Owner opt-in (2026-08-27, after "we can hold email reply... we can
    just draft the email"): Kyraan may CREATE drafts in the owner's Gmail
    — the owner reviews and presses send in Gmail themselves. Gmail has
    no drafts-only scope (gmail.compose is the narrowest), so the real
    boundary is this codebase: NO send code path exists anywhere."""
    import os
    return os.environ.get("KYRAAN_EMAIL_DRAFTS", "").strip() == "on"


def _mime_raw(to: str, subject: str, body: str,
              reply_headers: dict | None = None) -> str:
    import base64
    from email.mime.text import MIMEText
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    for name, value in (reply_headers or {}).items():
        mime[name] = value
    return base64.urlsafe_b64encode(mime.as_bytes()).decode()


def _create_draft(to: str, subject: str, body: str,
                  reply_to_query: str = "") -> dict:
    """Create ONE Gmail draft; replies thread onto the matched message
    (In-Reply-To/References + threadId) so Gmail shows them in place."""
    reply_headers, thread_id = {}, None
    if reply_to_query:
        listing = _api(f"/messages?maxResults=1&q={urllib.parse.quote(reply_to_query)}"
                       ) if bodies_enabled() else None
        # metadata scope forbids q= — fall back to scanning unread headers
        if listing is None:
            listing = _api("/messages?maxResults=25&labelIds=UNREAD")
            wanted = reply_to_query.lower()
            match_id = None
            for m in listing.get("messages", []):
                meta = _api(f"/messages/{m['id']}?format=metadata"
                            "&metadataHeaders=From&metadataHeaders=Subject")
                if wanted in (_header(meta, "From") + " "
                              + _header(meta, "Subject")).lower():
                    match_id = m["id"]
                    break
            listing = {"messages": [{"id": match_id}]} if match_id else {}
        messages = listing.get("messages") or []
        if not messages:
            raise ToolError(f"no email matches {reply_to_query!r} to reply to")
        original = _api(f"/messages/{messages[0]['id']}?format=metadata"
                        "&metadataHeaders=From&metadataHeaders=Subject"
                        "&metadataHeaders=Message-ID")
        thread_id = original.get("threadId")
        message_id = _header(original, "Message-ID")
        if message_id:
            reply_headers = {"In-Reply-To": message_id,
                             "References": message_id}
        to = to or _header(original, "From")
        original_subject = _header(original, "Subject")
        if not subject and original_subject:
            subject = original_subject if original_subject.lower().startswith(
                "re:") else f"Re: {original_subject}"
    message: dict = {"raw": _mime_raw(to, subject, body, reply_headers)}
    if thread_id:
        message["threadId"] = thread_id
    draft = _api_post("/drafts", {"message": message})
    return {"draft_id": draft.get("id", ""), "to": to, "subject": subject}


def _delete_draft(draft_id: str) -> bool:
    try:
        _api_post(f"/drafts/{urllib.parse.quote(draft_id)}", {},
                  method="DELETE")
        return True
    except ToolError:
        return False  # already gone / never existed


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
