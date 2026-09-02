"""Path-independent orchestrator invariants, ported out of the
classifier-era test_orchestrator.py at its deletion (P3.7b): the
deterministic guards, the confirm flow, the review flow, seeding
redaction, and handle_message's decoration layer — everything here
survives the classifier because it never depended on it."""
import asyncio
import json
import time

import pytest

from kyraan.agents import agent_loop, orchestrator


@pytest.fixture
def loop_reply(monkeypatch):
    """handle_message with the agent loop mocked to a fixed reply."""
    calls = []

    def set_reply(text="ok."):
        async def fake_run(chat_id, raw_text, tier):
            calls.append((raw_text, tier))
            return text

        monkeypatch.setattr(agent_loop, "run", fake_run)
        return calls

    return set_reply


# --- deterministic guards -------------------------------------------------

async def test_bare_time_phrase_is_deterministically_patient(loop_reply):
    loop_reply()
    reply = await orchestrator._dispatch(950_001, "at 9pm")
    assert reply == "Go on — I'm listening…"


def test_time_fragment_detector_boundaries():
    assert orchestrator.is_time_fragment("at 9pm")
    assert orchestrator.is_time_fragment("tomorrow at 7")
    assert not orchestrator.is_time_fragment("remind me to call mom at 9pm")
    assert not orchestrator.is_time_fragment("what time is it?")


def test_thought_open_reads_message_shape():
    assert orchestrator.thought_open("remind me to")
    assert orchestrator.thought_open("set a")
    assert not orchestrator.thought_open("what's the weather in Kolkata?")


def test_meta_detection_covers_complaints_and_questions():
    assert orchestrator._is_meta_question("are these the latest emails?")
    assert not orchestrator._is_meta_question("any new emails?")


def test_review_decision_parser_boundaries():
    assert orchestrator._parse_review_decision("approve all", 3) == ([0, 1, 2], [])
    assert orchestrator._parse_review_decision("approve 1 reject 2", 3) == ([0], [1])
    assert orchestrator._parse_review_decision("maybe later", 3) is None


# --- the confirm flow -----------------------------------------------------

async def _stash_ask(chat_id):
    from kyraan.control_plane import kernel

    async def gate(_a):
        raise kernel.ConfirmationRequired("home.control", {"entity": "switch.ac"})

    return await orchestrator._gated(
        chat_id, kernel.SkillCall("home.control", {}), gate,
        describe="About to turn the AC ON")


async def test_stale_confirmation_expires_instead_of_executing(loop_reply, monkeypatch):
    loop_reply()
    chat_id = 950_010
    await _stash_ask(chat_id)
    call, handler, _ = orchestrator._pending_confirmations[chat_id]
    orchestrator._pending_confirmations[chat_id] = (
        call, handler, time.monotonic() - orchestrator._CONFIRMATION_TTL_S - 1)
    reply = await orchestrator._dispatch(chat_id, "yes")
    assert "expired" in reply
    assert chat_id not in orchestrator._pending_confirmations


async def test_fresh_confirmation_still_works_within_ttl(monkeypatch):
    from kyraan.control_plane import kernel
    chat_id = 950_011
    ran = []

    async def gate(_a):
        if not kernel.confirmed_context():
            raise kernel.ConfirmationRequired("home.control", {})
        ran.append(True)
        return "The AC is ON."

    await orchestrator._gated(chat_id, kernel.SkillCall("home.control", {}),
                              gate, describe="About to turn the AC ON")
    reply = await orchestrator._dispatch(chat_id, "yes")
    assert ran and "AC is ON" in reply


_ASK = 'About to turn the AC ON — reply "yes" to confirm or "no" to cancel.'


async def test_orphaned_yes_after_restart_is_honest(loop_reply):
    loop_reply()
    chat_id = 950_012
    orchestrator._history[chat_id].append(("assistant", _ASK))
    reply = await orchestrator._dispatch(chat_id, "yes")
    assert "no longer pending" in reply


async def test_orphaned_ok_behind_a_proactive_is_honest(loop_reply):
    # Found live 2026-08-27: a temp alert landed between the ask and the
    # owner's "ok", hiding the ask from the single-newest-message check;
    # "ok" fell to the loop as small talk.
    loop_reply()
    chat_id = 950_014
    orchestrator._history[chat_id].append(("assistant", _ASK))
    orchestrator._history[chat_id].append(
        ("assistant", "Bedroom temperature is above 27°C"))
    reply = await orchestrator._dispatch(chat_id, "ok")
    assert "no longer pending" in reply


