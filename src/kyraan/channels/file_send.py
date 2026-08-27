"""Files OUT (owner picked it as the top worker-gap, 2026-08-28):
Kyraan sends real Telegram documents — an exported card, a CSV, a
summary file — instead of everything living in chat bubbles.

Text formats only, deliberately: the model COMPOSES the content, the
channel validates and delivers. Binary generation (PDFs, images) is a
different risk class and stays out until asked for. Files go only to
the chat that asked — the send hook is chat-addressed and the executor
passes its own chat_id, never a model-chosen one.
"""
import re

from kyraan.control_plane.logging_setup import log_event

_ALLOWED_EXTENSIONS = (".txt", ".csv", ".md", ".json", ".html")
_MAX_BYTES = 200_000
_send_fn = None  # async (chat_id, filename, data: bytes, caption) -> None


def init(send_fn) -> None:
    global _send_fn
    _send_fn = send_fn


def available() -> bool:
    return _send_fn is not None


def clean_filename(name: str) -> str:
    """A safe basename or ValueError — model-generated, so path
    separators and traversal never reach the transport."""
    name = str(name or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w.\- ]+", "", name).strip(" .")
    if not name or "." not in name:
        raise ValueError("give a filename with an extension, like usage.csv")
    if not name.lower().endswith(_ALLOWED_EXTENSIONS):
        raise ValueError(
            f"only {', '.join(_ALLOWED_EXTENSIONS)} files can be sent")
    return name[:80]


async def send_stored(chat_id: int, path: str, filename: str,
                      caption: str = "") -> dict:
    """Deliver a STORED ORIGINAL back to its owner (a saved card photo,
    an uploaded PDF) — user-uploaded bytes returned verbatim, so the
    text-format rules don't apply; the caller (documents.original_file)
    owns path validation and chat scoping."""
    from pathlib import Path
    if _send_fn is None:
        raise ValueError("file sending isn't wired on this channel")
    data = Path(path).read_bytes()
    await _send_fn(chat_id, filename, data, caption[:200])
    log_event("file_sent", chat_id=chat_id, filename=filename,
              bytes=len(data), stored_original=True)
    return {"filename": filename, "bytes": len(data)}


async def send(chat_id: int, filename: str, content: str,
               caption: str = "") -> dict:
    """Validate and deliver one text file to `chat_id`. Raises
    ValueError on anything unsendable."""
    if _send_fn is None:
        raise ValueError("file sending isn't wired on this channel")
    filename = clean_filename(filename)
    data = str(content or "").encode()
    if not data.strip():
        raise ValueError("the file has no content")
    if len(data) > _MAX_BYTES:
        raise ValueError(f"that's {len(data):,} bytes — the cap is "
                         f"{_MAX_BYTES:,}; split it or trim it")
    await _send_fn(chat_id, filename, data, caption[:200])
    log_event("file_sent", chat_id=chat_id, filename=filename,
              bytes=len(data))
    return {"filename": filename, "bytes": len(data)}
