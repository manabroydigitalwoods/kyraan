"""P3.5b — per-stage tool scoping, both layers: the menu never shows an
out-of-scope tool, and the kernel refuses one however the model asks —
the read_mostly AC probe is the ticket's Done-when."""
import json

import pytest

from kyraan.agents import agent_loop
from kyraan.control_plane import kernel


@pytest.fixture
def read_mostly(monkeypatch):
    token = kernel.set_viewer_stage("read_mostly")
    yield
    kernel.reset_viewer_stage(token)


# --- layer 1: the menu ----------------------------------------------------

def test_owner_menu_is_unchanged():
    menu = agent_loop._tools_block(stage="owner")
    for name in ("home.turn_on", "email.unread", "memory.forget",
                 "tasks.schedule", "reminders.create"):
        assert name in menu


def test_read_mostly_menu_scopes_down():
    menu = agent_loop._tools_block(stage="read_mostly")
    for allowed in ("reminders.create", "reminders.list", "calendar.list_events",
                    "weather.get", "places.nearby"):
        assert allowed in menu
    for denied in ("home.turn_on", "home.turn_off", "home.get_state",
                   "email.unread", "email.read", "memory.forget",
                   "memory.recall_episodes", "memory.relations",
                   "tasks.schedule", "faces.remember", "usage.report",
                   "calendar.create_event", "calendar.delete_event"):
        assert denied not in menu, denied


def test_unknown_stage_gets_no_tools():
    assert agent_loop._tools_block(stage="mystery") == ""


# --- layer 2: the kernel wall ---------------------------------------------

async def test_kernel_refuses_out_of_scope_tool(read_mostly):
    with pytest.raises(kernel.ToolFailed, match="access level"):
        await kernel.run_tool(kernel.ToolCall("home.turn_on",
                                              {"entity": "switch.ac"}))


async def test_kernel_refuses_out_of_scope_skill(read_mostly):
    async def handler(_a):
        raise AssertionError("an out-of-scope skill must never execute")

    with pytest.raises(kernel.ToolFailed, match="access level"):
        await kernel.run_skill(kernel.SkillCall("memory.forget", {"fact": "x"}),
                               handler)


async def test_in_scope_skill_still_runs(read_mostly):
    async def handler(_a):
        return "answered"

    result = await kernel.run_skill(kernel.SkillCall("qa.answer", {}), handler)
    assert result == "answered"


def test_internal_paths_stay_unscoped():
    # scheduled runs / scripts never set the contextvar → owner scope
    assert kernel.viewer_stage() == "owner"
    assert kernel.stage_allows("home.turn_on")


# --- the Done-when: read_mostly cannot switch the AC by ANY phrasing ------

async def test_read_mostly_cannot_switch_the_ac(read_mostly, monkeypatch):
    """Even a model that IGNORES the menu and calls home.turn_on gets the
    kernel wall, and the reply carries the honest refusal."""
    decisions = [
        {"action": "call", "tool": "home.turn_on",
         "args": {"entity": "switch.ac"}, "consider": "user asked"},
        {"action": "reply", "consider": "refused by scope",
         "text": "I can't switch the AC at your access level."},
    ]

    async def fake_acall(**kwargs):
        class R:
            text = json.dumps(decisions.pop(0))
        return R()

    monkeypatch.setattr(agent_loop.router, "acall", fake_acall)
    switched = []
    monkeypatch.setitem(agent_loop.TOOLS["home.turn_on"], "run",
                        lambda *a, **k: switched.append(a))
    from kyraan.agents import orchestrator
    reply = await agent_loop.run(5, "switch on the AC please", tier="cheap")
    assert switched == []  # the executor never ran
    assert "access level" in reply or "can't switch" in reply