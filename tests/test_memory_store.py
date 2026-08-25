from kyraan.memory import store


def test_propose_promote_roundtrip():
    target = "people/_test_person.md"
    proposal = store.propose_fact(target, "- likes tea", source="I like tea")
    try:
        assert proposal.exists()
        assert "_test_person.md" not in store.list_fact_files("people")  # not live until promoted

        promoted_path = store.promote(proposal)
        try:
            assert not proposal.exists()
            assert "likes tea" in promoted_path.read_text()
        finally:
            promoted_path.unlink(missing_ok=True)
    finally:
        proposal.unlink(missing_ok=True)


def test_reject_discards_proposal():
    proposal = store.propose_fact("people/_test_person2.md", "- irrelevant", source="irrelevant")
    store.reject(proposal)
    assert not proposal.exists()
