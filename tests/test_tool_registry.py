"""Registry load-validation and kernel.run_tool gating — the Phase 2 tool
layer. All adapters are faked; nothing touches the network."""
import asyncio
import sys
import types

import pytest

from kyraan.control_plane import config, kernel, kill_switch
from kyraan.tools import registry


def _cfg(tools: dict, servers: dict | None = None) -> dict:
    base = config.load()
    return {**base, "tools": tools, "tool_servers": servers if servers is not None else {"fake": {"transport": "builtin", "module": "fake_adapter"}}}


@pytest.fixture
def patched_cfg(monkeypatch):
    """Returns a setter that swaps in a tools/tool_servers config."""
    def set_config(tools, servers=None):
        cfg = _cfg(tools, servers)
        monkeypatch.setattr(config, "load", lambda: cfg)
        registry._adapter_module.cache_clear()
    yield set_config
    registry._adapter_module.cache_clear()


@pytest.fixture
def fake_adapter(monkeypatch):
    """Installs an importable fake adapter module; returns its call log."""
    calls = []
    module = types.ModuleType("fake_adapter")

    async def call(tool_name, args):
        calls.append((tool_name, args))
        return {"ok": True, "tool": tool_name}

    module.call = call
    monkeypatch.setitem(sys.modules, "fake_adapter", module)
    return calls


def _tool(permission="auto", side_effects="read", **over):
    entry = {
        "description": "t", "server": "fake", "permission": permission,
        "side_effects": side_effects,
        "params": {"x": {"type": "string", "required": True}},
        "failure": {"retries": 0, "timeout_s": 5, "on_failure": "surface"},
    }
    entry.update(over)
    return entry


# --- load-time validation ---

def test_write_tool_with_auto_permission_fails_load(patched_cfg):
    patched_cfg({"t.write": _tool(permission="auto", side_effects="write")})
    with pytest.raises(ValueError, match="auto write tools are forbidden"):
        registry.load()


def test_write_tool_with_confirm_or_disabled_loads(patched_cfg):
    patched_cfg({
        "t.write": _tool(permission="confirm", side_effects="write"),
        "t.parked": _tool(permission="disabled", side_effects="write"),
    })
    assert set(registry.load()) == {"t.write", "t.parked"}


def test_unknown_server_fails_load(patched_cfg):
    patched_cfg({"t.x": _tool(server="nope")})
    with pytest.raises(ValueError, match="unknown server"):
        registry.load()


def test_silent_failure_only_for_notify_tools(patched_cfg):
    patched_cfg({"t.read": _tool(failure={"on_failure": "silent"})})
    with pytest.raises(ValueError, match="silent"):
        registry.load()


def test_fallback_must_target_a_registered_tool(patched_cfg):
    patched_cfg({"t.a": _tool(failure={"on_failure": "fallback:t.missing"})})
    with pytest.raises(ValueError, match="fallback target"):
        registry.load()


# --- kernel.run_tool gating + policy ---

async def test_run_tool_validates_args_before_the_adapter(patched_cfg, fake_adapter):
    patched_cfg({"t.read": _tool()})
    with pytest.raises(kernel.ToolFailed, match="missing required"):
        await kernel.run_tool(kernel.ToolCall("t.read", {}))
    with pytest.raises(kernel.ToolFailed, match="unexpected parameter"):
        await kernel.run_tool(kernel.ToolCall("t.read", {"x": "ok", "evil": 1}))
    assert fake_adapter == []  # adapter never touched by an invalid call


async def test_run_tool_happy_path_dispatches_and_logs(patched_cfg, fake_adapter):
    patched_cfg({"t.read": _tool()})
    result = await kernel.run_tool(kernel.ToolCall("t.read", {"x": "hello"}))
    assert result == {"ok": True, "tool": "t.read"}
    assert fake_adapter == [("t.read", {"x": "hello"})]


async def test_confirm_tool_without_approval_raises(patched_cfg, fake_adapter):
    patched_cfg({"t.write": _tool(permission="confirm", side_effects="write")})
    with pytest.raises(kernel.ConfirmationRequired):
        await kernel.run_tool(kernel.ToolCall("t.write", {"x": "y"}))
    assert fake_adapter == []


