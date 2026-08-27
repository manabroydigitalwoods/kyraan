"""Document memory (owner directive 2026-08-27): ingestion, the hybrid
digits+meaning retrieval, exposure gating, and the forget cascade."""
import os
from pathlib import Path

import pytest

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import documents, embed, pg  # noqa: E402

_PG_UP = pg.available()

_CARD = ("RAVI COOL SERVICES\nAC repair and maintenance\n"
         "Phone: 98300 12345\nSevoke Road, Siliguri")


@pytest.fixture
def doc_db(monkeypatch):
    if not _PG_UP:
        pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    monkeypatch.setattr(documents, "_allowed_exposures",
                        lambda: ("cloud_ok",))
    from kyraan.store import episodes
    monkeypatch.setattr(episodes, "sensitivity_flags",
                        lambda text, exposure="cloud_ok": [])
    monkeypatch.setattr(embed, "embed",
                        lambda texts: [[0.5] * embed.EMBED_DIM for _ in texts])
    with pg.connection() as conn:
        conn.execute("TRUNCATE document CASCADE")
        conn.commit()
    yield
    pg.reset_pool_for_tests()


@pytest.mark.pg
def test_ingest_chunks_and_dedups(doc_db):
    a = documents.ingest(7, "photo", _CARD, caption="ac repair card")
    b = documents.ingest(7, "photo", _CARD, caption="sent again")
    assert a == b  # same text, same chat → one document
    with pg.connection() as conn:
        docs, = conn.execute("SELECT count(*) FROM document").fetchone()
        chunks, = conn.execute("SELECT count(*) FROM document_chunk").fetchone()
    assert docs == 1 and chunks >= 1


@pytest.mark.pg
def test_digits_hit_through_fts_not_embeddings(doc_db, monkeypatch):
    documents.ingest(7, "photo", _CARD, caption="ac card")
    # embeddings deliberately USELESS for this query — orthogonal vector
    monkeypatch.setattr(embed, "embed",
                        lambda texts: [[0.0] * (embed.EMBED_DIM - 1) + [1.0]
                                       for _ in texts])
    hits = documents.search(7, "98300 12345")
    assert hits and "98300 12345" in hits[0]["text"]
    assert hits[0]["fts"] is True  # the digits arm carried it


@pytest.mark.pg
def test_thin_text_is_not_a_document(doc_db):
    assert documents.ingest(7, "photo", "hi there") is None


@pytest.mark.pg
def test_chat_scope_holds(doc_db):
    documents.ingest(7, "photo", _CARD)
    assert documents.search(8, "98300 12345") == []


@pytest.mark.pg
def test_local_only_never_enters_a_cloud_prompt(doc_db, monkeypatch):
    documents.ingest(7, "pdf", _CARD, caption="private report")
    with pg.connection() as conn:
        conn.execute("UPDATE document SET exposure = 'local_only'")
        conn.commit()
    monkeypatch.setattr(documents, "_allowed_exposures", lambda: ("cloud_ok",))
    assert documents.search(7, "98300 12345") == []       # cloud prompt: absent
    monkeypatch.setattr(documents, "_allowed_exposures",
                        lambda: ("cloud_ok", "local_only"))
    assert documents.search(7, "98300 12345")             # local prompt: served


@pytest.mark.pg
def test_forget_cascade_covers_documents(doc_db):
    documents.ingest(7, "photo", _CARD)
    swept = documents.suppress_for_fact(
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "Ravi AC repair Siliguri")
    assert swept == 1
    assert documents.search(7, "98300 12345") == []       # unfindable


@pytest.mark.pg
def test_snippet_labels_caption_and_date(doc_db):
    documents.ingest(7, "photo", _CARD, caption="ac repair card")
    snippet = documents.relevant_snippet(7, "98300 12345")
    assert snippet.startswith('[from a saved document "ac repair card", 20')


def test_executor_empty_is_honest(monkeypatch):
    import asyncio

    from kyraan.agents import loop_tools
    monkeypatch.setattr(documents, "search", lambda c, q, k=3: [])
    result = asyncio.run(loop_tools._documents_search(7, {"query": "nothing"}, ""))
    assert result["found"] == 0 and "never invent" in result["note"]


def test_exposure_helper_defaults_to_cloud_only(monkeypatch):
    from kyraan.agents import agent_loop
    token = agent_loop._current_tier.set("frontier")
    try:
        assert documents._allowed_exposures() == ("cloud_ok",)
    finally:
        agent_loop._current_tier.reset(token)

@pytest.mark.pg
def test_captionless_ingest_gets_a_first_line_title(doc_db):
    """"show me all docs" listed photo "(untitled)" rows that told the
    owner nothing (2026-08-27) — a captionless, filenameless ingest
    falls back to the first meaningful content line as its name."""
    doc_id = documents.ingest(7, "photo",
                              "Cash Memo\nAMANTARN HP GAS SERVICE\n"
                              "Consumer: 607795, Amount 8340")
    rows = documents.list_documents(7)
    assert [r["caption"] for r in rows if r["id"] == doc_id] == ["Cash Memo"]


@pytest.mark.pg
def test_rename_returns_prior_and_scopes_to_chat(doc_db):
    doc_id = documents.ingest(7, "photo", _CARD, caption="Immunization card")
    assert documents.rename_document(8, doc_id, "stolen") is None  # not chat 8's
    prior = documents.rename_document(7, doc_id, "Kiaan's vaccination card")
    assert prior == "Immunization card"
    rows = documents.list_documents(7)
    assert rows[0]["caption"] == "Kiaan's vaccination card"


async def test_rename_executor_is_confirm_gated():
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    with pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._documents_rename(
            7, {"query": "immunization", "new_name": "Kiaan's card"}, "")


def test_rename_undo_swaps_names_back():
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["documents.rename"](
        {}, {"renamed": True, "doc_id": "x", "prior": "Immunization card",
             "now": "Kiaan's card"}, None
    ) == ("documents.rename", {"query": "Kiaan's card",
                               "new_name": "Immunization card"})
