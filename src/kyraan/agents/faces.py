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

from kyraan.control_plane.logging_setup import log_event

_ROOT = Path(__file__).resolve().parents[3]
FACES_DIR = _ROOT / "data" / "faces"
MODELS_DIR = _ROOT / "data" / "models"
_YUNET = "face_detection_yunet_2023mar.onnx"
_SFACE = "face_recognition_sface_2021dec.onnx"
_COSINE_THRESHOLD = 0.363  # SFace's documented match threshold

_ENROLL_RE = re.compile(
    r"^\s*remember\s+(?:this|my)\s+face\s+as\s+(.{2,40}?)\s*[.!]?\s*$",
    re.IGNORECASE)


def enroll_request(caption: str):
    """The enrollment caption, parsed deterministically — returns the
    name or None. Never model-inferred: storing a biometric on a guessed
    intent would be wrong in a new way."""
    m = _ENROLL_RE.match(caption or "")
    return m.group(1).strip() if m else None


def available() -> bool:
    if not ((MODELS_DIR / _YUNET).exists() and (MODELS_DIR / _SFACE).exists()):
        return False
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def _slug(name: str) -> str:
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
    """{'names': [...matched...], 'unknown_faces': N} — empty-safe, and
    any failure means 'no faces' rather than a broken photo turn."""
    try:
        found = _detect_and_embed(image_bytes)
    except Exception as exc:
        log_event("face_recognize_error", error=str(exc)[:150])
        return {"names": [], "unknown_faces": 0}
    enrolled = _load_enrolled()
    names, unknown = [], 0
    for emb in found:
        best_name, best_score = None, 0.0
        for name, stored in enrolled.items():
            for s in stored:
                score = _cosine(emb, s)
                if score > best_score:
                    best_name, best_score = name, score
        if best_name is not None and best_score >= _COSINE_THRESHOLD and best_name not in names:
            names.append(best_name)
        elif best_score < _COSINE_THRESHOLD:
            unknown += 1
    if names:
        log_event("faces_recognized", names=names, unknown=unknown)
    return {"names": names, "unknown_faces": unknown}


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
    path = FACES_DIR / f"{_slug(name)}.json"
    record = {"name": name, "embeddings": []}
    if path.exists():
        try:
            record = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    record["embeddings"].append(found[0])
    path.write_text(json.dumps(record))
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
    log_event("face_forgotten", name=name)
    return True


def enrolled_names() -> list:
    return sorted(_load_enrolled())