async def test_ok_after_resolved_ask_is_a_plain_ack(loop_reply):
    # An ask FOLLOWED by "Done — ..." is settled: a casual "ok" must not
    # trigger a false "that ask expired" (it gets the bare-ack reply).
    loop_reply()
    chat_id = 950_015
    orchestrator._history[chat_id].append(("assistant", _ASK))
    orchestrator._history[chat_id].append(("assistant", "Done — the ac is on."))
    reply = await orchestrator._dispatch(chat_id, "ok")
    assert reply == "👍"


async def test_bare_ack_never_reaches_the_loop(loop_reply):
    calls = loop_reply()
    chat_id = 950_016
    reply = await orchestrator._dispatch(chat_id, "thanks")
    assert reply == "👍"
    assert not calls


async def test_dropped_ask_is_noted_for_the_next_reply(loop_reply):
    loop_reply()
    chat_id = 950_013
    await _stash_ask(chat_id)
    await orchestrator._dispatch(chat_id, "what's the weather?")  # moved on
    assert orchestrator._dropped_ask_note.get(chat_id) == "home.control"
    assert chat_id not in orchestrator._pending_confirmations


# --- the review flow ------------------------------------------------------

async def test_review_lists_then_mixed_decision_promotes_and_rejects(loop_reply):
    loop_reply()
    from kyraan.memory import store as memory_store
    chat_id = 950_020
    memory_store.propose_fact("preferences/a.md", "Fact alpha", "said a")
    memory_store.propose_fact("preferences/b.md", "Fact beta", "said b")
    listing = await orchestrator._dispatch(chat_id, "review memory")
    assert "Fact alpha" in listing and "Fact beta" in listing
    # same-second filenames order by uuid — read alpha's number from the list
    alpha_n = next(line[0] for line in listing.splitlines()
                   if "Fact alpha" in line)
    beta_n = "1" if alpha_n == "2" else "2"
    verdict = await orchestrator._dispatch(
        chat_id, f"approve {alpha_n} reject {beta_n}")
    assert "Fact alpha" in verdict and "Rejected" in verdict
    from kyraan.memory import engine
    active = [e["content"] for e in engine.active_entries()]
    assert "Fact alpha" in active and "Fact beta" not in active


async def test_unrelated_reply_leaves_the_review_queue_untouched(loop_reply):
    loop_reply()
    from kyraan.memory import store as memory_store
    chat_id = 950_021
    memory_store.propose_fact("preferences/c.md", "Fact gamma", "said c")
    await orchestrator._dispatch(chat_id, "review memory")
    await orchestrator._dispatch(chat_id, "what's the weather?")
    assert len(list(memory_store.PENDING_DIR.glob("*.md"))) == 1


# --- seeding redaction ----------------------------------------------------

def test_pre_upgrade_email_logs_are_redacted_at_seed(tmp_path, monkeypatch):
    from kyraan.control_plane import logging_setup
    log = tmp_path / "chat.jsonl"
    log.write_text(json.dumps({
        "ts": "2026-08-26T05:00:00+00:00", "chat_id": 7,
        "role": "assistant",
        "text": "You have about 4 unread emails. Latest unread:\n- Bank: secret"}) + "\n")
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    from kyraan.agents import session
    monkeypatch.setattr(session, "_history",
                        session._PerChat(session._ChatHistory))
    session.seed_history_from_log()
    entries = list(session._history[7])
    assert entries == [("assistant", "[showed the unread email summary]")]


def test_history_block_tiers_old_entries(loop_reply):
    chat_id = 950_030
    for i in range(20):
        orchestrator._history[chat_id].append(("user", "x" * 500))
    block = orchestrator._history_block(chat_id, clip=600, older_clip=100)
    lines = block.splitlines()
    assert len(lines[0]) < 120      # old entries clipped tight
    assert len(lines[-1]) > 400     # recent entries keep full clip


# --- handle_message decoration --------------------------------------------

