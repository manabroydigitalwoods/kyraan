"""Local face recognition — enrollment caption parsing, the cosine
matcher, template storage/deletion, the confirm-gated flows, and the
on-device boundary (only names ever reach a prompt)."""
import time

import pytest

from kyraan.agents import faces, orchestrator, photo
from kyraan.control_plane.kernel import SkillCall


def test_enroll_caption_parsing():
    assert faces.enroll_request("remember this face as Maan") == "Maan"
    assert faces.enroll_request("  Remember my face as Maan Roy! ") == "Maan Roy"
    assert faces.enroll_request("what is this?") is None
    assert faces.enroll_request("") is None
    assert faces.enroll_request("remember this face as") is None


def test_enroll_match_and_forget(monkeypatch):
    # embeddings faked — the matcher's math and storage are what's under test
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[1.0, 0.0, 0.0]])
    receipt = faces.enroll("Maan", b"img")
    assert "only on this machine" in receipt
    assert faces.enrolled_names() == ["Maan"]

    # same direction → cosine 1.0 → confident match
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[0.9, 0.0, 0.0]])
    result = faces.recognize(b"img2")
    assert result == {"names": ["Maan"], "maybe": [], "unknown_faces": 0}

    # borderline (between MAYBE and SURE thresholds) → hedged, not named
    import math
    angle_emb = [0.42, math.sqrt(1 - 0.42**2), 0.0]  # cosine ≈ 0.42 vs [1,0,0]
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [angle_emb])
    result = faces.recognize(b"img_borderline")
    assert result == {"names": [], "maybe": ["Maan"], "unknown_faces": 0}

    # orthogonal → below both thresholds → unknown
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[0.0, 1.0, 0.0]])
    result = faces.recognize(b"img3")
    assert result == {"names": [], "maybe": [], "unknown_faces": 1}

    assert faces.forget("Maan") is True
    assert faces.forget("Maan") is False
    assert faces.enrolled_names() == []


def test_enroll_hint_on_naming_captions(monkeypatch):
    """Live: "this kiaan" was expected to save the face; the hint offers
    the real phrase — but never for enrolled names or ordinary captions."""
    assert faces.enroll_hint("this kiaan") == "kiaan"
    assert faces.enroll_hint("This is Ruma!") == "Ruma"
    assert faces.enroll_hint("what is this?") is None
    assert faces.enroll_hint("remember this face as Maan") is None  # real phrase, not a hint case
    monkeypatch.setattr(faces, "enrolled_names", lambda: ["Kiaan"])
    assert faces.enroll_hint("this is kiaan") is None  # already enrolled


def test_enroll_from_text_matches_photo_followups_not_family_facts():
    assert faces.enroll_from_text("remember this is kiaan") == "kiaan"
    assert faces.enroll_from_text("Remember this face as Maan Roy") == "Maan Roy"
    assert faces.enroll_from_text("remember her as Ruma") == "Ruma"
    # ordinary memory statements must NOT read as biometric requests
    assert faces.enroll_from_text("remember that my wife is Ruma") is None
    assert faces.enroll_from_text("remember to call mom") is None
    assert faces.enroll_from_text("what is this?") is None


async def test_text_followup_enrolls_the_recent_photo(monkeypatch):
    """Live 2026-08-26: photo sent, then the TEXT "remember this is
    kiaan" — the photo was gone and the owner was told to resend it. The
    channel now stashes the last photo (in memory, 10-min TTL) so the
    follow-up enrolls it, still through the confirm gate."""
    import time as _time

    from kyraan.channels import telegram_bot
    from kyraan.agents import orchestrator

    orchestrator._pending_confirmations.pop(97, None)
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[1.0, 0.0]])
    telegram_bot._recent_photos[97] = (b"the-photo-bytes", _time.monotonic())

    replies = []

    async def reply_text(text, **kw):
        replies.append(text)

    update = __import__("types").SimpleNamespace(
        effective_user=__import__("types").SimpleNamespace(id=1),
        effective_chat=__import__("types").SimpleNamespace(id=97, type="private"),
        message=__import__("types").SimpleNamespace(
            text="remember this is kiaan", reply_text=reply_text),
    )
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    await telegram_bot._on_message(update, __import__("types").SimpleNamespace(bot=None))
    assert replies and "FACE TEMPLATE" in replies[0] and "kiaan" in replies[0]
    assert 97 in orchestrator._pending_confirmations   # awaiting the yes
    assert faces.enrolled_names() == []                # nothing stored yet
    orchestrator._pending_confirmations.pop(97, None)
    telegram_bot._recent_photos.pop(97, None)


def test_enroll_needs_exactly_one_face(monkeypatch):
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [])
    with pytest.raises(ValueError, match="no face"):
        faces.enroll("X", b"img")
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[1.0], [2.0]])
    with pytest.raises(ValueError, match="exactly one"):
        faces.enroll("X", b"img")


def test_recognize_failure_is_empty_not_broken(monkeypatch):
    def boom(b):
        raise RuntimeError("cv2 exploded")
    monkeypatch.setattr(faces, "_detect_and_embed", boom)
    assert faces.recognize(b"img") == {"names": [], "maybe": [], "unknown_faces": 0}


async def test_recognized_names_ride_into_the_vision_prompt(monkeypatch):
    seen = {}

    class _R:
        text = "Kiaan is crawling on the floor."
        latency_ms = 5.0

    async def fake_acall(prompt="", system="", tier="", images=None, **kw):
        seen["prompt"] = prompt
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply = await photo.answer(9, "data:x", "what's he doing?", recognized=["Kiaan"])
    assert "Kiaan" in reply
    assert "LOCALLY RECOGNIZED FACES" in seen["prompt"]
    assert "Kiaan" in seen["prompt"]


async def test_forget_face_command_is_confirm_gated(monkeypatch):
    orchestrator._pending_confirmations.pop(95, None)
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[1.0, 0.0]])
    faces.enroll("Maan", b"img")

    async def no_facts(raw_text, context="", insist=False):
        return []
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)

    ask = await orchestrator.handle_message(chat_id=95, raw_text="forget the face Maan")
    assert "DELETE the stored face template" in ask and '"yes"' in ask
    assert faces.enrolled_names() == ["Maan"]     # nothing deleted before the yes

    reply = await orchestrator.handle_message(chat_id=95, raw_text="yes")
    assert "Deleted the stored face template" in reply
    assert faces.enrolled_names() == []
    orchestrator._pending_confirmations.pop(95, None)


async def test_enroll_flow_is_confirm_gated(monkeypatch):
    """The channel-side gate: the ask names the biometric, and the yes
    runs enrollment with the captured bytes."""
    from kyraan.channels import telegram_bot

    orchestrator._pending_confirmations.pop(96, None)
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[1.0, 0.0]])

    ask = await telegram_bot._enroll_face_gated(96, "Maan", b"imgbytes")
    assert "FACE TEMPLATE" in ask and "ONLY on this machine" in ask
    assert faces.enrolled_names() == []           # not stored before the yes

    async def no_facts(raw_text, context="", insist=False):
        return []
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    reply = await orchestrator.handle_message(chat_id=96, raw_text="yes")
    assert "only on this machine" in reply
    assert faces.enrolled_names() == ["Maan"]
    orchestrator._pending_confirmations.pop(96, None)
