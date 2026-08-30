"""Plan §3c items (adopted 2026-08-28, built 2026-08-31): taint-class
taxonomy, capability-contract metadata, normalized error names, and
memory.search_facts. These pin the DERIVATIONS the contracts rest on."""
import pytest

from kyraan.control_plane import taint
from kyraan.tools import registry


def test_every_taint_class_has_doctrine_and_sources_are_known():
    classes = {taint.WEB_UNTRUSTED, taint.EMAIL_UNTRUSTED, taint.BIOMETRIC,
               taint.PENDING_FACTS, taint.CONTACT_DATA}
    assert set(taint.DOCTRINE) == classes
    assert set(taint.SOURCE_CLASSES.values()) <= classes
    # the mechanisms the names formalize
    assert taint.source_class("web.search") == taint.WEB_UNTRUSTED
    assert taint.source_class("email.read") == taint.EMAIL_UNTRUSTED
    assert taint.source_class("faces.check_photo") == taint.BIOMETRIC
    assert taint.source_class("reminders.create") is None


def test_web_taint_rail_reads_the_class_map():
    # The agent loop's lockout keys off the map, not a hardcoded name —
    # a future untrusted-text source gets the rail by adding ONE line.
    import inspect

    from kyraan.agents import agent_loop
    src = inspect.getsource(agent_loop)
    assert "taint.source_class(tool) == taint.WEB_UNTRUSTED" in src


def test_contracts_derive_and_every_write_is_confirm_gated():
    contracts = registry.contracts()
    assert contracts  # non-empty
    for name, c in contracts.items():
        assert set(c) == {"effect", "risk", "requires_confirmation", "taint"}
        if c["effect"] == "write":
            # The invariant the whole derivation rests on: no write-effect
            # registry tool may ever be permission auto.
            assert c["requires_confirmation"], f"{name} writes without confirm"
            assert c["risk"] == "external_write"
    assert contracts["web.search"]["taint"] == taint.WEB_UNTRUSTED


@pytest.mark.parametrize("exc,name", [
    (registry.TransientToolError("connect timeout"), "TIMEOUT"),
    (registry.TransientToolError("connection reset"), "NETWORK"),
    (registry.ToolError("401 unauthorized"), "AUTH_REQUIRED"),
    (registry.ToolError("429 too many requests"), "RATE_LIMITED"),
    (registry.ToolError("event not found"), "NOT_FOUND"),
    (registry.ToolError("something odd"), "TOOL_FAILED"),
])
def test_error_names_normalize(exc, name):
    assert registry.error_name(exc) == name


async def test_search_facts_ranks_and_stays_honest(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.memory import engine

    entries = [
        {"content": "Kiaan studies at DPS Siliguri", "created": "2026-08-20T00:00:00", "_sim": 0.6},
        {"content": "Owner prefers tea", "created": "2026-08-01T00:00:00", "_sim": 0.1},
    ]
    monkeypatch.setattr(engine, "_pg_candidates", lambda q: entries)
    out = await loop_tools._memory_search_facts(7, {"query": "Kiaan school"}, "")
    assert out["matches"] == ["- Kiaan studies at DPS Siliguri (saved 2026-08-20)"]

    monkeypatch.setattr(engine, "_pg_candidates", lambda q: None)  # outage
    with pytest.raises(Exception, match="unreachable"):
        await loop_tools._memory_search_facts(7, {"query": "Kiaan school"}, "")

    monkeypatch.setattr(engine, "_pg_candidates", lambda q: [])
    out = await loop_tools._memory_search_facts(7, {"query": "zzz topic"}, "")
    assert out["matches"] == [] and "never invent" in out["note"]


def test_search_facts_is_read_only_and_not_in_frozen_stages():
    from kyraan.agents.loop_tools import _READ_ONLY_TOOLS
    assert "memory.search_facts" in _READ_ONLY_TOOLS
    import yaml
    stages = yaml.safe_load(open("config/permissions.yaml"))["stage_toolsets"]
    for stage, tools in stages.items():
        assert "memory.search_facts" not in tools  # owner-only for now
