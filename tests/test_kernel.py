import pytest

from kyraan.control_plane import kill_switch
from kyraan.control_plane.kernel import ConfirmationRequired, KillSwitchEngaged, SkillCall, run_skill


@pytest.mark.asyncio
async def test_auto_skill_runs_without_confirmation():
    async def handler(args):
        return "ok"

    result = await run_skill(SkillCall("reminders.create", {}), handler)
    assert result == "ok"


@pytest.mark.asyncio
async def test_confirm_skill_blocks_without_confirmation():
    async def handler(args):
        return "should not run"

    with pytest.raises(ConfirmationRequired):
        await run_skill(SkillCall("memory.write", {}), handler)


@pytest.mark.asyncio
async def test_kill_switch_blocks_everything():
    async def handler(args):
        return "should not run"

    kill_switch.engage("test")
    try:
        with pytest.raises(KillSwitchEngaged):
            await run_skill(SkillCall("reminders.create", {}), handler)
    finally:
        kill_switch.disengage()
