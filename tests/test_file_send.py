"""Files OUT (owner, 2026-08-28): the model composes text files, the
channel validates and delivers to the requesting chat only."""
import pytest

from kyraan.agents import loop_tools
from kyraan.channels import file_send
from kyraan.control_plane import kernel


@pytest.fixture
def wired(monkeypatch):
    sent = []

    async def fake_send(chat_id, filename, data, caption):
        sent.append((chat_id, filename, data, caption))

    monkeypatch.setattr(file_send, "_send_fn", fake_send)
    return sent


def test_filename_hygiene_is_deterministic():
    assert file_send.clean_filename("../../etc/passwd.txt") == "passwd.txt"
    assert file_send.clean_filename("ac usage.csv") == "ac usage.csv"
    for bad in ("report.exe", "no_extension", "", "x.pdf"):
        with pytest.raises(ValueError):
            file_send.clean_filename(bad)


async def test_send_validates_and_delivers(wired):
    out = await file_send.send(7, "usage.csv", "day,kwh\nMon,3.6\n",
                               caption="AC usage")
    assert out == {"filename": "usage.csv", "bytes": 16}
    assert wired == [(7, "usage.csv", b"day,kwh\nMon,3.6\n", "AC usage")]
    with pytest.raises(ValueError, match="no content"):
        await file_send.send(7, "empty.txt", "   ")
    with pytest.raises(ValueError, match="cap is"):
        await file_send.send(7, "big.txt", "x" * 300_000)


async def test_executor_sends_to_its_own_chat_and_short_circuits(wired):
    out = await loop_tools._files_send(
        42, {"filename": "schedule.md", "content": "# Doses\n- Hep-B\n"}, "")
    assert wired[0][0] == 42          # the requester's chat, never chosen
    assert "__direct_reply__" in out and "schedule.md" in out["__direct_reply__"]


async def test_executor_honest_when_unwired(monkeypatch):
    monkeypatch.setattr(file_send, "_send_fn", None)
    with pytest.raises(kernel.ToolFailed, match="isn't available"):
        await loop_tools._files_send(7, {"filename": "a.txt",
                                         "content": "hi"}, "")


async def test_original_round_trip_and_delete_cleanup(monkeypatch, tmp_path):
    """Owner (2026-08-28): originals persist under data/documents and
    come back on request; deleting the doc deletes its file."""
    import pytest as _pytest
    from kyraan.store import documents, pg
    if not pg.available():
        _pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    from kyraan.store import embed, episodes
    monkeypatch.setattr(episodes, "sensitivity_flags",
                        lambda text, exposure="cloud_ok": [])
    monkeypatch.setattr(embed, "embed",
                        lambda texts: [[0.5] * embed.EMBED_DIM for _ in texts])
    monkeypatch.setattr(documents, "FILES_DIR", tmp_path / "documents")
    with pg.connection() as conn:
        conn.execute("TRUNCATE document CASCADE")
        conn.commit()
    doc_id = documents.ingest(
        7, "photo", "GAS MEMO\nConsumer 607795 amount 8340",
        caption="gas memo", original=(b"\xff\xd8fakejpegbytes", "jpg"))
    stored = documents.original_file(7, doc_id)
    assert stored is not None
    path, filename = stored
    from pathlib import Path
    assert Path(path).read_bytes() == b"\xff\xd8fakejpegbytes"
    assert filename.endswith(".jpg") and "gas memo" in filename
    assert documents.original_file(8, doc_id) is None      # chat-scoped
    documents.delete_documents(7, [doc_id])
    assert not Path(path).exists()                          # file went too
    pg.reset_pool_for_tests()


async def test_same_bytes_different_ocr_is_one_document(monkeypatch, tmp_path):
    """Owner (2026-08-28): the same file re-sent can OCR slightly
    differently and dodge the text-identity doc id — identical BYTES
    are the same document."""
    import pytest as _pytest
    from kyraan.store import documents, pg
    if not pg.available():
        _pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    from kyraan.store import embed, episodes
    monkeypatch.setattr(episodes, "sensitivity_flags",
                        lambda text, exposure="cloud_ok": [])
    monkeypatch.setattr(embed, "embed",
                        lambda texts: [[0.5] * embed.EMBED_DIM for _ in texts])
    monkeypatch.setattr(documents, "FILES_DIR", tmp_path / "documents")
    with pg.connection() as conn:
        conn.execute("TRUNCATE document CASCADE")
        conn.commit()
    photo = (b"\xff\xd8samecardbytes", "jpg")
    a = documents.ingest(7, "photo", "GAS MEMO Consumer 607795 total 995",
                         caption="gas memo", original=photo)
    b = documents.ingest(7, "photo", "GAS MEM0 Consumer 6O7795 total 995.",
                         caption="resent", original=photo)  # OCR variance
    assert a == b                                     # one document
    with pg.connection() as conn:
        n, = conn.execute("SELECT count(*) FROM document").fetchone()
    assert n == 1
    # a different chat's identical bytes are THEIR OWN document
    c = documents.ingest(8, "photo", "GAS MEMO Consumer 607795 total 995",
                         caption="other chat", original=photo)
    assert c != a
    pg.reset_pool_for_tests()