async def test_noted_for_review_line_rides_the_reply(loop_reply, monkeypatch):
    loop_reply("Nice choice!")

    async def fake_note(chat_id, raw_text):
        return "\n\n📝 Noted for review: Favourite snack is murukku"

    monkeypatch.setattr(orchestrator, "_extraction_note", fake_note)
    reply = await orchestrator.handle_message(950_040, "my favourite snack is murukku")
    assert reply.startswith("Nice choice!")
    assert "Noted for review" in reply


async def test_extraction_failure_never_breaks_the_reply(loop_reply, monkeypatch):
    loop_reply("Answer stands.")

    async def boom(chat_id, raw_text):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", boom)
    reply = await orchestrator.handle_message(950_041, "a long enough statement here")
    assert reply.startswith("Answer stands.")


async def test_short_messages_skip_extraction_entirely(loop_reply, monkeypatch):
    loop_reply("Hi!")
    called = []
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message",
                        lambda *a, **k: called.append(1))
    reply = await orchestrator.handle_message(950_042, "hey")
    assert reply.startswith("Hi!") and called == []


async def test_statement_matching_a_saved_fact_says_already(loop_reply, monkeypatch):
    loop_reply("Good taste!")
    from kyraan.memory import engine
    engine.add_fact("Favourite snack is murukku", "preferences/snack.md", "t")

    async def nothing(*a, **k):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", nothing)
    reply = await orchestrator.handle_message(950_043, "my favourite snack is murukku")
    assert "already have that saved" in reply


async def test_all_tiers_failing_is_an_honest_outage(monkeypatch):
    async def fake_run(chat_id, raw_text, tier):
        raise agent_loop.AgentUnavailable(f"{tier} down")

    monkeypatch.setattr(agent_loop, "run", fake_run)
    reply = await orchestrator._dispatch(950_044, "what's the weather?")
    assert "nothing was done" in reply.lower()
    assert "unreachable" in reply.lower()

async def test_time_fragment_answering_a_question_reaches_the_loop(loop_reply):
    # Seen live 2026-08-27: "what time on 30 Aug should I remind you?"
    # -> "at 5am" -> "Go on — I'm listening…" and no reminder was ever
    # created. A fragment answering Kyraan's own question is complete.
    calls = loop_reply("Done — reminder set for 5:00 AM.")
    chat_id = 950_017
    orchestrator._history[chat_id].append(
        ("assistant", "Sure — what time on 30 Aug should I remind you "
                      "about Kiaan's vaccination (e.g., 10:00 AM)?"))
    orchestrator._history[chat_id].append(
        ("assistant", "Reminder: Drink water"))  # proactive in between
    reply = await orchestrator._dispatch(chat_id, "at 5am")
    assert calls and calls[0][0] == "at 5am"
    assert "5:00 AM" in reply


async def test_unprompted_time_fragment_still_waits(loop_reply):
    calls = loop_reply()
    chat_id = 950_018
    orchestrator._history[chat_id].append(
        ("assistant", "Done — the ac is off."))
    reply = await orchestrator._dispatch(chat_id, "at 5am")
    assert reply == "Go on — I'm listening…"
    assert not calls


def test_correction_openers_are_flagged():
    """Audit item 5 (2026-08-28): correction turns are eval-corpus gold."""
    for yes in ("no, that reminder is wrong", "that's wrong", "i meant 5am",
                "it will be OMNIGEL not OMINM", "actually, make it Friday",
                "you got it wrong"):
        assert orchestrator._CORRECTION_RE.match(yes), yes
    for no in ("nothing much", "note this down", "noon works",
               "it is hot today", "say that again"):
        assert not orchestrator._CORRECTION_RE.match(no), no


async def test_answer_to_a_question_ending_in_emoji_is_not_swallowed(monkeypatch):
    """Live 2026-09-02: Kyraan asked "...today, last 7 days...? 🙂" and
    the fragment guard swallowed "today" with "Go on — I'm listening…"
    because the emoji sat after the question mark."""
    from kyraan.agents import orchestrator
    orchestrator._history[4242] = [
        ("user", "read social channel messages"),
        ("assistant", "how far back should I read—today, last 7 days? 🙂")]
    seen = []

    async def fake_loop(chat_id, text, **kw):
        seen.append(text); return "read it"
    from kyraan.agents import agent_loop as _al
    monkeypatch.setattr(_al, "run", fake_loop)
    reply = await orchestrator._dispatch(4242, "today")
    assert "listening" not in reply and seen == ["today"]
    orchestrator._history.pop(4242, None)
