"""Local face recognition — biometrics never leave the machine.

OpenCV's built-in stack (YuNet detector + SFace 128-d embeddings, two
small ONNX files in data/models/, one-time download via
scripts/setup_faces.py). Recognition runs on-device in ~3ms before any
cloud call; only a matched NAME ever rides in a prompt — the face
template itself lives in data/faces/<slug>.json (gitignored), one file
per person, deletable like any fact.

Enrollment is a confirm-gated write (a biometric template is the most
personal fact this system stores): photo captioned "remember this face
as <name>" → the standard yes/no ask. Family enrollment should wait for
docs/governance.md §1 — that agreement is human, not enforceable here,
so the confirm ask says plainly what is being stored.

SFace's documented cosine-similarity threshold is 0.363; we store
multiple embeddings per person (each enrollment adds one) and match
against the best.
"""
import json
import re
from pathlib import Path

from kyraan.control_plane.filelock import atomic_write_text
from kyraan.control_plane.logging_setup import log_event

_ROOT = Path(__file__).resolve().parents[3]
FACES_DIR = _ROOT / "data" / "faces"
MODELS_DIR = _ROOT / "data" / "models"
_YUNET = "face_detection_yunet_2023mar.onnx"
_SFACE = "face_recognition_sface_2021dec.onnx"
# SFace's documented adult threshold is 0.363 — live testing (2026-08-26)
# showed it FALSE-POSITIVES on infants (adult-trained embeddings cluster
# baby faces tightly; a different baby matched the enrolled one three
# photos running). Two bands now: a confident match names outright, a
# borderline one only hedges ("might be").
_COSINE_SURE = 0.50
_COSINE_MAYBE = 0.363

_ENROLL_RE = re.compile(
    r"^\s*remember\s+(?:this|my)\s+face\s+as\s+(.{2,40}?)\s*[.!]?\s*$",
    re.IGNORECASE)


_NAMING_RE = re.compile(
    r"^\s*this\s+(?:is\s+)?(?:me,?\s+)?([A-Za-z][A-Za-z .'-]{1,30}?)\s*[.!]?\s*$",
    re.IGNORECASE)


def enroll_request(caption: str):
    """The enrollment caption, parsed deterministically — returns the
    name or None. Never model-inferred: storing a biometric on a guessed
    intent would be wrong in a new way."""
    m = _ENROLL_RE.match(caption or "")
    return m.group(1).strip() if m else None


_SELF_RE = re.compile(
    r"^\s*(?:it'?s|this is|that'?s)\s+me\s*[.!]?\s*$", re.IGNORECASE)


def self_claim(caption: str) -> bool:
    """A photo captioned "its me" is the OWNER asserting identity — the
    natural moment to enroll their face (live 2026-08-28 01:58: the
    selfie got a scene description; the owner's own face had never been
    enrolled, so Kyraan could not recognize its own owner)."""
    return bool(_SELF_RE.match(caption or ""))


def owner_display_name() -> str:
    """The owner's human name from their registry aliases (the tracked
    code never hardcodes personal names) — longest alias, title-cased."""
    try:
        from kyraan.store import persons
        aliases = [n for n, pid in persons.name_map().items()
                   if pid == "owner" and n != "owner"]
        if aliases:
            return max(aliases, key=len).title()
    except Exception:
        pass
    return "Me"


_INVITE_RE = re.compile(
    r"(?:save|remember|store)\s+([A-Z][\w .'-]{1,30}?)[’']s\s+face",
    re.IGNORECASE)


def invite_followup(last_assistant: str) -> str | None:
    """Deterministic: the assistant ITSELF just asked for a photo to save
    NAME's face — a captionless photo that follows is the answer to that
    ask, not a random picture. Found live 2026-08-27 23:37: "to save
    Kamal's face... please send the latest photo" was answered with the
    photo and the bot described the garden instead of enrolling. The
    confirm gate still owns the biometric write."""
    text = last_assistant or ""
    if "photo" not in text.lower():
        return None
    m = _INVITE_RE.search(text)
    return m.group(1).strip() if m else None


_TEXT_ENROLL_RE = re.compile(
    r"^\s*remember\s+(?:(?:this|that|it|him|her)\s+)?(?:face\s+)?(?:is|as)\s+"
    r"([A-Za-z][A-Za-z .'-]{1,30}?)\s*[.!]?\s*$", re.IGNORECASE)

