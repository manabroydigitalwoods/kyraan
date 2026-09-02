"""Verification completeness (audit milestone 2026-08-31): every write
tool declares its verification class; deletions verify by ABSENCE;
Gmail modifies verify by label state; drafts verify by existence."""
import pytest

from kyraan.agents import loop_tools
from kyraan.control_plane import kernel


def test_every_write_tool_declares_a_verification_class():
    writes = set(loop_tools.TOOLS) - loop_tools._READ_ONLY_TOOLS
    missing = {t for t in writes if t not in loop_tools.VERIFICATION_CLASS}
    assert not missing, f"writes with no declared verification: {missing}"
    legal = {"read_after_write", "same_store", "undo_path", "foreign"}
    assert set(loop_tools.VERIFICATION_CLASS.values()) <= legal


def test_contracts_surface_the_class():
    from kyraan.tools import registry
    c = registry.contracts()
    assert c["calendar.delete_event"]["verification"] == "read_after_write"
    assert c["calendar.list_events"]["verification"] is None


async def test_deletion_verifies_by_absence(monkeypatch):
    async def run(call, **kw):
        if call.tool_name == "calendar.get_event":
            raise kernel.ToolFailed("Google Calendar returned 404 for that event")
        raise AssertionError(call.tool_name)
    monkeypatch.setattr(kernel, "run_tool", run)
    out = await loop_tools._verified_gone(
        {"deleted": True}, kernel.ToolCall("calendar.get_event",
                                           {"event_id": "e1"}))
    assert out["verified"] is True


async def test_deletion_that_did_not_stick_is_named(monkeypatch):
    async def run(call, **kw):
        return {"id": "e1", "title": "still here"}
    monkeypatch.setattr(kernel, "run_tool", run)
    out = await loop_tools._verified_gone(
        {"deleted": True}, kernel.ToolCall("calendar.get_event",
                                           {"event_id": "e1"}))
    assert out["verified"] is False
    assert "STILL EXISTS" in out["verify_note"]


async def test_mark_read_verifies_label_state(monkeypatch):
    from kyraan.tools import gmail
    monkeypatch.setattr(gmail, "find_message", lambda q: {
        "id": "m1", "from": "a@b.c", "subject": "s",
        "labelIds": ["INBOX", "UNREAD"]})
    monkeypatch.setattr(gmail, "set_labels", lambda *a: None)
    monkeypatch.setattr(gmail, "message_labels", lambda mid: ["INBOX"])
    monkeypatch.setattr(kernel, "confirmed_context", lambda: True)
    out = await loop_tools._email_mark_read(7, {"query": "a@b.c"}, "")
    assert out["verified"] is True

    monkeypatch.setattr(gmail, "message_labels",
                        lambda mid: ["INBOX", "UNREAD"])   # didn't stick
    out = await loop_tools._email_mark_read(7, {"query": "a@b.c"}, "")
    assert out["verified"] is False and "did not stick" in out["verify_note"]

    def boom(mid):
        raise RuntimeError("gmail down")
    monkeypatch.setattr(gmail, "message_labels", boom)     # fail-soft
    out = await loop_tools._email_mark_read(7, {"query": "a@b.c"}, "")
    assert out["verified"] is None
