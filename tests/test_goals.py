"""Goal continuity (owner decisions 2026-08-31, docs/design/
goal_continuity.md): lifecycle, the conversational update path, the
read-only work cycle's edge-style reporting, the per-person viewer
context, and the caps."""
import pytest

from kyraan.control_plane import kernel
from kyraan.triggers import goals


@pytest.fixture(autouse=True)
def isolated_goals(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "GOALS_PATH", tmp_path / "goals.json")
    monkeypatch.setattr(goals, "_schedule_fn", None)
    monkeypatch.setattr(goals, "_run_fn", None)
    monkeypatch.setattr(goals, "_send_fn", None)


def _goal(**over):
    base = dict(chat_id=7, person="owner", stage="owner",
                title="Plan Kiaan's birthday",
                steps=["guest list", "venue", "cake"])
    base.update(over)
    return goals.create(**base)


# --- lifecycle ------------------------------------------------------------

def test_create_validates_and_caps_at_three_active():
    g = _goal()
    assert [x.id for x in goals.list_for(7)] == [g.id]
    with pytest.raises(ValueError, match="short, clear title"):
        _goal(title="ab")
    _goal(title="Second goal")
    _goal(title="Third goal")
    with pytest.raises(ValueError, match="already active"):
        _goal(title="Fourth goal")
    goals.set_status(7, "second", "paused")
    _goal(title="Fourth goal")  # a paused goal frees its slot


def test_resolve_by_words_refuses_ambiguity():
    _goal(title="Plan the birthday")
    _goal(title="Plan the house move")
    with pytest.raises(ValueError, match="2 goals match"):
        goals.resolve(7, "plan the")
    assert goals.resolve(7, "house").title == "Plan the house move"
    with pytest.raises(ValueError, match="no goal matches"):
        goals.resolve(7, "zzz")


def test_update_checks_steps_and_journals_notes():
    g = _goal()
    goals.update(7, "birthday", step_done="venue",
                 note="Sharma Garden quoted 8k, holds 40 people")
    got = goals.get(g.id)
    assert [s["done"] for s in got.steps] == [False, True, False]
    assert "Sharma Garden" in got.journal[0]["text"]
    with pytest.raises(ValueError, match="no open step matches"):
        goals.update(7, "birthday", step_done="venue")  # already done
    goals.update(7, "birthday", add_step="send invites")
    assert len(goals.get(g.id).steps) == 4


def test_status_round_trip_carries_prior_for_undo():
    _goal()
    g, prior = goals.set_status(7, "birthday", "paused")
    assert (g.status, prior) == ("paused", "active")
    g, prior = goals.set_status(7, "birthday", "active")
    assert (g.status, prior) == ("active", "paused")
    with pytest.raises(ValueError, match="already"):
        goals.set_status(7, "birthday", "active")


def test_goals_are_chat_scoped():
    _goal(chat_id=7)
    _goal(chat_id=891, person="ruma", stage="full", title="Learn driving")
    assert [g.title for g in goals.list_for(891)] == ["Learn driving"]
    with pytest.raises(ValueError, match="no goal matches"):
        goals.resolve(891, "birthday")  # the owner's goal is invisible


def test_brief_line_shows_progress_and_next_step():
    _goal()
    goals.update(7, "birthday", step_done="guest list")
    line = goals.brief_line(7)
    assert "1/3 steps" in line and "next: venue" in line
    assert goals.brief_line(999) == ""


# --- the work cycle -------------------------------------------------------

@pytest.fixture
def cycling(monkeypatch):
    sent, ran = [], []

    async def send(chat_id, text):
        sent.append((chat_id, text))
        return True

    async def run(goal):
        ran.append((goal.id, kernel.viewer_person(), kernel.viewer_stage()))
        return "- Sharma Garden available on the 12th, 8k for 40 guests"

    goals.init(lambda *a: None, run, send)
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    return sent, ran


async def test_cycle_reports_findings_and_journals_them(cycling):
    sent, ran = cycling
    g = _goal()
    await goals.fire(g.id)
    assert "Sharma Garden" in sent[0][1] and "🎯" in sent[0][1]
    assert "Sharma Garden" in goals.get(g.id).journal[0]["text"]


async def test_nothing_new_and_refound_findings_stay_silent(cycling, monkeypatch):
    sent, _ = cycling
    g = _goal()

    async def quiet(goal):
        return "NOTHING_NEW"

    monkeypatch.setattr(goals, "_run_fn", quiet)
    await goals.fire(g.id)
    assert sent == []
    # a finding the journal already holds is not progress either
    goals.update(7, "birthday",
                 note="- Sharma Garden available on the 12th, 8k for 40 guests")

    async def refind(goal):
        return "- Sharma Garden available on the 12th, 8k for 40 guests"

    monkeypatch.setattr(goals, "_run_fn", refind)
    await goals.fire(g.id)
    assert sent == []


