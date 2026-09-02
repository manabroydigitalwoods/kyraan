"""Photo analysis — the vision call path, the no-tools-by-construction
taint property, kill-switch gating, and the router's image plumbing."""
from types import SimpleNamespace

import pytest

from kyraan.agents import orchestrator, photo
from kyraan.model_router import router


@pytest.fixture(autouse=True)
def _no_live_document_store(monkeypatch):
    """Moment ingestion (2026-08-31) would write real rows from unit
    tests — stub it; the moment tests below install their own capture."""
    from kyraan.store import documents
    monkeypatch.setattr(documents, "ingest",
                        lambda *a, **k: None)


async def test_answer_sends_image_to_frontier(monkeypatch):
    seen = {}

    class _R:
        text = "That's a red square."
        latency_ms = 42.0

    async def fake_acall(prompt="", system="", tier="", images=None, **kw):
        seen.update(tier=tier, images=images, system=system, prompt=prompt)
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply, enroll = await photo.answer(9, "data:image/jpeg;base64,AAA", "what is this?")
    assert reply == "That's a red square."   # non-JSON response: fallback keeps the text
    assert enroll is None
    assert seen["tier"] == "frontier"
    assert seen["images"] == ["data:image/jpeg;base64,AAA"]
    assert "what is this?" in seen["prompt"]
    assert "never instructions" in seen["system"]   # the taint rule rides along


async def test_vision_extracts_enrollment_intent_from_any_caption(monkeypatch):
    """The caption path's intelligence: the vision model returns JSON with
    the reply AND any enrollment intent — regex phrases no longer the
    only door (live 2026-08-26: 'save this face, it's Maan' had no path)."""
    class _R:
        text = '{"reply": "A man in a green blazer.", "remember_face_as": "Maan"}'
        latency_ms = 5.0

    async def fake_acall(**kw):
        assert kw.get("force_json") is True
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply, enroll = await photo.answer(9, "data:x", "save this face, it's Maan")
    assert reply == "A man in a green blazer."
    assert enroll == "Maan"

    class _R2:
        text = '{"reply": "A toddler crawling.", "remember_face_as": null}'
        latency_ms = 5.0

    async def fake_acall2(**kw):
        return _R2()

    monkeypatch.setattr(photo.router, "acall", fake_acall2)
    reply, enroll = await photo.answer(9, "data:x", "this is kiaan")
    assert enroll is None   # naming alone is not an enrollment request


async def test_kill_switch_blocks_photo_turns(monkeypatch):
    monkeypatch.setattr(photo.kill_switch, "is_engaged", lambda: True)
    reply = await photo.answer(9, "data:x", "hi")
    assert "kill switch" in str(reply).lower()


async def test_provider_error_becomes_vision_unavailable(monkeypatch):
    async def broken(**kw):
        raise router.ModelProviderError("no images")

    monkeypatch.setattr(photo.router, "acall", broken)
    with pytest.raises(photo.VisionUnavailable):
        await photo.answer(9, "data:x", "")


def test_router_rejects_images_on_non_openai_providers():
    with pytest.raises(router.ModelProviderError, match="cannot process images"):
        router._dispatch("ollama", "qwen3:8b", "p", "s", 100, images=["data:x"])


