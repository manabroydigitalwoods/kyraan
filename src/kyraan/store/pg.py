"""Postgres access for the Phase 3 stores — one pool, one writer (the
bot process), DSN from KYRAAN_PG_DSN (localhost container; arch §2.1).

Everything degrades honestly: `available()` is a cheap liveness probe,
and callers treat an unreachable PG as "use the file path and log it" —
files remain the source of truth for facts (arch §2.1), so PG being
down never loses data, only retrieval features.
"""
import os
from contextlib import contextmanager

_pool = None


def dsn() -> str:
    """KYRAAN_PG_DSN wins; otherwise the DSN is BUILT from the same
    KYRAAN_PG_PASSWORD the compose file feeds Postgres. The old hardcoded
    'kyraan:kyraan' default disagreed with the documented setup, so a
    by-the-book install failed authentication (Bugbot P2)."""
    explicit = os.environ.get("KYRAAN_PG_DSN", "").strip()
    if explicit:
        return explicit
    password = (os.environ.get("KYRAAN_PG_PASSWORD", "").strip()
                or _compose_password() or "kyraan")
    return f"postgresql://kyraan:{password}@127.0.0.1:5432/kyraan"


def _compose_password() -> str:
    """The password Postgres itself was started with. Compose reads
    docker/.env; the bot reads the repo .env — so a documented custom
    password left the two disagreeing and the app failing auth (Bugbot
    P2). Read the same file compose does, as a fallback only: an
    explicit DSN or environment variable still wins."""
    from pathlib import Path
    env_file = Path(__file__).resolve().parents[3] / "docker" / ".env"
    try:
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "KYRAAN_PG_PASSWORD":
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _get_pool():
    global _pool
    if _pool is None:
        import atexit

        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(dsn(), min_size=0, max_size=4, open=True,
                               timeout=5)
        atexit.register(_pool.close)  # silence the finalization warning
    return _pool


@contextmanager
def connection():
    with _get_pool().connection() as conn:
        yield conn


def available() -> bool:
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def reset_pool_for_tests() -> None:
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
    _pool = None
