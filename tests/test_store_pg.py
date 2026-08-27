"""Phase 3 store scaffolding — migrations against the real local
Postgres container. Marked `pg`: auto-skips wherever the container isn't
reachable (dev without docker, CI legs without services)."""
import os
from pathlib import Path

import pytest

# ONLY the DSN — a full load_dotenv here leaked KYRAAN_EMAIL_BODIES (and
# every other live setting) into the whole suite's environment and broke
# order-dependent capability tests.
_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import pg  # noqa: E402

pytestmark = pytest.mark.pg

_pg_up = pg.available()
if not _pg_up:
    pytestmark = [pytest.mark.pg,
                  pytest.mark.skip(reason="local Postgres container unreachable")]


def test_migrations_applied_and_idempotent():
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "scripts/migrate.py"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "already applied: 001_core.sql" in result.stdout  # second+ run: no diff


def test_schema_v1_tables_exist_with_audit_columns():
    with pg.connection() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert {"person", "fact", "triple", "action_log", "schema_version"} <= tables
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='fact'")}
        # the design-audit columns are the point of schema v2
        assert {"legacy_id", "subject_reviewed", "exposure", "visibility"} <= cols
        # 002: the read path's ranking columns (P3.2b)
        assert {"importance", "term", "target"} <= cols
        # triple: one row per supporting fact (no bare head/relation/tail unique)
        uniques = [r[0] for r in conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename='triple'")]
        assert any("fact_id" in u for u in uniques if "UNIQUE" in u.upper() or "unique" in u)


def test_vector_extension_available():
    with pg.connection() as conn:
        row = conn.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchone()
        assert row is not None