def test_openai_payload_carries_image_parts(monkeypatch):
    captured = {}

    class _Msg:
        content = "red"
        reasoning = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = None

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr(router, "_get_openai_compatible_client",
                        lambda provider, cfg: _Client())
    router._call_openai_compatible(
        "openai", {"max_tokens_param": "max_completion_tokens"},
        "gpt-5.4-nano", "what is it?", "sys", 100,
        images=["data:image/png;base64,AA"])
    content = captured["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "what is it?"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AA"
    # high since 2026-08-28: low downscaled to ~512px and garbled
    # package/label lettering ("OMNIGEL" -> "OMMLNIL")
    assert content[1]["image_url"]["detail"] == "high"


def test_record_exchange_lands_in_history_and_chat_log(monkeypatch):
    orchestrator._history.pop(94, None)
    orchestrator.record_exchange(94, "[sent a photo: what's this?]", "A red square.")
    assert list(orchestrator._history[94]) == [
        ("user", "[sent a photo: what's this?]"),
        ("assistant", "A red square.")]
    from kyraan.control_plane import logging_setup
    import json
    rows = [json.loads(l) for l in logging_setup.CHAT_LOG.read_text().splitlines()]
    assert rows[-1]["role"] == "assistant" and rows[-1]["chat_id"] == 94
    orchestrator._history.pop(94, None)


def test_brief_flips_the_image_denial(monkeypatch):
    from kyraan.agents import capabilities

    base = capabilities.config.load()
    monkeypatch.setattr(capabilities.config, "load", lambda: {
        **base, "model_tiers": {"cheap": {"provider": "ollama"},
                                "frontier": {"provider": "openai"}}})
    brief = capabilities.capability_brief()
    assert "analyze PHOTOS" in brief
    assert "you can SEE photos" in brief
    assert "cannot create, draft, see" not in brief

    monkeypatch.setattr(capabilities.config, "load", lambda: {
        **base, "model_tiers": {"cheap": {"provider": "ollama"},
                                "frontier": {"provider": "ollama"}}})
    brief = capabilities.capability_brief()
    assert "analyze PHOTOS" not in brief
    assert "cannot create, draft, see" in brief


async def test_empty_vision_answer_retries_once(monkeypatch):
    """A successful call with a BLANK reply gets one in-process retry
    (found live 2026-08-27: 7s call, empty answer, the owner had to
    resend the photo); a second blank still gets the honest apology."""
    calls = []

    async def blank_then_good(prompt="", **kw):
        calls.append(1)
        text = '{"reply": ""}' if len(calls) == 1 else '{"reply": "A receipt."}'
        return SimpleNamespace(text=text, latency_ms=1.0)

    monkeypatch.setattr(photo.router, "acall", blank_then_good)
    reply, _ = await photo.answer(9, "data:image/jpeg;base64,AAA", "")
    assert reply == "A receipt."
    assert len(calls) == 2

    calls.clear()

    async def always_blank(prompt="", **kw):
        calls.append(1)
        return SimpleNamespace(text='{"reply": ""}', latency_ms=1.0)

    monkeypatch.setattr(photo.router, "acall", always_blank)
    reply, _ = await photo.answer(9, "data:image/jpeg;base64,AAA", "")
    assert "couldn't read that photo" in reply
    assert len(calls) == 2  # exactly one retry, never a loop


async def test_command_caption_yields_title_not_the_command(monkeypatch):
    """Live 2026-08-28 01:49: caption "save this supliment for kian"
    became the document's NAME. A command caption is an instruction:
    the vision title names the doc; the caption still supplies
    subjects (via ingest's own resolution)."""
    ingested = {}

    def fake_ingest(chat_id, kind, text, caption="", subjects=None,
                    original=None, **kw):
        ingested.update(caption=caption, subjects=subjects)
        return "doc-1"

    from kyraan.store import documents
    monkeypatch.setattr(documents, "ingest", fake_ingest)
    monkeypatch.setattr(documents, "subjects_from_name",
                        lambda title: ["kiaan"] if "kian" in title.lower() else [])

    class _R:
        text = ('{"reply": "A supplement box.", "remember_face_as": null, '
                '"document_text": "Fourts B Drops Zinc Vitamin C", '
                '"document_title": "Fourts B Drops box", '
                '"document_subjects": []}')
        latency_ms = 5.0

    async def fake_acall(**kw):
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply, _ = await photo.answer(9, "data:image/jpeg;base64,AAAA",
                                  "save this supliment for kian")
    assert ingested["caption"] == "Fourts B Drops box"
    assert "kiaan" in ingested["subjects"]


async def test_naming_caption_sheds_its_prefix(monkeypatch):
    """Live 2026-08-28 02:12: "this is Ruma's pain killer gel" became
    the document's title verbatim — a naming statement names the thing
    AFTER the "this is"."""
    ingested = {}

    def fake_ingest(chat_id, kind, text, caption="", subjects=None,
                    original=None, **kw):
        ingested.update(caption=caption)
        return "doc-1"

    from kyraan.store import documents
    monkeypatch.setattr(documents, "ingest", fake_ingest)
    monkeypatch.setattr(documents, "subjects_from_name", lambda t: [])

    class _R:
        text = ('{"reply": "A pain gel box.", "remember_face_as": null, '
                '"document_text": "OMNIGEL pain relief gel", '
                '"document_title": "Omnigel box", "document_subjects": []}')
        latency_ms = 5.0

    async def fake_acall(**kw):
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    await photo.answer(9, "data:image/jpeg;base64,AAAA",
                       "this is Ruma's pain killer gel")
    assert ingested["caption"] == "Ruma's pain killer gel"



async def test_scene_photo_becomes_a_moment_with_face_links(monkeypatch):
    """Owner 2026-08-31: "store the image ... find the matches and link
    them properly — it will create beautiful memories." A photo with NO
    document text used to be described then dropped; now it persists
    with recognized faces as person subjects and the original bytes."""
    from kyraan.store import documents
    stored = {}

    def capture(chat_id, kind, text, caption="", subjects=None,
                original=None, entities=None, **kw):
        stored.update(kind=kind, text=text, caption=caption,
                      subjects=subjects, original=original, entities=entities)
        return "doc-1"

    monkeypatch.setattr(documents, "ingest", capture)

    class _R:
        text = ('{"reply": "Kiaan grinning on the swing, golden evening light.", '
                '"entities": ["#playground", "Sharma Garden"]}')
        latency_ms = 5.0

    async def fake_acall(**kw):
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply, enroll = await photo.answer(
        9, "data:image/jpeg;base64,QUJD", "", recognized=["Kiaan"])
    assert stored["kind"] == "moment"
    assert "golden evening light" in stored["text"]
    assert stored["subjects"] == ["Kiaan"]          # face match -> person link
    assert stored["original"][0] == b"ABC"          # bytes kept
    assert "Kiaan —" in stored["caption"]           # auto-title names who
    # the label's own things, cleaned: names first, ONE category last
    assert stored["entities"] == ["Sharma Garden", "#playground"]
    assert "Saved to memories" in reply


async def test_enrollment_photo_is_not_a_memory(monkeypatch):
    from kyraan.store import documents
    called = []
    monkeypatch.setattr(documents, "ingest",
                        lambda *a, **k: called.append(1) or "x")

    class _R:
        text = '{"reply": "Noted.", "remember_face_as": "Suman"}'
        latency_ms = 5.0

    async def fake_acall(**kw):
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    await photo.answer(9, "data:image/jpeg;base64,QUJD",
                       "remember this face as Suman")
    assert called == []   # biometric intake, not a memory


def test_moment_captions_link_bare_names_and_self_words(monkeypatch):
    from kyraan.store import documents
    monkeypatch.setattr(documents, "_name_map",
                        lambda: {"kiaan": "kiaan", "ruma": "ruma",
                                 "maan": "owner", "owner": "owner"})
    assert documents.caption_people("me and kiaan") == ["kiaan", "owner"]   # explicit "me"
    assert documents.caption_people("my supplement") == ["owner"]
    assert documents.caption_people("with maan and kiaan") == ["kiaan"]  # a NAME never links the owner
    assert documents.caption_people("Ruma at the market") == ["ruma"]
    assert documents.caption_people("sunset") == []
    # document TITLES keep the strict rule — a shop is not a person
    assert documents.subjects_from_name("Ruma Stores receipt") == []


@pytest.mark.pg
def test_person_correction_links_the_latest_moment(monkeypatch):
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    from kyraan.store import pg
    if not pg.available():
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    from kyraan.store import documents
    with pg.connection() as conn:
        conn.execute("DELETE FROM document WHERE chat_id = 4242")
        conn.execute(
            """INSERT INTO document (id, chat_id, kind, caption, text,
                                     subject_persons)
               VALUES (gen_random_uuid(), 4242, 'moment', 'garden photo',
                       'x', ARRAY['kiaan'])""")
        conn.commit()
    got = documents.link_person_to_latest_moment(4242, "ruma")
    assert got == ("garden photo", ["kiaan"])   # prior subjects returned
    with pg.connection() as conn:
        subs, = conn.execute("""SELECT subject_persons FROM document
                                WHERE chat_id = 4242""").fetchone()
    assert sorted(subs) == ["kiaan", "ruma"]
    assert documents.link_person_to_latest_moment(9999, "ruma") is None
    pg.reset_pool_for_tests()


@pytest.mark.pg
def test_owner_claim_names_links_and_files_the_latest_moment(monkeypatch):
    """Live 2026-09-03: "this is my medicine" after a photo changed nothing.
    The claim makes the capture the owner's, named by the phrase, filed
    under a deterministic category — and leaves a real title alone."""
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    from kyraan.store import pg
    if not pg.available():
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    from kyraan.store import documents
    with pg.connection() as conn:
        conn.execute("DELETE FROM document WHERE chat_id = 4243")
        conn.execute(
            """INSERT INTO document (id, chat_id, kind, caption, text,
                                     subject_persons, entities)
               VALUES (gen_random_uuid(), 4243, 'moment', 'Moment — 03 Sep 2026',
                       '[photo] Wellbeing Nutrition throat relief lozenges',
                       '{}', ARRAY['Wellbeing Nutrition'])""")
        conn.commit()
    got = documents.claim_latest_moment(4243, "my medicine")
    assert got == ("my medicine", ["Wellbeing Nutrition", "#medical"])
    with pg.connection() as conn:
        subs, cap = conn.execute("""SELECT subject_persons, caption FROM document
                                    WHERE chat_id = 4243""").fetchone()
    assert subs == ["owner"] and cap == "my medicine"
    # a second claim is idempotent; a real title is never overwritten
    with pg.connection() as conn:
        conn.execute("UPDATE document SET caption = 'throat lozenges' WHERE chat_id = 4243")
        conn.commit()
    assert documents.claim_latest_moment(4243, "my medicine") == (
        "throat lozenges", ["Wellbeing Nutrition", "#medical"])
    assert documents.claim_latest_moment(9998, "my medicine") is None
    pg.reset_pool_for_tests()


def test_search_hits_say_whose_they_are(monkeypatch):
    """Live 2026-09-03: "my medications" listed Kiaan's drops as the owner's."""
    import asyncio
    from kyraan.agents import loop_tools
    from kyraan.store import documents
    monkeypatch.setattr(documents, "search", lambda *a, **k: [
        {"caption": "Fourts B Drops", "date": "2026-08-27", "text": "drops",
         "subjects": ["kiaan"]},
        {"caption": "my supplement", "date": "2026-09-02", "text": "omega",
         "subjects": ["owner"]}])
    out = asyncio.run(loop_tools._documents_search(1, {"query": "medicine"}, ""))
    assert out[0].startswith('[document "Fourts B Drops", 2026-08-27, about: kiaan]')
    assert 'about: owner]' in out[1]


@pytest.mark.pg
def test_capture_and_milestone_note_relate_both_ways(monkeypatch):
    """Owner 2026-09-03: Kiaan's milestone note "1st wear sree krishna
    dress" and the photo "today kiaan with lord shree krishna dressed"
    linked Kiaan and never each other. relate() joins them by the note's
    title words + a shared person, inherits the note's #tags onto the
    capture, and is symmetric and idempotent. A body-only word
    ("standing") never links."""
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    from kyraan.store import pg
    if not pg.available():
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    from kyraan.store import documents
    monkeypatch.setattr(documents, "_name_map", lambda: {"kiaan": "kiaan"})
    with pg.connection() as conn:
        conn.execute("DELETE FROM document WHERE chat_id = 4244")
        conn.execute("""INSERT INTO document (id, chat_id, kind, caption, text, subject_persons, entities) VALUES
            ('00000000-0000-0000-0000-00000000a001', 4244, 'note', '1st wear sree krishna dress',
             'Today his mom him dress up of lord krishna', ARRAY['kiaan'], ARRAY['#family','#milestone','type:milestone']),
            ('00000000-0000-0000-0000-00000000a002', 4244, 'note', '1st Self- Support Standing',
             'He started self-supporting standing', ARRAY['kiaan'], ARRAY['#milestone']),
            ('00000000-0000-0000-0000-00000000a003', 4244, 'moment', 'today kiaan with lord shree krishna dressed',
             '[photo] kiaan dressed as Lord Shree Krishna standing on a tiled floor', ARRAY['kiaan'], ARRAY['Lord Shree Krishna','#festival']),
            ('00000000-0000-0000-0000-00000000a004', 4244, 'moment', 'ruma at the market',
             '[photo] ruma in a krishna print dress', ARRAY['ruma'], '{}')""")
        conn.commit()
    got = documents.relate('00000000-0000-0000-0000-00000000a003')
    assert got == ['00000000-0000-0000-0000-00000000a001']       # not the standing note
    assert documents.relate('00000000-0000-0000-0000-00000000a003') == []   # idempotent
    with pg.connection() as conn:
        rows = dict(conn.execute("SELECT id::text, related::text[] FROM document WHERE chat_id = 4244").fetchall())
        ents, = conn.execute("SELECT entities FROM document WHERE id = '00000000-0000-0000-0000-00000000a003'").fetchone()
    assert rows['00000000-0000-0000-0000-00000000a001'] == ['00000000-0000-0000-0000-00000000a003']
    assert rows['00000000-0000-0000-0000-00000000a003'] == ['00000000-0000-0000-0000-00000000a001']
    assert rows['00000000-0000-0000-0000-00000000a004'] == []                # no shared person
    assert ents == ['Lord Shree Krishna', '#festival', '#family', '#milestone']   # inherited hubs
    # the note side finds the capture too (a note written after the photo)
    assert documents.relate('00000000-0000-0000-0000-00000000a002') == []
    listing = documents.list_documents(4244)
    by = {d["caption"]: d for d in listing}
    assert by["1st wear sree krishna dress"]["related"] == ["today kiaan with lord shree krishna dressed"]
    pg.reset_pool_for_tests()


def test_did_you_save_it_answers_from_the_store(monkeypatch):
    """Live 2026-09-03: 15 minutes after a photo, "did you save it?" got
    "what do you mean by it?". The latest capture is the referent."""
    import asyncio, datetime
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    from kyraan.store import documents
    cap = {"doc_id": "x", "kind": "moment", "caption": "today kiaan with lord shree krishna dressed",
           "created": datetime.datetime(2026, 9, 3, 0, 30, tzinfo=datetime.timezone.utc),
           "subjects": ["kiaan"], "entities": ["Lord Shree Krishna"],
           "tags": ["#festival", "#milestone"], "related": ["1st wear sree krishna dress"]}
    monkeypatch.setattr(documents, "latest_capture", lambda chat_id, max_age_h=24: cap)
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    for q in ("did you save it?", "Did u save the image?", "links?", "what is it linked to?"):
        out = asyncio.run(orchestrator.handle_message(1, q))
        assert out.startswith('Yes — saved as "today kiaan with lord shree krishna dressed"'), q
        assert 'Linked to: "1st wear sree krishna dress"' in out and "#milestone" in out
    # nothing recent -> not our rail (the loop would answer)
    monkeypatch.setattr(documents, "latest_capture", lambda chat_id, max_age_h=24: None)
    assert documents.describe_capture(cap).count("\n") == 4


def test_vision_enrollment_needs_an_enrollment_word():
    """Live 2026-09-03 01:03: "can you similar images for kiaan? if yes
    then link it with them" became a face-template confirm."""
    from kyraan.agents import faces
    assert not faces.enroll_words("can you similar images for kiaan? if yes then link it with them")
    assert not faces.enroll_words("this is kiaan")
    assert faces.enroll_words("remember this is Suman Ghosh")
    assert faces.enroll_words("save his face as Kamal")
    assert faces.enroll_words("recognise me next time")


@pytest.mark.pg
def test_similar_captures_share_a_person_and_a_close_description(monkeypatch):
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    from kyraan.store import pg, embed
    if not pg.available():
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    import json
    from kyraan.store import documents
    def vec(a, b):
        v = [0.0] * embed.EMBED_DIM; v[0] = a; v[1] = b; return json.dumps(v)
    with pg.connection() as conn:
        conn.execute("DELETE FROM document WHERE chat_id = 4245")
        for i, (cap, subs, v) in enumerate([
                ("krishna dress", ["kiaan"], vec(1, 0)),
                ("krishna dress again", ["kiaan"], vec(0.9, 0.44)),   # sim ~0.9
                ("ruma in krishna print", ["ruma"], vec(1, 0)),         # no shared person
                ("kiaan vaccination card", ["kiaan"], vec(0, 1))]):     # sim 0
            did = f"00000000-0000-0000-0000-0000000000b{i}"
            conn.execute("INSERT INTO document (id, chat_id, kind, caption, text, subject_persons) VALUES (%s, 4245, 'moment', %s, 'x', %s)", (did, cap, subs))
            conn.execute("INSERT INTO document_chunk (id, document_id, seq, text, embedding) VALUES (gen_random_uuid(), %s, 0, 'x', %s)", (did, v))
        conn.commit()
    sims = documents.similar_captures("00000000-0000-0000-0000-0000000000b0")
    assert [s["caption"] for s in sims] == ["krishna dress again"]
    assert documents.link_captures("00000000-0000-0000-0000-0000000000b0", [s["doc_id"] for s in sims]) == 1
    with pg.connection() as conn:
        rel = dict(conn.execute("SELECT caption, related::text[] FROM document WHERE chat_id = 4245").fetchall())
    assert rel["krishna dress"] == ["00000000-0000-0000-0000-0000000000b1"]
    assert rel["krishna dress again"] == ["00000000-0000-0000-0000-0000000000b0"]
    pg.reset_pool_for_tests()


def test_my_links_is_the_links_rail(monkeypatch):
    import asyncio, datetime
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    from kyraan.store import documents
    cap = {"doc_id": "x", "kind": "moment", "caption": "c", "created": datetime.datetime.now(datetime.timezone.utc),
           "subjects": [], "entities": [], "tags": [], "related": []}
    monkeypatch.setattr(documents, "latest_capture", lambda chat_id, max_age_h=24: cap)
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    for q in ("my links", "show links", "connections?"):
        assert asyncio.run(orchestrator.handle_message(1, q)).startswith('Yes — saved as "c"'), q
