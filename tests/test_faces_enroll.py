

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
