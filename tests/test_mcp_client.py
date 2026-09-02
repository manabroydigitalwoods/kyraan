"""MCP client adapter (§3d #3, 2026-08-31): the stdio wire against a
REAL subprocess (tests/fake_mcp_server.py), the yaml-declared mount
path, generated menu entries, taint, and the frozen-surface posture."""
import sys

import pytest

from kyraan.tools import registry
from kyraan.tools.registry import MCPStdioAdapter, ToolError

FAKE = [sys.executable, "tests/fake_mcp_server.py"]


async def test_handshake_call_and_error_over_real_stdio():
    adapter = MCPStdioAdapter(FAKE)
    assert await adapter.call("shout", {"text": "hello"}) == "HELLO"
    with pytest.raises(ToolError, match="reported an error"):
        await adapter.call("nope", {})
    adapter._proc.kill()  # dead child respawns on the next call
    await adapter._proc.wait()
    assert await adapter.call("shout", {"text": "again"}) == "AGAIN"


async def test_env_reaches_the_child_process():
    adapter = MCPStdioAdapter(FAKE, env={"FAKE_KEY": "sk-fake-123"})
    assert await adapter.call("envcheck", {}) == "sk-fake-123"


def _mount(monkeypatch, **tool_over):
    from kyraan.control_plane import config
    cfg = config.load()
    cfg = {**cfg,
           "tool_servers": {**cfg["tool_servers"],
                            "fake": {"transport": "mcp-stdio",
                                     "command": FAKE,
                                     "untrusted": True}},
           "tools": {**cfg["tools"],
                     "fake.shout": {
                         "description": "Uppercase an input string.",
                         "server": "fake", "permission": "auto",
                         "side_effects": "read",
                         "params": {"text": {"type": "string",
                                             "required": True}},
                         "returns": "text",
                         "failure": {"retries": 1, "timeout_s": 10,
                                     "on_failure": "surface"},
                         **tool_over}}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    registry._adapter.cache_clear()
    return cfg


async def test_yaml_mounted_tool_runs_through_the_kernel(monkeypatch):
    from kyraan.control_plane import kernel
    _mount(monkeypatch)
    result = await kernel.run_tool(
        kernel.ToolCall("fake.shout", {"text": "kyraan"}))
    assert result == "KYRAAN"


def test_untrusted_server_taints_its_tools(monkeypatch):
    from kyraan.control_plane import taint
    _mount(monkeypatch)
    assert taint.source_class("fake.shout") == taint.WEB_UNTRUSTED
    assert taint.source_class("weather.get") is None


def test_registered_menu_entry_derives_from_the_declaration(monkeypatch):
    from kyraan.agents import loop_tools
    _mount(monkeypatch)
    try:
        loop_tools.register_mcp_tools()
        entry = loop_tools.TOOLS["fake.shout"]
        assert "Uppercase" in entry["about"]
        assert "untrusted external text" in entry["about"]
        assert "fake.shout" in loop_tools._READ_ONLY_TOOLS
    finally:
        loop_tools.TOOLS.pop("fake.shout", None)
        loop_tools._READ_ONLY_TOOLS.discard("fake.shout")


async def test_write_mcp_tool_is_confirm_gated_and_undo_explicit(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    _mount(monkeypatch, permission="confirm", side_effects="write")
    try:
        loop_tools.register_mcp_tools()
        with pytest.raises(kernel.ConfirmationRequired):
            await loop_tools.TOOLS["fake.shout"]["run"](7, {"text": "x"}, "")
        assert loop_tools.UNDO_MAP["fake.shout"]({}, {}, None) is None
    finally:
        loop_tools.TOOLS.pop("fake.shout", None)
        loop_tools.UNDO_MAP.pop("fake.shout", None)


def test_auto_write_mcp_tool_is_refused_at_load(monkeypatch):
    _mount(monkeypatch, permission="auto", side_effects="write")
    with pytest.raises(ValueError, match="auto write tools are forbidden"):
        registry.load()


def test_mounted_tools_join_no_stage_toolset(monkeypatch):
    from kyraan.control_plane import kernel
    _mount(monkeypatch)
    assert not kernel.stage_allows("fake.shout", stage="full")
    assert not kernel.stage_allows("fake.shout", stage="read_mostly")


def test_env_placeholders_resolve_from_the_process_environment(monkeypatch):
    from kyraan.control_plane import config
    monkeypatch.setenv("FAKE_SLACK_TOKEN", "xoxp-real")
    cfg = config.load()
    cfg = {**cfg, "tool_servers": {**cfg["tool_servers"], "envtest": {
        "transport": "mcp-stdio", "command": FAKE,
        "env": {"TOKEN": "${FAKE_SLACK_TOKEN}", "PLAIN": "x"}}}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    registry._adapter.cache_clear()
    adapter = registry._adapter("envtest")
    assert adapter._env == {"TOKEN": "xoxp-real", "PLAIN": "x"}
    registry._adapter.cache_clear()


def test_slack_mount_declares_the_house_shape():
    from kyraan.agents import loop_tools
    specs = registry.load()
    assert specs["slack.post"].permission == "confirm"
    assert specs["slack.history"].mcp_name == "conversations_history"
    from kyraan.control_plane import taint
    assert taint.source_class("slack.history") == taint.WEB_UNTRUSTED
    assert "slack.history" in loop_tools.TOOLS          # menu generated
    assert "slack.history" in loop_tools._READ_ONLY_TOOLS
    from kyraan.control_plane import kernel
    assert not kernel.stage_allows("slack.history", stage="full")


async def test_adapter_resolves_executable_on_augmented_path(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")     # a launchd-like PATH
    adapter = MCPStdioAdapter(["python3", "tests/fake_mcp_server.py"])
    # python3 must resolve via the augmented search even with PATH gutted
    import shutil
    if shutil.which("python3", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin") is None:
        pytest.skip("no python3 on the augmented path here")
    assert await adapter.call("shout", {"text": "path"}) == "PATH"
    bad = MCPStdioAdapter(["definitely-not-a-binary-xyz"])
    with pytest.raises(ToolError, match="not found on PATH"):
        await bad.call("shout", {})
