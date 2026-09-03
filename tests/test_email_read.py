"""email.read — the local-only bodies opt-in: flag gating, MIME
extraction, and the executor keeping every byte of content out of cloud
prompts and history."""
import base64
import json

import pytest

from kyraan.agents import agent_loop, orchestrator
from kyraan.tools import gmail
from kyraan.tools import registry as reg


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_disabled_flag_refuses_with_the_optin_path(monkeypatch):
    monkeypatch.delenv("KYRAAN_EMAIL_BODIES", raising=False)
    with pytest.raises(reg.ToolError, match="KYRAAN_EMAIL_BODIES=local"):
        gmail._read("", 2)
    assert not gmail.bodies_enabled()


def test_mime_extraction_prefers_plain_falls_back_to_html():
    plain = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": _b64("<b>ignored</b>")}},
        {"mimeType": "text/plain", "body": {"data": _b64("the real text")}},
    ]}
    assert gmail._extract_text(plain) == "the real text"
    html_only = {"mimeType": "text/html",
                 "body": {"data": _b64("<p>Hello &amp; goodbye</p>")}}
    assert "Hello & goodbye" in gmail._extract_text(html_only)


def test_read_filters_by_query_and_caps_body(monkeypatch):
    monkeypatch.setenv("KYRAAN_EMAIL_BODIES", "local")

    def fake_api(path):
        if path.startswith("/messages?"):
            return {"messages": [{"id": "a"}, {"id": "b"}]}
        mid = path.split("/")[2].split("?")[0]
        sender = "Axis Bank <x@axis.com>" if mid == "a" else "Other <o@x.com>"
        return {"payload": {
            "headers": [{"name": "From", "value": sender},
                        {"name": "Subject", "value": f"mail {mid}"},
                        {"name": "Date", "value": "d"}],
            "mimeType": "text/plain",
            "body": {"data": _b64("body " * 3000)},
        }}

    monkeypatch.setattr(gmail, "_api", fake_api)
    out = gmail._read("axis", 3)
    assert len(out) == 1 and "Axis Bank" in out[0]["from"]
    assert len(out[0]["body"]) <= gmail._BODY_CHAR_CAP


async def test_executor_keeps_bodies_out_of_cloud_prompts(monkeypatch):
    """The core §3a assertion: body text reaches ONLY the local tier's
    summarize call, and the user-facing reply short-circuits — the cloud
    loop transcript never sees content."""
    monkeypatch.setenv("KYRAAN_EMAIL_BODIES", "local")
    local_calls = []

    async def fake_run_tool(call):
        assert call.tool_name == "email.read"
        return [{"from": "Axis Bank <x@axis.com>", "subject": "Payment due",
                 "date": "d", "body": "SECRET-BODY: pay 4,200 by Friday"}]

    class _R:
        text = "Axis Bank says a payment of 4,200 is due Friday."

    async def fake_acall(prompt="", system="", tier="", **kw):
        local_calls.append((tier, prompt))
        return _R()

    monkeypatch.setattr(agent_loop.kernel, "run_tool", fake_run_tool)
    monkeypatch.setattr(agent_loop.router, "acall", fake_acall)
    monkeypatch.setattr(agent_loop.router, "provider_is_local", lambda p: True)

    result = await agent_loop._email_read(9, {"query": "axis"}, "what does the axis mail say")
    reply = result["__direct_reply__"]
    assert "4,200" in reply and "never left" in reply
    assert local_calls and all(t == "cheap" for t, _ in local_calls)
    assert "SECRET-BODY" in local_calls[0][1]   # the local model saw it


async def test_executor_refuses_when_cheap_tier_is_cloud(monkeypatch):
    monkeypatch.setenv("KYRAAN_EMAIL_BODIES", "local")
    monkeypatch.setattr(agent_loop.router, "provider_is_local", lambda p: False)
    result = await agent_loop._email_read(9, {}, "read my email")
    assert "LOCAL model" in result["__direct_reply__"]


async def test_unread_executor_delegates_body_questions_when_enabled(monkeypatch):
    """Live 2026-08-26: after the opt-in, 'what does my latest email say?'
    was still denied — the model called email.unread first and its
    hardcoded metadata denial short-circuited the turn. A body question
    with bodies enabled must delegate to email.read, whichever tool the
    model picked."""
    monkeypatch.setenv("KYRAAN_EMAIL_BODIES", "local")

    async def fake_run_tool(call):
        if call.tool_name == "email.unread":
            raise AssertionError("should have delegated before fetching metadata")
        return [{"from": "Axis <x@a.com>", "subject": "S", "date": "d",
                 "body": "the actual content"}]

    class _R:
        text = "summary of the actual content"

    async def fake_acall(prompt="", system="", tier="", **kw):
        return _R()

    monkeypatch.setattr(agent_loop.kernel, "run_tool", fake_run_tool)
    monkeypatch.setattr(agent_loop.router, "acall", fake_acall)
    monkeypatch.setattr(agent_loop.router, "provider_is_local", lambda p: True)
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)

    result = await agent_loop._email_unread(9, {"limit": 5}, "what does my latest email say?")
    assert "summary of the actual content" in result["__direct_reply__"]
    assert "never left" in result["__direct_reply__"]


def test_menu_gated_on_the_flag(monkeypatch):
    monkeypatch.delenv("KYRAAN_EMAIL_BODIES", raising=False)
    assert "- email.read {" not in agent_loop._tools_block()
    monkeypatch.setenv("KYRAAN_EMAIL_BODIES", "local")
    assert "- email.read {" in agent_loop._tools_block()


def test_brief_tracks_the_flag(monkeypatch):
    from kyraan.agents.capabilities import capability_brief
    for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
              "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("KYRAAN_EMAIL_BODIES", raising=False)
    brief = capability_brief()
    assert "senders and subjects ONLY" in brief
    assert "opening email bodies" in brief
    monkeypatch.setenv("KYRAAN_EMAIL_BODIES", "local")
    brief = capability_brief()
    assert "Email bodies are read by the local model" in brief and "never leave the machine" in brief
    assert "opening email bodies" not in brief