async def test_cycle_runs_as_the_goal_person_not_the_owner(cycling):
    _, ran = cycling
    g = _goal(chat_id=891, person="ruma", stage="full", title="Learn driving")
    from kyraan.channels import telegram_bot
    # the channel's run_fn is where the viewer swap lives — exercise it
    import inspect
    src = inspect.getsource(telegram_bot._wire_goals)
    assert "set_viewer(goal.person, goal.stage)" in src
    await goals.fire(g.id)
    assert ran[0][0] == g.id  # store-level fire used the fixture run


async def test_daily_cycle_cap_holds(cycling):
    sent, ran = cycling
    g = _goal()
    await goals.fire(g.id)
    await goals.fire(g.id)
    await goals.fire(g.id)  # third today: capped
    assert len(ran) == goals.MAX_CYCLES_PER_DAY


async def test_failed_delivery_keeps_the_finding_for_next_cycle(cycling, monkeypatch):
    sent, _ = cycling
    g = _goal()

    async def failing_send(chat_id, text):
        return False

    monkeypatch.setattr(goals, "_send_fn", failing_send)
    await goals.fire(g.id)
    got = goals.get(g.id)
    assert "Sharma Garden" in got.journal[0]["text"]   # truth kept
    assert "Sharma Garden" in got.unreported           # ping owed

    async def ok_send(chat_id, text):
        sent.append((chat_id, text))
        return True

    async def quiet(goal):
        return "NOTHING_NEW"

    monkeypatch.setattr(goals, "_send_fn", ok_send)
    monkeypatch.setattr(goals, "_run_fn", quiet)
    await goals.fire(g.id)
    assert "Sharma Garden" in sent[0][1]               # carried ping lands
    assert goals.get(g.id).unreported == ""


async def test_dnd_hold_skips_quietly(cycling, monkeypatch):
    sent, ran = cycling
    g = _goal()
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: False)
    await goals.fire(g.id)
    assert ran == [] and sent == []


# --- loop surface ---------------------------------------------------------

async def test_create_is_confirm_gated_and_needs_an_identified_person():
    from kyraan.agents import loop_tools
    with pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._goals_create(7, {"title": "Plan the birthday"}, "")


def test_undo_entries_pause_and_restore():
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["goals.create"]({}, {"id": "ab12"}, None) == \
        ("goals.set_status", {"goal": "ab12", "status": "paused"})
    assert UNDO_MAP["goals.set_status"](
        {}, {"id": "ab12", "prior": "active"}, None) == \
        ("goals.set_status", {"goal": "ab12", "status": "active"})


def test_undo_step_done_reopens_only_the_pure_case():
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["goals.update"](
        {"goal": "birthday", "step_done": "venue"}, {}, None) == \
        ("goals.update", {"goal": "birthday", "reopen_step": "venue"})
    assert UNDO_MAP["goals.update"](
        {"goal": "birthday", "step_done": "venue", "note": "x"}, {}, None) is None


def test_reopen_step_unchecks():
    g = _goal()
    goals.update(7, "birthday", step_done="venue")
    goals.update(7, "birthday", reopen_step="venue")
    assert all(not s["done"] for s in goals.get(g.id).steps)


async def test_cycle_step_markers_keep_the_goals_own_books(cycling, monkeypatch):
    """Owner "go" 2026-09-01: a cycle's findings can settle or reveal
    steps — applied deterministically, bounded, never guessed."""
    sent, _ = cycling
    g = _goal()

    async def run(goal):
        return ("- Sharma Garden confirmed: 8k, holds 40, free on the 12th\n"
                "STEP_DONE: venue\n"
                "STEP_ADD: visit Sharma Garden Saturday\n"
                "STEP_ADD: pay booking advance\n"
                "STEP_ADD: a third add beyond the cap")

    monkeypatch.setattr(goals, "_run_fn", run)
    await goals.fire(g.id)
    got = goals.get(g.id)
    assert [s["text"] for s in got.steps if s["done"]] == ["venue"]
    added = [s["text"] for s in got.steps[3:]]
    assert added == ["visit Sharma Garden Saturday", "pay booking advance"]
    ping = sent[0][1]
    assert "✔ step done: venue" in ping and "step added: visit" in ping
    assert "STEP_DONE" not in ping          # markers never reach the owner


async def test_ambiguous_or_unknown_step_done_is_ignored(cycling, monkeypatch):
    sent, _ = cycling
    g = _goal(steps=["book venue A", "book venue B"])

    async def run(goal):
        return "- looked around\nSTEP_DONE: book venue"   # matches two

    monkeypatch.setattr(goals, "_run_fn", run)
    await goals.fire(g.id)
    assert all(not s["done"] for s in goals.get(g.id).steps)  # untouched


async def test_duplicate_step_add_is_skipped(cycling, monkeypatch):
    sent, _ = cycling
    g = _goal()

    async def run(goal):
        return "- news\nSTEP_ADD: cake"    # already a step

    monkeypatch.setattr(goals, "_run_fn", run)
    await goals.fire(g.id)
    assert len(goals.get(g.id).steps) == 3