# The most recent photo per chat — process memory ONLY, never persisted;
# lets both the channel fast-path and the agent loop's faces.remember
# tool enroll "the photo you just sent" in any phrasing.
_recent_photos: dict = {}
_RECENT_PHOTO_TTL_S = 600


def stash_photo(chat_id: int, image_bytes: bytes) -> None:
    import time
    _recent_photos[chat_id] = (image_bytes, time.monotonic())


def recent_photo(chat_id: int):
    import time
    entry = _recent_photos.get(chat_id)
    if entry and time.monotonic() - entry[1] < _RECENT_PHOTO_TTL_S:
        return entry[0]
    return None


def enroll_from_text(text: str):
    """A follow-up TEXT like "remember this is kiaan" right after a photo
    (seen live 2026-08-26: the natural phrasing, typed as its own message
    once the photo was already sent). Returns the name or None. Narrow on
    purpose: "remember that my wife is Ruma" doesn't match — 'this/that'
    must sit directly against 'is/as', which is how people refer to the
    photo they just sent, not how they state a family fact."""
    m = _TEXT_ENROLL_RE.match(text or "")
    return m.group(1).strip() if m else None


def enroll_hint(caption: str):
    """A caption that NAMES someone ("this kiaan", "this is Ruma") without
    the enrollment phrase — seen live 2026-08-26: the owner captioned
    "this kiaan" expecting the face to be saved, and nothing offered the
    real phrase. Returns the name to hint about, or None. The hint is
    discoverability only; enrollment itself stays behind the exact phrase
    + confirm gate."""
    m = _NAMING_RE.match(caption or "")
    if not m:
        return None
    name = m.group(1).strip()
    if name.lower() in {n.lower() for n in enrolled_names()}:
        return None  # already enrolled — no hint needed
    return name


def available() -> bool:
    if not ((MODELS_DIR / _YUNET).exists() and (MODELS_DIR / _SFACE).exists()):
        return False
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def _slug(name: str) -> str:
    """Template file key: the REGISTRY person id when the name resolves
    (so "Titu"/"Titu Roy"/"titu-roy" are one template, formally joined
    to the person row), a plain slug otherwise — enrollment must never
    require registration first."""
    try:
        from kyraan.store import persons
        resolved = persons.resolve(name)
        if resolved:
            return resolved
    except Exception:
        pass
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "face"


def _detect_and_embed(image_bytes: bytes) -> list:
    """All faces in the image as 128-d embeddings (may be empty)."""
    import cv2
    import numpy as np

    frame = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return []
    h, w = frame.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(MODELS_DIR / _YUNET), "", (w, h))
    recognizer = cv2.FaceRecognizerSF.create(str(MODELS_DIR / _SFACE), "")
    _, faces = detector.detect(frame)
    embeddings = []
    for row in (faces if faces is not None else []):
        crop = recognizer.alignCrop(frame, row)
        embeddings.append(recognizer.feature(crop).flatten().tolist())
    return embeddings


def _cosine(a, b) -> float:
    import numpy as np
    va, vb = np.asarray(a), np.asarray(b)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb)) or 1e-9
    return float(np.dot(va, vb)) / denom


def _load_enrolled() -> dict:
    enrolled = {}
    if not FACES_DIR.exists():
        return enrolled
    for path in FACES_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            enrolled[record["name"]] = record.get("embeddings", [])
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return enrolled


def recognize(image_bytes: bytes) -> dict:
    """{'names': [...confident...], 'maybe': [...borderline...],
    'unknown_faces': N} — empty-safe; any failure means 'no faces'
    rather than a broken photo turn. Every match logs its score so the
    thresholds can be tuned from soak evidence."""
    try:
        found = _detect_and_embed(image_bytes)
    except Exception as exc:
        log_event("face_recognize_error", error=str(exc)[:150])
        return {"names": [], "maybe": [], "unknown_faces": 0}
    enrolled = _load_enrolled()
    names, maybe, unknown, scores = [], [], 0, []
    for emb in found:
        best_name, best_score = None, 0.0
        for name, stored in enrolled.items():
            for s in stored:
                score = _cosine(emb, s)
                if score > best_score:
                    best_name, best_score = name, score
        scores.append({"best": best_name, "score": round(best_score, 3)})
        if best_name is None or best_score < _COSINE_MAYBE:
            unknown += 1
        elif best_score >= _COSINE_SURE and best_name not in names:
            names.append(best_name)
        elif best_score < _COSINE_SURE and best_name not in maybe and best_name not in names:
            maybe.append(best_name)
    if found:
        log_event("faces_recognized", names=names, maybe=maybe,
                  unknown=unknown, scores=scores)
    return {"names": names, "maybe": maybe, "unknown_faces": unknown}