async def test_confirmed_skill_context_satisfies_confirm_tool(patched_cfg, fake_adapter, monkeypatch):
    """A confirm tool inside a skill the user already confirmed must not
    re-prompt — one intent, one yes."""
    patched_cfg({"t.write": _tool(permission="confirm", side_effects="write")})
    monkeypatch.setattr(kernel.config, "skill_config", lambda name: {"permission": "confirm", "model_tier": "cheap"})

    async def handler(args):
        return await kernel.run_tool(kernel.ToolCall("t.write", {"x": "y"}))

    result = await kernel.run_skill(kernel.SkillCall("s.write", {}, confirmed=True), handler)
    assert result == {"ok": True, "tool": "t.write"}


async def test_disabled_tool_refuses(patched_cfg, fake_adapter):
    patched_cfg({"t.parked": _tool(permission="disabled", side_effects="write")})
    with pytest.raises(kernel.ToolFailed, match="disabled"):
        await kernel.run_tool(kernel.ToolCall("t.parked", {"x": "y"}))


async def test_kill_switch_blocks_tools(patched_cfg, fake_adapter):
    patched_cfg({"t.read": _tool()})
    kill_switch.engage("test")
    try:
        with pytest.raises(kernel.KillSwitchEngaged):
            await kernel.run_tool(kernel.ToolCall("t.read", {"x": "y"}))
    finally:
        kill_switch.disengage()
    assert fake_adapter == []


async def test_transient_errors_retry_then_surface(patched_cfg, monkeypatch):
    patched_cfg({"t.read": _tool(failure={"retries": 2, "timeout_s": 5, "on_failure": "surface"})})
    attempts = []

    async def flaky(spec, args):
        attempts.append(1)
        raise registry.TransientToolError("blip")

    monkeypatch.setattr(registry, "dispatch", flaky)
    with pytest.raises(kernel.ToolFailed, match="blip"):
        await kernel.run_tool(kernel.ToolCall("t.read", {"x": "y"}))
    assert len(attempts) == 3  # initial + 2 retries


async def test_non_transient_errors_do_not_retry(patched_cfg, monkeypatch):
    patched_cfg({"t.read": _tool(failure={"retries": 2, "timeout_s": 5, "on_failure": "surface"})})
    attempts = []

    async def broken(spec, args):
        attempts.append(1)
        raise registry.ToolError("bad credential")

    monkeypatch.setattr(registry, "dispatch", broken)
    with pytest.raises(kernel.ToolFailed, match="bad credential"):
        await kernel.run_tool(kernel.ToolCall("t.read", {"x": "y"}))
    assert len(attempts) == 1


async def test_fallback_runs_once_and_never_chains(patched_cfg, monkeypatch):
    """A -> B on failure; B (whose own policy is fallback:A) failing must
    surface, not loop back to A."""
    patched_cfg({
        "t.a": _tool(failure={"on_failure": "fallback:t.b"}),
        "t.b": _tool(failure={"on_failure": "fallback:t.a"}),
    })
    called = []

    async def always_fail(spec, args):
        called.append(spec.name)
        raise registry.ToolError("down")

    monkeypatch.setattr(registry, "dispatch", always_fail)
    with pytest.raises(kernel.ToolFailed):
        await kernel.run_tool(kernel.ToolCall("t.a", {"x": "y"}))
    assert called == ["t.a", "t.b"]  # exactly one fallback hop


async def test_timeout_counts_as_transient(patched_cfg, monkeypatch):
    patched_cfg({"t.read": _tool(failure={"retries": 1, "timeout_s": 0.05, "on_failure": "surface"})})
    attempts = []

    async def slow(spec, args):
        attempts.append(1)
        await asyncio.sleep(1)

    monkeypatch.setattr(registry, "dispatch", slow)
    with pytest.raises(kernel.ToolFailed):
        await kernel.run_tool(kernel.ToolCall("t.read", {"x": "y"}))
    assert len(attempts) == 2


# --- the real shipped config must satisfy its own validator ---

def test_shipped_permissions_yaml_passes_validation():
    specs = registry.load()
    assert "calendar.list_events" in specs
    assert specs["calendar.list_events"].side_effects == "read"
