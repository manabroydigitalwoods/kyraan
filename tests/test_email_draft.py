"""Email drafting (owner: "we can hold email reply... we can just
draft the email", 2026-08-27): Kyraan CREATES Gmail drafts, the owner
sends from Gmail. No send code path exists anywhere — that absence is
the enforced boundary (gmail.compose has no drafts-only variant)."""
import base64

import pytest

from kyraan.agents import loop_tools
from kyraan.control_plane import kernel
from kyraan.tools import gmail


def test_no_send_code_path_exists():
    """The boundary IS this absence: nothing in the gmail adapter or the
    loop tools can send mail. If someone adds one, this test makes them
    face the owner's explicit hold on sending."""
    import inspect
    for module in (gmail, loop_tools):
        source = inspect.getsource(module)
        assert "messages/send" not in source
        assert "drafts/send" not in source
    assert "email.send" not in loop_tools.TOOLS


def test_drafts_enabled_is_an_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("KYRAAN_EMAIL_DRAFTS", raising=False)
    assert gmail.drafts_enabled() is False
    monkeypatch.setenv("KYRAAN_EMAIL_DRAFTS", "on")
    assert gmail.drafts_enabled() is True


def test_menu_hides_the_tool_until_opted_in(monkeypatch):
    from kyraan.agents import agent_loop
    monkeypatch.setattr(gmail, "drafts_enabled", lambda: False)
    assert "email.draft" not in agent_loop._tools_block()
    monkeypatch.setattr(gmail, "drafts_enabled", lambda: True)
    assert "email.draft" in agent_loop._tools_block()


def test_mime_raw_builds_reply_threading_headers():
    import email as email_lib
    raw = gmail._mime_raw("a@b.c", "Re: Bill", "Paid it today.",
                          {"In-Reply-To": "<m1@x>", "References": "<m1@x>"})
    parsed = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed["To"] == "a@b.c"
    assert parsed["Subject"] == "Re: Bill"
    assert parsed["In-Reply-To"] == "<m1@x>"
    assert parsed.get_payload(decode=True).decode() == "Paid it today."


async def test_executor_is_confirm_gated_and_validates():
    with pytest.raises(kernel.ToolFailed):
        await loop_tools._email_draft(7, {"to": "a@b.c", "body": ""}, "")
    with pytest.raises(kernel.ToolFailed):
        await loop_tools._email_draft(7, {"body": "hi"}, "")  # no target
    with pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._email_draft(
            7, {"to": "a@b.c", "subject": "Hi", "body": "hello"}, "")


def test_confirm_ask_shows_the_actual_body():
    ask = loop_tools._describe_call(
        "email.draft", {"reply_to_query": "amazon pay",
                        "body": "Paid it today, please confirm."})
    assert "DRAFT" in ask and "never sent" in ask
    assert "Paid it today, please confirm." in ask


def test_undo_deletes_the_draft():
    assert loop_tools.UNDO_MAP["email.draft"](
        {}, {"drafted": True, "draft_id": "r123", "to": "a@b.c",
             "subject": "Re: Bill"}, None
    ) == ("email.draft_delete", {"draft_id": "r123"})
