"""Memory precision: similarity against active facts (2026-09-04)."""
from kyraan.memory import engine


def test_similar_active_ranks_by_cosine_within_subject(monkeypatch):
    from kyraan.store import embed
    def vec(a, b): v = [0.0] * 8; v[0], v[1] = a, b; return v
    monkeypatch.setattr(embed, "embed", lambda texts: [vec(1.0, 0.0)])
    monkeypatch.setattr(engine, "_active_with_vectors", lambda subject="": [
        ("f1", "owner", "reminders every hour to drink water", vec(0.95, 0.31)),   # ~0.95
        ("f2", "owner", "raksha bandhan plans", vec(0.6, 0.8)),                  # 0.6: below
        ("f3", "kiaan", "kiaan born in october", vec(1.0, 0.0))])                # other subject
    got = engine.similar_active("remind me every 5 minutes to drink water", subject="owner")
    assert [g[1] for g in got] == ["f1"] and got[0][0] > 0.9
    assert engine.similarity_verdict(got[0][0]) == "replace"
    assert engine.similarity_verdict(0.76) == "similar" and engine.similarity_verdict(0.5) == ""


def test_review_hint_reads_the_proposal_meta(tmp_path):
    from kyraan.agents.orchestrator import _proposal_hint
    p = tmp_path / "p.md"
    p.write_text('---\ntarget: work/x.md\nmeta: {"supersedes": "old wording here", "similarity": 0.91}\n---\n\n- new wording\n')
    assert _proposal_hint(p).strip().startswith("↳ replaces: old wording here")
    p.write_text('---\ntarget: work/x.md\nmeta: {"similar_to": "a cousin fact"}\n---\n\n- new\n')
    assert "similar to (kept): a cousin fact" in _proposal_hint(p)
    p.write_text('---\ntarget: work/x.md\n---\n\n- plain\n')
    assert _proposal_hint(p) == ""
