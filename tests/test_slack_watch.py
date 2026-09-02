"""Slack mention watch (owner decisions 2026-09-02): draft + confirm,
never post; watermarks advance only past surfaced messages; the first
sight never replays history; the owner's own messages never trigger."""
import pytest

from kyraan.control_plane import kernel
from kyraan.triggers import slack_watch

CSV = ("MsgID,UserID,UserName,RealName,Channel,ThreadTs,Text,Time,Permalink,"
       "Reactions,BotName,FileCount,AttachmentIDs,HasMedia,Cursor\n"
       "1788346981.5,U0BUE9NSKSN,manab,Manab Roy,C1,,hello all,2026-09-02T11:03:01Z,,,,0,,false,\n"
       "1788346982.5,U0OTHER,ruma,Ruma,C1,,<@U0BUE9NSKSN> dinner at 8?,2026-09-02T11:04:00Z,,,,0,,false,\n"
       "1788346983.5,U0OTHER,ruma,Ruma,C1,,unrelated chatter,2026-09-02T11:05:00Z,,,,0,,false,\n")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_watch, "STATE_PATH", tmp_path / "slack_watch.json")
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)


def _wire(monkeypatch, csv_text=CSV):
    drafts, asks = [], []

    async def run_tool(call, **kw):
        assert call.tool_name == "slack.history"
        return csv_text

    async def draft(instruction, question=""):
        drafts.append(instruction)
        return "Yes, 8 works — see you then."

    async def ask(chat_id, channel, draft_text, context):
        asks.append((chat_id, channel, draft_text, context))

    monkeypatch.setattr(kernel, "run_tool", run_tool)
    slack_watch.init("U0BUE9NSKSN", draft, ask, owner_handle="manab")
    return drafts, asks


def test_parse_and_mention_detection():
    rows = slack_watch.parse_history(CSV)
    assert [r["ts"] for r in rows] == ["1788346981.5", "1788346982.5", "1788346983.5"]
    assert slack_watch.mentions_owner("<@U0BUE9NSKSN> hi", "U0BUE9NSKSN")
    assert slack_watch.mentions_owner("hey @manab", "U0BUE9NSKSN", "manab")
    assert not slack_watch.mentions_owner("hey @manabx", "U0BUE9NSKSN", "manab") is False
    assert not slack_watch.mentions_owner("plain text", "U0BUE9NSKSN", "manab")


async def test_first_sight_sets_watermark_and_replays_nothing(monkeypatch):
    drafts, asks = _wire(monkeypatch)
    assert await slack_watch.tick(["#social"], 7) == 0
    assert asks == []
    assert slack_watch._load()["watermarks"]["#social"] == "1788346983.5"


async def test_new_mention_is_drafted_and_asked_once(monkeypatch):
    drafts, asks = _wire(monkeypatch)
    slack_watch._save({"watermarks": {"#social": "1788346981.5"}})  # seen up to hello
    assert await slack_watch.tick(["#social"], 7) == 1
    assert len(asks) == 1 and asks[0][2] == "Yes, 8 works — see you then."
    assert "dinner at 8" in asks[0][3]
    assert "Output the message text only" in drafts[0]   # the writer brief
    # watermark moved past everything; a second tick is silent
    assert await slack_watch.tick(["#social"], 7) == 0
    assert len(asks) == 1


async def test_failed_surface_keeps_the_watermark_for_retry(monkeypatch):
    drafts, asks = _wire(monkeypatch)
    slack_watch._save({"watermarks": {"#social": "1788346981.5"}})

    async def bad_ask(*a):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(slack_watch, "_ask_fn", bad_ask)
    assert await slack_watch.tick(["#social"], 7) == 0
    assert slack_watch._load()["watermarks"]["#social"] == "1788346981.5"  # unmoved


async def test_dnd_holds_everything(monkeypatch):
    drafts, asks = _wire(monkeypatch)
    slack_watch._save({"watermarks": {"#social": "1788346981.5"}})
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: False)
    assert await slack_watch.tick(["#social"], 7) == 0
    assert asks == [] and slack_watch._load()["watermarks"]["#social"] == "1788346981.5"


async def test_owners_own_mention_of_self_never_triggers(monkeypatch):
    own = CSV.replace("U0OTHER,ruma,Ruma", "U0BUE9NSKSN,manab,Manab Roy")
    drafts, asks = _wire(monkeypatch, own)
    slack_watch._save({"watermarks": {"#social": "1788346981.5"}})
    assert await slack_watch.tick(["#social"], 7) == 0


def test_wiring_noops_without_a_token(monkeypatch):
    from kyraan.channels import telegram_bot
    monkeypatch.delenv("SLACK_MCP_XOXP_TOKEN", raising=False)
    armed = []

    class FakeJQ:
        def run_repeating(self, *a, **k): armed.append(k.get("name"))
    telegram_bot._wire_slack_watch(FakeJQ(), object())
    assert armed == []          # no token: the watch never arms


def test_meta_talk_is_recognized():
    bad = ["I can draft it, but I need 1 detail: what do you want me to say back to Ruma?",
           "Maan, which plan are you referring to?",
           "Do you want me to confirm the 20th?"]
    good = ["Good, tui? 😄", "Yes — 20th Oct, Darjeeling, 5 nights. Let me check the leave and confirm.",
            "Haan, mone ache. Let me check dates and tell you."]
    assert all(slack_watch.looks_like_meta_talk(b) for b in bad)
    assert not any(slack_watch.looks_like_meta_talk(g) for g in good)


def test_instruction_carries_thread_relationship_and_voice(monkeypatch):
    monkeypatch.setattr(slack_watch, "relationship_line",
                        lambda name: "Ruma Roy is your wife.")
    mention = {"user": "Ruma Roy", "text": "<@U1> 20th oct, darjeeling 5 nights",
               "ts": "3", "user_id": "U2"}
    thread = [{"user": "Ruma Roy", "text": "the plan from last month?", "ts": "1", "user_id": "U2"},
              {"user": "Manab Roy", "text": "which one?", "ts": "2", "user_id": "U1"}]
    brief = slack_watch.build_instruction("#social", mention, thread, ["ok chol", "which one?"])
    assert "is your wife" in brief and "the plan from last month" in brief
    assert "match this voice" in brief and "ok chol" in brief
    assert "never ask the reader" in brief


async def test_rejected_drafts_retry_then_surface_without_a_proposal(monkeypatch):
    drafts, asks = _wire(monkeypatch)
    slack_watch._save({"watermarks": {"#social": "1788346981.5"}})
    calls = []

    async def meta_draft(instruction, question=""):
        calls.append(instruction)
        return "Maan, what do you want me to say back to Ruma?"
    monkeypatch.setattr(slack_watch, "_draft_fn", meta_draft)
    assert await slack_watch.tick(["#social"], 7) == 1
    assert len(calls) == 2                       # one retry with the REJECTED note
    assert "REJECTED" in calls[1]
    assert asks[0][2] == ""                      # surfaced, honestly draftless



async def test_kyraan_posts_never_become_voice_samples(monkeypatch):
    drafts, asks = _wire(monkeypatch)
    slack_watch._save({"watermarks": {"#social": "1788346981.5"},
                       "kyraan_posted": ["hello all"]})   # our own earlier post
    await slack_watch.tick(["#social"], 7)
    assert "(no samples" in drafts[0]          # the only owner line was ours
    assert "never promise" in drafts[0] and "Don't repeat" in drafts[0]