def enroll(name: str, image_bytes: bytes) -> str:
    """Store one embedding for `name` from a photo containing exactly one
    face. Returns the user-facing receipt; raises ValueError on a photo
    that can't enroll."""
    found = _detect_and_embed(image_bytes)
    if len(found) == 0:
        raise ValueError("no face found in that photo — try a clearer, closer shot")
    if len(found) > 1:
        raise ValueError(f"{len(found)} faces in that photo — enrollment needs "
                         "exactly one, so I know which face is meant")
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        FACES_DIR.chmod(0o700)   # the directory, like data/ and memory/
    except OSError:
        pass
    path = FACES_DIR / f"{_slug(name)}.json"
    record = {"name": name, "embeddings": []}
    if path.exists():
        try:
            record = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    record["embeddings"].append(found[0])
    # Biometric templates are the most sensitive bytes in the system:
    # write them 0600 like every other data file. A bare write_text left
    # NEW enrollments 0644 — readable by any local user (Bugbot P1); the
    # atomic writer chmods before the rename, so no window exists.
    atomic_write_text(path, json.dumps(record))
    _mirror_templates(name, _slug(name), record["embeddings"])
    log_event("face_enrolled", name=name, samples=len(record["embeddings"]))
    return (f'Face saved as "{name}" ({len(record["embeddings"])} sample'
            f'{"s" if len(record["embeddings"]) != 1 else ""} now) — stored '
            "only on this machine; more photos improve matching. Say "
            f'"forget the face {name}" to delete it.')


def forget(name: str) -> bool:
    path = FACES_DIR / f"{_slug(name)}.json"
    if not path.exists():
        return False
    path.unlink()
    try:  # the file unlink is the authoritative delete; the mirror row
        from kyraan.store import facts as _facts  # must not outlive it
        from kyraan.store import pg as _pg
        if _facts.MIRROR_ENABLED:
            with _pg.connection() as conn:
                conn.execute("DELETE FROM face_template WHERE slug = %s",
                             (_slug(name),))
                conn.commit()
    except Exception as exc:
        log_event("face_sync_deferred", name=name, reason=str(exc)[:120])
    log_event("face_forgotten", name=name)
    return True


def _template_uuid(slug: str, embedding: list) -> str:
    import hashlib
    import uuid as _uuid
    digest = hashlib.sha1(json.dumps(embedding).encode()).hexdigest()[:16]
    return str(_uuid.uuid5(_uuid.NAMESPACE_DNS,
                           f"face.kyraan.local:{slug}:{digest}"))


def _mirror_templates(name: str, slug: str, embeddings: list) -> bool:
    """Best-effort FULL mirror of one person's templates into the local
    Postgres (deterministic ids ⇒ idempotent). Gated on the store
    mirroring switch so tests never touch the live container; failure
    logs and never blocks the file write — files stay the authority."""
    try:
        from kyraan.store import facts as _facts
        from kyraan.store import pg as _pg
        if not _facts.MIRROR_ENABLED:
            return False
        with _pg.connection() as conn:
            conn.execute("DELETE FROM face_template WHERE slug = %s", (slug,))
            for emb in embeddings:
                conn.execute(
                    """INSERT INTO face_template (id, slug, name, embedding)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (_template_uuid(slug, emb), slug, name, json.dumps(emb)))
            conn.commit()
        return True
    except Exception as exc:
        log_event("face_sync_deferred", name=name, reason=str(exc)[:120])
        return False


def resync_templates() -> int:
    """Full rebuild of face_template from data/faces/ — rows for slugs
    whose file is gone are removed. Idempotent; called by resync_facts."""
    from kyraan.store import pg as _pg
    slugs, mirrored = [], 0
    for path in sorted(FACES_DIR.glob("*.json")) if FACES_DIR.exists() else []:
        try:
            record = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        slug = path.stem
        slugs.append(slug)
        if _mirror_templates(record.get("name", slug), slug,
                             record.get("embeddings") or []):
            mirrored += len(record.get("embeddings") or [])
    with _pg.connection() as conn:
        if slugs:
            conn.execute("DELETE FROM face_template WHERE slug != ALL(%s)",
                         (slugs,))
        else:
            conn.execute("DELETE FROM face_template")
        conn.commit()
    return mirrored


def enrolled_names() -> list:
    return sorted(_load_enrolled())
