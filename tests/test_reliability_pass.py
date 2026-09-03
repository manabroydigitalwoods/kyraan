"""Reliability pass 2026-09-04: fewer corrections, same answers."""
import asyncio

from kyraan.agents import agent_loop


def test_trailing_offer_is_stripped_after_a_statement():
    s = agent_loop._strip_trailing_offer
    assert s("Okay Maan 😊 That's good — let him sleep.\n\nWant me to set a reminder for the next feed?",
             "He is sleeping with his mom") == "Okay Maan 😊 That's good — let him sleep."
    assert s("Nice 😊 New toy day. Do you want me to note it or set a reminder? 😊",
             "today kiaan get new car toy") == "Nice 😊 New toy day."
    # a request keeps the guard's re-decide (asking permission for a stated request is the real error)
    assert s("Should I set it for 9 PM tomorrow instead?", "remind me to call mom at 9pm") is None
    # an offer that IS the reply is not shortened to nothing
    assert s("Do you want me to save that?", "murukku is nice") is None


def test_volume_and_photo_rails(monkeypatch):
    from kyraan.agents import orchestrator, loop_tools
    from kyraan.control_plane import kernel
    from kyraan.store import persons, documents
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    seen = []

    async def fake_vol(chat_id, args, raw_text):
        seen.append(args["percent"]); return {"volume": args["percent"] * 10, "on": "manab_s_echo_dot"}
    monkeypatch.setattr(loop_tools, "_speaker_volume", fake_vol)
    for q in ("volum 4", "can you up the volume 7?", "adjust echo dot volume to 7", "volume 5"):
        out = asyncio.run(orchestrator.handle_message(1, q))
        assert out.startswith("Volume set to"), q
    assert seen == [4, 7, 7, 5]
    monkeypatch.setattr(persons, "resolve", lambda n: n.lower().strip() if n.lower().strip() in ("kiaan", "souvik") else None)
    monkeypatch.setattr(documents, "list_documents", lambda chat_id, limit, person, tag, kind: (
        [{"id": "a", "caption": "kiaan — 02 Sep 2026", "date": "2026-09-02"}]
        if kind == "moment" and person == "kiaan" else []))
    monkeypatch.setattr(documents, "original_file", lambda chat_id, doc_id: None)
    out = asyncio.run(orchestrator.handle_message(1, "show kiaan's photos"))
    assert out.startswith('I have "kiaan — 02 Sep 2026"')
    assert asyncio.run(orchestrator.handle_message(1, "show souvik's photos")).startswith("No photos of souvik")
