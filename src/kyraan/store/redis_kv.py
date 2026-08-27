"""Redis for VOLATILE session state (Part 4, arch §2.3 step 5): the
conversation window, summary backlogs, confirmation stashes, listing
caches. Nothing durable lives here — the budget ledger, facts, and
promises stay file/PG, and a FLUSHALL must never lose anything the
owner was promised (P3.4c).

Degradation contract: any Redis failure flips this process to DEAD mode
— every caller falls back to its in-memory structure, one
`session_backend_fallback` event is logged, and the process behaves
exactly as it did before Part 4 (restart amnesia included). No
per-operation split-brain: once dead, dead until restart.
"""
import json
import os

from kyraan.control_plane.logging_setup import log_event

_client = None
_dead = False
_PREFIX = "kyraan:"


def url() -> str:
    return os.environ.get("KYRAAN_REDIS_URL", "redis://127.0.0.1:6379/0")


def enabled() -> bool:
    return os.environ.get("KYRAAN_SESSION_BACKEND", "memory").strip().lower() == "redis"


def client():
    """The live client, or None (flag off, or Redis declared dead)."""
    global _client, _dead
    if _dead or not enabled():
        return None
    if _client is None:
        try:
            import redis
            _client = redis.Redis.from_url(url(), decode_responses=True,
                                           socket_timeout=2,
                                           socket_connect_timeout=2)
            _client.ping()
        except Exception as exc:
            mark_dead(f"connect: {exc}")
            return None
    return _client


def mark_dead(reason: str) -> None:
    """One logged event; memory fallback for the rest of the process."""
    global _dead, _client
    if not _dead:
        _dead = True
        _client = None
        log_event("session_backend_fallback", backend="redis",
                  reason=str(reason)[:200])


def reset_for_tests() -> None:
    global _client, _dead
    _client = None
    _dead = False


def key(*parts) -> str:
    return _PREFIX + ":".join(str(p) for p in parts)


# --- JSON kv (confirmation stash, listing cache) --------------------------

def set_json(name: str, value, ttl_s: int | None = None) -> bool:
    c = client()
    if c is None:
        return False
    try:
        c.set(name, json.dumps(value, ensure_ascii=False), ex=ttl_s)
        return True
    except Exception as exc:
        mark_dead(exc)
        return False


def get_json(name: str):
    c = client()
    if c is None:
        return None
    try:
        raw = c.get(name)
        return json.loads(raw) if raw is not None else None
    except Exception as exc:
        mark_dead(exc)
        return None


def delete(name: str) -> None:
    c = client()
    if c is None:
        return
    try:
        c.delete(name)
    except Exception as exc:
        mark_dead(exc)


# --- JSON lists (history, backlog) ----------------------------------------

def list_all(name: str) -> list | None:
    c = client()
    if c is None:
        return None
    try:
        return [json.loads(x) for x in c.lrange(name, 0, -1)]
    except Exception as exc:
        mark_dead(exc)
        return None


def list_append(name: str, item, maxlen: int | None = None,
                ttl_s: int = 7 * 24 * 3600) -> bool:
    c = client()
    if c is None:
        return False
    try:
        pipe = c.pipeline()
        pipe.rpush(name, json.dumps(item, ensure_ascii=False))
        if maxlen:
            pipe.ltrim(name, -maxlen, -1)
        pipe.expire(name, ttl_s)
        pipe.execute()
        return True
    except Exception as exc:
        mark_dead(exc)
        return False


def list_set(name: str, items: list, ttl_s: int = 7 * 24 * 3600) -> bool:
    c = client()
    if c is None:
        return False
    try:
        pipe = c.pipeline()
        pipe.delete(name)
        if items:
            pipe.rpush(name, *[json.dumps(x, ensure_ascii=False) for x in items])
            pipe.expire(name, ttl_s)
        pipe.execute()
        return True
    except Exception as exc:
        mark_dead(exc)
        return False
