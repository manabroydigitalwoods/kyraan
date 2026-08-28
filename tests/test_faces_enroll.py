

def test_invite_followup_reads_the_bots_own_ask():
    """Live 2026-08-27 23:37: the bot asked "To save Kamal's face for
    recognition, please send the latest photo..." — the captionless
    photo that followed got a garden description instead of the
    enrollment confirm. The follow-up is deterministic."""
    from kyraan.agents import faces
    invite = ("Sure—Kamal.\nTo save Kamal's face for recognition, please "
              "send the latest photo where his face is clearly visible.")
    assert faces.invite_followup(invite) == "Kamal"
    # multiword names survive; possessive apostrophe variants both parse
    assert faces.invite_followup(
        "Please send a photo so I can remember Suman Ghosh’s face") \
        == "Suman Ghosh"
    # no photo request, or no face-save phrasing -> not an invite
    assert faces.invite_followup("Kamal's face looked happy today") is None
    assert faces.invite_followup("Tell me more about Kamal") is None
    assert faces.invite_followup("") is None


async def test_faces_list_is_the_only_truth(monkeypatch):
    """Live 2026-08-28: "do you have suman's face data" got "Yes" twice
    for a face never enrolled — the loop had no way to check, so it
    guessed from conversation. faces.list answers from the store."""
    from kyraan.agents import faces, loop_tools
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "enrolled_names",
                        lambda: ["Kamal", "Kiaan", "Titu Roy"])
    out = await loop_tools._faces_list(7, {}, "")
    assert out["enrolled_faces"] == ["Kamal", "Kiaan", "Titu Roy"]
    assert "COMPLETE" in out["note"]


async def test_check_photo_uses_the_stash_or_says_expired(monkeypatch):
    """"you can take from above" got "please resend the photo" although
    the bytes sat in the 10-minute stash (live 2026-08-28 00:11)."""
    from kyraan.agents import faces, loop_tools
    from kyraan.control_plane import kernel
    import pytest as _pytest
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "recent_photo", lambda chat_id: b"jpegbytes")
    monkeypatch.setattr(faces, "recognize",
                        lambda image: {"names": ["Suman Ghosh"], "maybe": [],
                                       "unknown_faces": 0})
    monkeypatch.setattr(faces, "enrolled_names", lambda: ["Suman Ghosh"])
    out = await loop_tools._faces_check_photo(7, {}, "")
    assert out["recognized"] == ["Suman Ghosh"]

    monkeypatch.setattr(faces, "recent_photo", lambda chat_id: None)
    with _pytest.raises(kernel.ToolFailed, match="expire after 10 minutes"):
        await loop_tools._faces_check_photo(7, {}, "")


async def test_persons_add_is_gated_and_grants_nothing(monkeypatch):
    """Owner (2026-08-28): friends become CONTACTS — registry rows with
    no chat and stage none, addressable but access-less."""
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    from kyraan.store import persons
    import pytest as _pytest
    calls = []
    monkeypatch.setattr(persons, "list_persons", lambda: [("owner",), ("kamal",)])
    monkeypatch.setattr(persons, "enroll",
                        lambda pid, chat, stage, consented:
                        calls.append((pid, chat, stage, consented)))
    with _pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._persons_add(7, {"name": "Titu Roy"}, "")
    dup = await loop_tools._persons_add(7, {"name": "Kamal"}, "")
    assert dup["added"] is False          # no confirm ask for a no-op
    with _pytest.raises(kernel.ToolFailed):
        await loop_tools._persons_add(7, {"name": "owner"}, "")
    assert calls == []                    # nothing written without a yes


async def test_persons_profile_aggregates_everything(monkeypatch):
    from kyraan.agents import faces, loop_tools
    from kyraan.memory import engine
    from kyraan.store import documents, persons, triples
    monkeypatch.setattr(persons, "resolve",
                        lambda n: "titu_roy" if "titu" in n.lower() else None)
    monkeypatch.setattr(persons, "name_map",
                        lambda: {"titu": "titu_roy", "titu roy": "titu_roy"})
    monkeypatch.setattr(engine, "active_entries",
                        lambda: [{"content": "Titu is a school friend"},
                                 {"content": "Wife's name is Mira"}])
    monkeypatch.setattr(documents, "list_documents",
                        lambda c, limit=15, person="":
                        [{"caption": "Titu — invoice", "date": "2026-08-28"}])
    monkeypatch.setattr(triples, "relations_for",
                        lambda p: [{"head": "titu_roy",
                                    "relation": "friend_of", "tail": "owner"}])
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "enrolled_names", lambda: ["Titu Roy"])
    out = await loop_tools._persons_profile(7, {"name": "Titu"}, "")
    assert out["person"] == "titu_roy"
    assert out["facts"] == ["Titu is a school friend"]
    assert out["documents"] == ["Titu — invoice (2026-08-28)"]
    assert out["graph"] == ["titu_roy —friend_of→ owner"]
    assert out["face_recognition"] == "enrolled"

    out = await loop_tools._persons_profile(7, {"name": "Stranger"}, "")
    assert out["found"] is False


