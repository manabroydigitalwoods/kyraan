"""Email tools enhancement (owner, 2026-08-28): priority digest,
sender/subject/label filtering, mark-read/archive — all metadata-only,
same §3a boundary as email.unread/email.read."""
import pytest

from kyraan.agents import loop_tools
from kyraan.control_plane import kernel
from kyraan.tools import gmail


def _msg(id_="m1", frm="someone@x.com", subject="hi", labels=None):
    return {"id": id_, "from": frm, "subject": subject, "date": "d",
            "labelIds": labels or []}


# --- gmail.py: the deterministic scoring/filtering ------------------------

def test_important_merges_gmail_vip_and_keyword_signals(monkeypatch):
    monkeypatch.setattr(gmail, "_vip_and_keywords",
                        lambda: (["ruma"], ["otp", "invoice"]))
    scanned = [
        _msg("m1", "Ruma <r@x.com>", "lunch plans"),           # VIP only
        _msg("m2", "bank@x.com", "Your OTP is 1234"),          # keyword only
        _msg("m3", "random@x.com", "newsletter", ["IMPORTANT"]),  # gmail only
        _msg("m4", "random@x.com", "nothing relevant"),        # none — excluded
    ]
    monkeypatch.setattr(gmail, "_list_metadata", lambda labels, n: scanned)
    result = gmail._important(10)
    ids = {m["id"]: m["why"] for m in result["messages"]}
    assert ids["m1"] == ["VIP sender"]
    assert ids["m2"] == ["keyword: otp"]
    assert ids["m3"] == ["Gmail marked important"]
    assert "m4" not in ids
    assert result["scanned"] == 4


def test_important_respects_limit(monkeypatch):
    monkeypatch.setattr(gmail, "_vip_and_keywords", lambda: (["x"], []))
    scanned = [_msg(f"m{i}", "x@x.com", "s") for i in range(10)]
    monkeypatch.setattr(gmail, "_list_metadata", lambda labels, n: scanned)
    result = gmail._important(3)
    assert len(result["messages"]) == 3


def test_search_filters_by_sender_subject_and_validates_label(monkeypatch):
    scanned = [
        _msg("m1", "Kamal <kamal@x.com>", "profile update"),
        _msg("m2", "bank@x.com", "statement"),
    ]
    monkeypatch.setattr(gmail, "_list_metadata", lambda labels, n: scanned)
    out = gmail._search("kamal", "", "INBOX", 10)
    assert [m["id"] for m in out["messages"]] == ["m1"]
    out = gmail._search("", "statement", "INBOX", 10)
    assert [m["id"] for m in out["messages"]] == ["m2"]
    with pytest.raises(gmail.ToolError, match="label must be one of"):
        gmail._search("", "", "NOT_A_LABEL", 10)


def test_find_message_resolves_or_raises(monkeypatch):
    scanned = [_msg("m1", "Amazon Pay <a@x.com>", "Payment Reminder",
                    ["UNREAD", "INBOX"])]
    monkeypatch.setattr(gmail, "_list_metadata", lambda labels, n: scanned)
    match = gmail.find_message("amazon pay")
    assert match["id"] == "m1"
    with pytest.raises(gmail.ToolError, match="no email matches"):
        gmail.find_message("nonexistent sender")
    with pytest.raises(gmail.ToolError):
        gmail.find_message("   ")


def test_set_labels_posts_the_modify_payload(monkeypatch):
    captured = {}
    monkeypatch.setattr(gmail, "_api_post",
                        lambda path, payload, method="POST":
                        captured.update(path=path, payload=payload))
    gmail.set_labels("m1", ["INBOX"], ["UNREAD"])
    assert captured["path"] == "/messages/m1/modify"
    assert captured["payload"] == {"addLabelIds": ["INBOX"],
                                   "removeLabelIds": ["UNREAD"]}


def test_modify_enabled_is_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("KYRAAN_EMAIL_MODIFY", raising=False)
    assert gmail.modify_enabled() is False
    monkeypatch.setenv("KYRAAN_EMAIL_MODIFY", "on")
    assert gmail.modify_enabled() is True


# --- loop_tools executors: the §3a boundary and no-op guards --------------

async def test_important_short_circuits_under_a_cloud_tier(monkeypatch):
    from kyraan.agents import orchestrator
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)

    async def fake_run_tool(call):
        return {"messages": [{"from": '"Ruma" <r@x.com>', "subject": "hi",
                              "why": ["VIP sender"]}], "scanned": 1}

    monkeypatch.setattr(kernel, "run_tool", fake_run_tool)
    out = await loop_tools._email_important(7, {}, "")
    assert "__direct_reply__" in out
    assert "Ruma" in out["__direct_reply__"]
    assert "VIP sender" in out["__direct_reply__"]


