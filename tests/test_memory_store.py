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


def test_same_second_same_target_proposals_do_not_collide():
    """External review P1: second-resolution filenames overwrote a second
    same-target fact from the same extraction."""
    a = store.propose_fact("people/kiaan.md", "- Fact one", source="s")
    b = store.propose_fact("people/kiaan.md", "- Fact two", source="s")
    assert a != b
    assert a.exists() and b.exists()


def test_promote_retry_does_not_duplicate_the_markdown_fact():
    """Review P2: a crash between the tree append and the proposal unlink
    meant a retried promote appended the same fact line twice."""
    proposal = store.propose_fact("people/wife.md", "- Wife's name is Mira", source="s")
    saved_text = proposal.read_text()
    target = store.promote(proposal)
    # simulate the crash-retry: the proposal file reappears, promote reruns
    proposal.write_text(saved_text)
    store.promote(proposal)
    assert target.read_text().count("Wife's name is Mira") == 1