def test_completed_undo_matrix_shapes():
    """P3.1d closed: every destroy has an inverse built from the prior
    capture_prior observed."""
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["calendar.delete_event"](
        {}, {}, {"title": "Standup", "start": "s", "end": "e"}
    ) == ("calendar.create_event", {"title": "Standup", "start": "s", "end": "e"})
    assert UNDO_MAP["reminders.cancel"](
        {}, {}, {"text": "call mom", "when_iso": "w", "repeat": "",
                 "interval_minutes": 0, "window_start": "", "window_end": ""}
    )[0] == "reminders.recreate"
    assert UNDO_MAP["tasks.cancel"](
        {}, {}, {"instruction": "check mail", "when_iso": "w"}
    ) == ("tasks.recreate", {"instruction": "check mail", "when_iso": "w",
                             "repeat": ""})
    assert UNDO_MAP["memory.forget"](
        {}, {}, {"entry_ids": ["e1"], "contents": ["x"]}
    ) == ("memory.unforget", {"entry_ids": ["e1"]})
    assert UNDO_MAP["rules.cancel"]({}, {"id": "ab12"}, None) \
        == ("rules.reactivate", {"rule_id": "ab12"})
    # unobserved prior => honestly not undoable
    assert UNDO_MAP["calendar.delete_event"]({}, {}, None) is None
    assert UNDO_MAP["memory.forget"]({}, {}, None) is None


def test_self_claim_reads_its_me_variants(monkeypatch):
    from kyraan.agents import faces
    for yes in ("its me", "It's me", "this is me", "that's me!", "its me."):
        assert faces.self_claim(yes)
    for no in ("its me and kamal", "who is this?", "me at the beach", ""):
        assert not faces.self_claim(no)
    from kyraan.store import persons
    monkeypatch.setattr(persons, "name_map",
                        lambda: {"owner": "owner", "maan": "owner",
                                 "manab roy": "owner", "kamal": "kamal"})
    assert faces.owner_display_name() == "Manab Roy"


async def test_persons_alias_renames_never_duplicates(monkeypatch):
    """Live 2026-08-28 02:45: "rename Kamal to Habu" produced a junk
    standalone contact. An alias makes both names one person; a name
    already meaning someone ELSE is refused."""
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    from kyraan.store import persons
    import pytest as _pytest
    added = []
    mapping = {"kamal": "kamal", "ruma": "ruma"}
    monkeypatch.setattr(persons, "resolve", lambda n: mapping.get(n.lower()))
    monkeypatch.setattr(persons, "add_alias",
                        lambda pid, alias: added.append((pid, alias)))
    with _pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._persons_alias(7, {"name": "Kamal",
                                            "alias": "Habu"}, "")
    noop = await loop_tools._persons_alias(7, {"name": "Kamal",
                                               "alias": "kamal"}, "")
    assert noop["aliased"] is False       # already means them: no ask
    with _pytest.raises(kernel.ToolFailed, match="cannot point at two"):
        await loop_tools._persons_alias(7, {"name": "Kamal",
                                            "alias": "Ruma"}, "")
    with _pytest.raises(kernel.ToolFailed, match="not in the person"):
        await loop_tools._persons_alias(7, {"name": "Nobody",
                                            "alias": "Xy"}, "")
    assert added == []                    # nothing written without a yes


async def test_profile_face_status_resolves_display_aliases(monkeypatch):
    """Live 2026-08-28 22:26: the face record shows "Habu" (display)
    while the hub key is kamal — the truth tool claimed "no face data"
    for an enrolled face. The resolver is the join."""
    from kyraan.agents import faces, loop_tools
    from kyraan.memory import engine
    from kyraan.store import documents, persons, triples
    monkeypatch.setattr(persons, "resolve",
                        lambda n: {"habu": "kamal", "kamal": "kamal"}.get(
                            n.lower()))
    monkeypatch.setattr(persons, "name_map",
                        lambda: {"habu": "kamal", "kamal": "kamal"})
    monkeypatch.setattr(engine, "active_entries", lambda: [])
    monkeypatch.setattr(documents, "list_documents",
                        lambda c, limit=15, person="": [])
    monkeypatch.setattr(triples, "relations_for", lambda p: [])
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "enrolled_names", lambda: ["Habu"])
    out = await loop_tools._persons_profile(7, {"name": "habu"}, "")
    assert out["face_recognition"] == "enrolled"


async def test_persons_list_is_the_bulk_roster(monkeypatch):
    """Live 2026-08-28 13:04: "list my all relatives" got "there isn't
    a bulk-list tool here". Now there is — resolver-joined."""
    from kyraan.agents import faces, loop_tools
    from kyraan.store import persons
    monkeypatch.setattr(persons, "list_persons",
                        lambda: [("owner", None, "owner", None),
                                 ("kamal", None, "none", None),
                                 ("ruma", 891, "none", "2026-08-27")])
    monkeypatch.setattr(persons, "name_map",
                        lambda: {"owner": "owner", "maan": "owner",
                                 "kamal": "kamal", "habu": "kamal",
                                 "ruma": "ruma"})
    monkeypatch.setattr(persons, "resolve",
                        lambda n: {"habu": "kamal"}.get(n.lower()))
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "enrolled_names", lambda: ["Habu"])
    out = await loop_tools._persons_list(7, {}, "")
    rows = {r["person"]: r for r in out["people"]}
    assert rows["kamal"]["face"] is True          # Habu resolves to kamal
    assert rows["kamal"]["aka"] == ["habu"]
    assert rows["kamal"]["kind"] == "contact"
    assert rows["ruma"]["kind"] == "household"
    assert rows["owner"]["kind"] == "the user"
