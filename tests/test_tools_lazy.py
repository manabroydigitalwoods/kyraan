"""The two-tier tool catalogue (token audit 2026-09-04)."""
import asyncio

from kyraan.agents import agent_loop as al, loop_tools as lt


def test_hot_tools_carry_params_and_cold_ones_a_line():
    block = al._tools_block()
    assert "- home.get_state {" in block                   # hot: parameters shown
    assert "MORE TOOLS" in block
    assert "- rules.cancel:" in block and '- rules.cancel {"rule_id"' not in block   # cold: name + line
    assert len(block) < 16000                              # was ~20k chars
    assert "tools.describe" in al._hot_tools()


def test_describe_returns_the_full_spec():
    out = asyncio.run(lt._tools_describe(1, {"names": ["rules.cancel", "nope.tool"]}, ""))
    assert '- rules.cancel {"rule_id"' in out["tools"] and "nope.tool: no such tool" in out["tools"]
    assert "tools.describe" in lt._READ_ONLY_TOOLS
