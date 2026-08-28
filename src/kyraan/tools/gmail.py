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


# Gmail label ids a filter/search may name — fixed vocabulary (no q=
# under gmail.metadata scope; labelIds is the only filter Google allows).
LABELS = ("INBOX", "UNREAD", "IMPORTANT", "STARRED", "SENT",
          "CATEGORY_PERSONAL", "CATEGORY_UPDATES", "CATEGORY_PROMOTIONS",
          "CATEGORY_SOCIAL", "CATEGORY_FORUMS")


def _list_metadata(label_ids: list, max_results: int) -> list:
    """One metadata listing call, label-filtered — the shared primitive
    behind unread/important/search (2026-08-28, email enhancement)."""
    q = "&".join(f"labelIds={lid}" for lid in label_ids)
    listing = _api(f"/messages?{q}&maxResults={int(max_results)}")
    out = []
    for ref in listing.get("messages", []):
        msg = _api(
            f"/messages/{ref['id']}?format=metadata"
            "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date")
        out.append({
            "id": ref["id"],
            "from": _header(msg, "From"),
            "subject": _header(msg, "Subject") or "(no subject)",
            "date": _header(msg, "Date"),
            "labelIds": msg.get("labelIds") or [],
        })
    return out


def _vip_and_keywords() -> tuple:
    try:
        from kyraan.control_plane import config
        cfg = config.load().get("email") or {}
        return ([s.lower() for s in cfg.get("vip_senders") or []],
                [k.lower() for k in cfg.get("important_keywords") or []])
    except Exception:
        return [], []


def _important(limit: int) -> dict:
    """Deterministic priority digest over UNREAD mail — no model
    judgment, no body access. A message qualifies for ANY of: Gmail's
    own IMPORTANT label (its account-level ML, already computed),
    a VIP sender match, or a subject keyword match — each reason is
    named so the owner sees WHY, not just a bare list."""
    vip, keywords = _vip_and_keywords()
    scanned = _list_metadata(["UNREAD"], max(int(limit) * 4, 20))
    items = []
    for m in scanned:
        why = []
        if "IMPORTANT" in m["labelIds"]:
            why.append("Gmail marked important")
        sender = m["from"].lower()
        if any(v in sender for v in vip):
            why.append("VIP sender")
        subject = m["subject"].lower()
        hit_kw = next((k for k in keywords if k in subject), None)
        if hit_kw:
            why.append(f"keyword: {hit_kw}")
        if why:
            items.append({**m, "why": why})
        if len(items) >= int(limit):
            break
    return {"messages": items, "scanned": len(scanned)}


def _search(sender: str, subject: str, label: str, limit: int) -> dict:
    """Filter mail by sender/subject substrings (typed by the user —
    never derived from email content) and an optional Gmail label.
    Metadata only; the same boundary as every other listing here."""
    label = (label or "INBOX").strip().upper()
    if label not in LABELS:
        raise ToolError(f"label must be one of {LABELS}")
    scanned = _list_metadata([label], max(int(limit) * 5, 25))
    sender_w = sender.strip().lower()
    subject_w = subject.strip().lower()
    out = []
    for m in scanned:
        if sender_w and sender_w not in m["from"].lower():
            continue
        if subject_w and subject_w not in m["subject"].lower():
            continue
        out.append(m)
        if len(out) >= int(limit):
            break
    return {"messages": out, "scanned": len(scanned)}


def modify_enabled() -> bool:
    """Owner opt-in (2026-08-28, email enhancement): mark-read/archive
    need gmail.modify — real write access to labels, but Google's own
    scope description EXCLUDES Send, so this can never open a path to
    sending mail (that boundary stays enforced by code absence, as
    with drafts)."""
    import os
    return os.environ.get("KYRAAN_EMAIL_MODIFY", "").strip() == "on"


def find_message(query: str) -> dict:
    """Resolve the user's own words to ONE inbox message — metadata
    including labelIds, so the caller can tell current state (read?
    archived?) BEFORE deciding to ask for a write (the no-op-guard
    pattern: a rename to the same name never asks; neither should
    marking an already-read email read)."""
    query_w = [w for w in query.strip().lower().split() if w]
    if not query_w:
        raise ToolError("say which email — a sender or subject word")
    for m in _list_metadata(["INBOX"], 40):
        haystack = f"{m['from']} {m['subject']}".lower()
        if all(w in haystack for w in query_w):
            return m
    raise ToolError(f"no email matches {query!r} in the inbox")


def set_labels(message_id: str, add: list, remove: list) -> None:
    _api_post(f"/messages/{message_id}/modify",
             {"addLabelIds": add, "removeLabelIds": remove})


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
    if tool_name == "email.important":
        return await asyncio.to_thread(
            _important, max(1, min(int(args.get("limit", 5) or 5), 15)))
    if tool_name == "email.search":
        return await asyncio.to_thread(
            _search, str(args.get("sender", "") or ""),
            str(args.get("subject", "") or ""),
            str(args.get("label", "") or "INBOX"),
            max(1, min(int(args.get("limit", 10) or 10), 20)))
    raise ToolError(f"gmail adapter does not provide {tool_name!r}")