async def test_important_passes_through_raw_when_all_local(monkeypatch):
    from kyraan.agents import orchestrator
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: False)
    raw = {"messages": [{"from": "x", "subject": "y", "why": ["z"]}],
           "scanned": 1}

    async def fake_run_tool(call):
        return raw

    monkeypatch.setattr(kernel, "run_tool", fake_run_tool)
    out = await loop_tools._email_important(7, {}, "")
    assert out is raw   # nothing composed — the local model may see it


async def test_search_requires_at_least_one_filter():
    with pytest.raises(kernel.ToolFailed, match="give a sender"):
        await loop_tools._email_search(7, {}, "")


async def test_mark_read_no_op_when_already_read(monkeypatch):
    monkeypatch.setattr(gmail, "find_message",
                        lambda q: {"id": "m1", "from": "x", "subject": "y",
                                   "labelIds": ["INBOX"]})  # no UNREAD
    out = await loop_tools._email_mark_read(7, {"query": "the bill"}, "")
    assert out == {"changed": False, "note": "that email is already read"}


async def test_mark_read_confirms_then_writes(monkeypatch):
    monkeypatch.setattr(gmail, "find_message",
                        lambda q: {"id": "m1", "from": "Bank <b@x.com>",
                                   "subject": "Statement",
                                   "labelIds": ["UNREAD", "INBOX"]})
    calls = []
    monkeypatch.setattr(gmail, "set_labels",
                        lambda mid, add, remove: calls.append((mid, add, remove)))
    with pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._email_mark_read(7, {"query": "statement"}, "")
    assert calls == []
    monkeypatch.setattr(kernel, "confirmed_context", lambda: True)
    monkeypatch.setattr(gmail, "message_labels", lambda mid: ["INBOX"])
    out = await loop_tools._email_mark_read(7, {"query": "statement"}, "")
    assert out == {"changed": True, "id": "m1", "from": "Bank <b@x.com>",
                   "subject": "Statement", "verified": True}
    assert calls == [("m1", [], ["UNREAD"])]


async def test_archive_no_op_when_already_archived(monkeypatch):
    monkeypatch.setattr(gmail, "find_message",
                        lambda q: {"id": "m1", "from": "x", "subject": "y",
                                   "labelIds": []})  # no INBOX
    out = await loop_tools._email_archive(7, {"query": "old promo"}, "")
    assert out == {"changed": False, "note": "that email is already archived"}


async def test_archive_confirms_then_writes(monkeypatch):
    monkeypatch.setattr(gmail, "find_message",
                        lambda q: {"id": "m2", "from": "Promo <p@x.com>",
                                   "subject": "Sale", "labelIds": ["INBOX"]})
    calls = []
    monkeypatch.setattr(gmail, "set_labels",
                        lambda mid, add, remove: calls.append((mid, add, remove)))
    with pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._email_archive(7, {"query": "sale"}, "")
    monkeypatch.setattr(kernel, "confirmed_context", lambda: True)
    out = await loop_tools._email_archive(7, {"query": "sale"}, "")
    assert out["changed"] is True
    assert calls == [("m2", [], ["INBOX"])]


def test_undo_shapes_for_mark_read_and_archive():
    assert loop_tools.UNDO_MAP["email.mark_read"](
        {}, {"changed": True, "id": "m1"}, None
    ) == ("email.mark_unread", {"message_id": "m1"})
    assert loop_tools.UNDO_MAP["email.archive"](
        {}, {"changed": True, "id": "m2"}, None
    ) == ("email.unarchive", {"message_id": "m2"})
    # a no-op write is never recorded as undoable
    assert loop_tools.UNDO_MAP["email.mark_read"](
        {}, {"changed": False, "note": "already read"}, None) is None


# --- menu gating: mark_read/archive are opt-in ----------------------------

def test_mark_read_and_archive_hidden_until_modify_enabled(monkeypatch):
    from kyraan.agents import agent_loop
    monkeypatch.setattr(gmail, "modify_enabled", lambda: False)
    menu = agent_loop._tools_block()
    assert "email.mark_read" not in menu and "email.archive" not in menu
    monkeypatch.setattr(gmail, "modify_enabled", lambda: True)
    menu = agent_loop._tools_block()
    assert "email.mark_read" in menu and "email.archive" in menu


def test_important_and_search_always_in_the_owner_menu():
    from kyraan.agents import agent_loop
    menu = agent_loop._tools_block()
    assert "email.important" in menu and "email.search" in menu


# --- config -----------------------------------------------------------

def test_vip_and_keyword_config_loads():
    vip, keywords = gmail._vip_and_keywords()
    assert vip and keywords   # the seeded permissions.yaml block


# --- registry wiring (kernel.run_tool dispatch) ----------------------

def test_registry_resolves_the_two_dispatched_tools():
    from kyraan.tools import registry
    for name in ("email.important", "email.search"):
        spec = registry.get(name)
        assert spec.server == "gmail" and spec.permission == "auto"
