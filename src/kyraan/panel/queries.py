"""Read-only queries behind the panel's API.

Every function here READS: the event log (live + rotated archives), the
trace log, the cost ledger, and the trigger stores. Nothing in this module
opens a file for writing — that is the package's first rule, and the
reason Phase A needs no governance round.

Sources are the ones that already exist; the panel adds no instrumentation:
- logs/events.jsonl  — every model call, tool call, and gate decision
- logs/traces.jsonl  — user text, prompts, responses, turn boundaries
- data/cost_ledger.json via model_router (budget authority)
- data/reminders.json, agent_tasks.json, goals.json via the trigger stores
"""
import json
from pathlib import Path
from urllib.parse import quote
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from kyraan.control_plane import logging_setup

# The page is served fresh from disk on every request; this module is
# imported once at startup. So editing the panel's Python while it runs
# leaves a NEW page talking to an OLD API, and the failure is silent —
# a missing field just renders as an empty console (found live
# 2026-08-31: the systems matrix went blank because the running server
# predated `components`). Bump this whenever a response SHAPE changes,
# and bump EXPECTED_API in app.js with it; the page then says so out loud
# instead of quietly dropping a panel.
API_VERSION = 12

# A turn is "overdue" for the trigger board on the same slack the
# scheduler itself uses, so the panel and the bot never disagree about
# whether something fired on time.
OVERDUE_SLACK_S = 60


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _since_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _event_files():
    """Live log + rotated archives, oldest first. Shared with
    usage_report so a widened window never silently reads only today."""
    from kyraan.model_router import usage_report
    return usage_report._event_files()


def _trace_files():
    log = logging_setup.TRACE_LOG
    archive = logging_setup.ARCHIVE_DIR
    rotated = list(archive.rglob(f"{log.stem}-*.jsonl")) if archive.exists() else []
    rotated += list(log.parent.glob(f"{log.stem}-*.jsonl"))
    return [p for p in (*sorted(set(rotated)), log) if p.exists()]


def _iter_records(paths, since: str = "", needle: str = ""):
    """Stream JSON records from newline-delimited logs.

    `needle` is a cheap pre-JSON substring reject — at 5MB/day of events,
    parsing every line to then discard it is the whole cost of a request.
    A record whose ts is older than `since` is skipped without yielding;
    records missing a ts are kept (a malformed line should surface in the
    panel, not vanish from it).
    """
    for path in paths:
        try:
            with open(path, "r", errors="replace") as handle:
                for line in handle:
                    if needle and needle not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since and record.get("ts", "") and record["ts"] < since:
                        continue
                    yield record
        except OSError:
            continue  # rotated out from under us mid-read; not an error


def _clip(value, limit: int = 400):
    """Bound one field's size. The API is JSON and the page never renders
    HTML, so this is about response weight, not escaping."""
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + f"… (+{len(value) - limit} chars)"


# --------------------------------------------------------------------------
# events


def events(limit: int = 200, hours: float = 24, kind: str = "",
           turn_id: str = "", anomalies_only: bool = False,
           query: str = "", tools: tuple = ()) -> dict:
    """The most recent events, newest first.

    `tools` narrows to events naming one of those tools — what the brain's
    selection hands over when you ask "show me these firing".
    """
    limit = max(1, min(int(limit), 1000))
    since = _since_iso(hours)
    needle = f'"{kind}"' if kind else ""
    matched = []
    for record in _iter_records(_event_files(), since=since, needle=needle):
        if kind and record.get("kind") != kind:
            continue
        if turn_id and not str(record.get("turn_id", "")).startswith(turn_id):
            continue
        if anomalies_only and record.get("kind") not in logging_setup.ANOMALY_KINDS:
            continue
        if tools and (record.get("tool") or record.get("skill")) not in tools:
            continue
        if query and query.lower() not in json.dumps(record, default=str).lower():
            continue
        matched.append({k: _clip(v) for k, v in record.items()})
        # Keep only the tail: an unbounded list over a week of archives is
        # the one way this endpoint could hurt the machine it monitors.
        if len(matched) > limit * 4:
            del matched[: len(matched) - limit]
    matched.sort(key=lambda r: r.get("ts", ""))
    tail = matched[-limit:]
    tail.reverse()
    return {"events": tail, "hours": hours, "count": len(tail)}


def event_kinds(hours: float = 24) -> dict:
    """Kind histogram for the stream filter — what actually occurs, so the
    filter offers real options instead of the full ANOMALY_KINDS list."""
    counts: dict = defaultdict(int)
    for record in _iter_records(_event_files(), since=_since_iso(hours)):
        counts[record.get("kind", "?")] += 1
    return {"kinds": [
        {"kind": k, "count": c,
         "anomaly": k in logging_setup.ANOMALY_KINDS}
        for k, c in sorted(counts.items(), key=lambda kv: -kv[1])
    ]}


# --------------------------------------------------------------------------
# turns


def _blank_turn(turn_id: str) -> dict:
    return {
        "turn_id": turn_id, "ts": "", "user_text": "", "reply": "",
        "total_ms": None, "model_calls": 0, "tool_calls": 0,
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "cost_usd": 0.0, "anomalies": [], "providers": [], "models": [],
        "tools": [], "errors": 0,
    }


def turns(limit: int = 50, hours: float = 24, sort: str = "recent",
          tools: tuple = ()) -> dict:
    """One row per turn: what it cost, how long it took, what it touched,
    and whether it was clean.

    `sort='tokens'` answers the panel's headline question — which turns
    spend the budget. Ranking needs the whole window scored before the
    cut, so the aggregate is built over `hours` and sliced at the end.
    """
    limit = max(1, min(int(limit), 500))
    since = _since_iso(hours)
    rows: dict = {}

    def row(turn_id):
        return rows.setdefault(turn_id, _blank_turn(turn_id))

    for record in _iter_records(_event_files(), since=since):
        turn_id = record.get("turn_id")
        if not turn_id:
            continue
        entry = row(turn_id)
        kind = record.get("kind")
        if not entry["ts"] or record.get("ts", "") < entry["ts"]:
            entry["ts"] = record.get("ts", "")
        if kind == "model_call":
            entry["model_calls"] += 1
            entry["input_tokens"] += record.get("input_tokens") or 0
            entry["output_tokens"] += record.get("output_tokens") or 0
            entry["cached_tokens"] += record.get("cached_tokens") or 0
            entry["cost_usd"] = round(entry["cost_usd"] + (record.get("cost_usd") or 0), 6)
            for field, bucket in (("provider", "providers"), ("model", "models")):
                value = record.get(field)
                if value and value not in entry[bucket]:
                    entry[bucket].append(value)
        elif kind == "tool_call":
            entry["tool_calls"] += 1
            tool = record.get("tool") or record.get("skill")
            if tool and tool not in entry["tools"]:
                entry["tools"].append(tool)
        if kind in logging_setup.ANOMALY_KINDS:
            entry["errors"] += 1
            if kind not in entry["anomalies"]:
                entry["anomalies"].append(kind)

    # Trace log supplies the human ends of the turn — what was asked and
    # what came back. Only for turns the event pass already knows about.
    for record in _iter_records(_trace_files(), since=since):
        turn_id = record.get("turn_id")
        if not turn_id or turn_id not in rows:
            continue
        entry = rows[turn_id]
        if record.get("kind") == "turn_start":
            entry["user_text"] = _clip(record.get("user_text", ""), 240)
        elif record.get("kind") == "turn_end":
            entry["reply"] = _clip(record.get("reply", ""), 240)
            entry["total_ms"] = record.get("total_ms")

    ordered = list(rows.values())
    if tools:
        # Turns that actually CALLED one of these — the brain's "which
        # turns used this skill" question, answered from the audit log.
        ordered = [t for t in ordered if any(x in tools for x in t["tools"])]
    if sort == "tokens":
        ordered.sort(key=lambda r: -(r["input_tokens"] + r["output_tokens"]))
    elif sort == "cost":
        ordered.sort(key=lambda r: -r["cost_usd"])
    elif sort == "slow":
        ordered.sort(key=lambda r: -(r["total_ms"] or 0))
    else:
        ordered.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return {"turns": ordered[:limit], "total_turns": len(ordered),
            "hours": hours, "sort": sort}


def turn_detail(turn_id: str, full: bool = False, hours: float = 72) -> dict:
    """One turn's complete flow, events and traces merged in time order —
    the same reconstruction `scripts/trace.py` prints, as JSON.

    Prompt and response TEXT is clipped unless `full` is asked for: the
    trace log holds entire assembled prompts, and the default view should
    not ship tens of KB of memory context per row.
    """
    if not turn_id:
        return {"turn_id": "", "records": [], "found": False}
    since = _since_iso(hours)
    limit = 100_000 if full else 600
    records = []
    for source, paths in (("event", _event_files()), ("trace", _trace_files())):
        for record in _iter_records(paths, since=since, needle=turn_id[:12]):
            if not str(record.get("turn_id", "")).startswith(turn_id):
                continue
            records.append({"_source": source,
                            **{k: _clip(v, limit) for k, v in record.items()}})
    records.sort(key=lambda r: r.get("ts", ""))
    stages = []
    for record in records:
        if record.get("kind") == "turn_end" and record.get("stages"):
            stages = record["stages"]
    return {"turn_id": turn_id, "records": records, "stages": stages,
            "found": bool(records), "full": full}


# --------------------------------------------------------------------------
# cost, health, triggers


def usage(days: int = 7) -> dict:
    from kyraan.model_router import usage_report
    return usage_report.usage_summary(days=days)


_health_cache: dict = {"at": 0.0, "value": None}
# The probes make real network calls (searxng alone waits up to 8s). A
# dashboard that polls would otherwise hammer the services it reports on.
HEALTH_TTL_S = 60


def health(force: bool = False) -> dict:
    now = time.monotonic()
    cached = _health_cache["value"]
    if cached is not None and not force and now - _health_cache["at"] < HEALTH_TTL_S:
        return {**cached, "cached": True}
    from kyraan.control_plane import health as health_module
    # One sweep, two shapes: the component matrix for the mission-control
    # console and the full text for the health console. Probing twice
    # would double an already-slow call.
    probed = health_module._probe_components()
    verdict, text = health_module.report(probed=probed)
    value = {
        "verdict": verdict, "text": text, "checked_at": _utc_now_iso(),
        "components": [{"name": name, "ok": status == "OK", "detail": detail}
                       for name, status, detail in probed],
    }
    _health_cache.update(at=now, value=value)
    return {**value, "cached": False}


def _fire_state(when_iso: str) -> dict:
    """Parse a scheduled time into {iso, in_seconds, overdue} — the panel's
    whole reason for a trigger board is spotting the third one."""
    from kyraan.control_plane.dnd import local_now
    from kyraan.triggers import scheduler
    try:
        when = scheduler._parse_when(when_iso)
    except (ValueError, TypeError):
        return {"iso": when_iso, "in_seconds": None, "overdue": False,
                "unparsed": True}
    delta = (when - local_now()).total_seconds()
    return {"iso": when.isoformat(), "in_seconds": round(delta),
            "overdue": delta < -OVERDUE_SLACK_S}


def triggers() -> dict:
    """Everything scheduled: reminders, agent tasks, goals — with next
    fire times and, the point of the board, what is already late.

    A machine that sleeps misses due jobs (plan.md §3d #4); an overdue row
    here is that failure made visible instead of inferred from silence.
    """
    from kyraan.panel import demo
    if demo.enabled():
        rows = demo.tasks()
        rows.sort(key=lambda r: r["fire"]["in_seconds"])
        return {"triggers": rows,
                "overdue": sum(1 for r in rows if r["fire"]["overdue"])}

    from kyraan.triggers import agent_tasks, goals
    from kyraan.triggers import store as reminder_store

    rows = []
    try:
        for reminder in reminder_store.list_pending():
            rows.append({
                "type": "reminder", "id": reminder.id,
                "text": _clip(reminder.text, 200),
                "repeat": reminder.repeat or "",
                "chat_id": reminder.chat_id,
                "claimed_at": reminder.claimed_at or "",
                "fire": _fire_state(reminder.when_iso),
            })
    except (OSError, ValueError, TypeError, KeyError) as exc:
        rows.append({"type": "reminder", "id": "", "text": f"unreadable: {exc}",
                     "error": True, "fire": _fire_state("")})

    try:
        for task in agent_tasks.list_active():
            rows.append({
                "type": "agent_task", "id": task.id,
                "text": _clip(task.instruction, 200),
                "repeat": task.repeat or "",
                "chat_id": task.chat_id,
                "undelivered": bool(task.pending_result),
                "fire": _fire_state(task.when_iso),
            })
    except (OSError, ValueError, TypeError, KeyError) as exc:
        rows.append({"type": "agent_task", "id": "", "text": f"unreadable: {exc}",
                     "error": True, "fire": _fire_state("")})

    try:
        for record in goals._load():
            if record.get("status") != "active":
                continue
            rows.append({
                "type": "goal", "id": record.get("id", ""),
                "text": _clip(record.get("title", ""), 200),
                "repeat": f"every {record.get('cadence_hours', 24)}h",
                "chat_id": record.get("chat_id"),
                "person": record.get("person", ""),
                "undelivered": bool(record.get("unreported")),
                "steps_done": sum(1 for s in record.get("steps") or [] if s.get("done")),
                "steps_total": len(record.get("steps") or []),
                "fire": _fire_state(record.get("next_cycle_iso", "")),
            })
    except (OSError, ValueError, TypeError, KeyError) as exc:
        rows.append({"type": "goal", "id": "", "text": f"unreadable: {exc}",
                     "error": True, "fire": _fire_state("")})

    rows.sort(key=lambda r: (r["fire"]["in_seconds"] is None,
                             r["fire"]["in_seconds"] or 0))
    return {"triggers": rows,
            "overdue": sum(1 for r in rows if r["fire"]["overdue"])}


def status() -> dict:
    """The header strip: kill switch, budget, today's turn counts."""
    from kyraan.control_plane import kill_switch
    from kyraan.control_plane.dnd import local_now
    from kyraan.model_router import router

    engaged = kill_switch.is_engaged()
    reason = ""
    if engaged:
        try:
            reason = _clip(kill_switch.KILL_SWITCH_PATH.read_text().strip(), 200)
        except OSError:
            reason = ""

    budget = router.daily_budget_usd()
    spent = router.today_cost_usd()

    since = _since_iso(24)
    turn_count = anomalous = 0
    for record in _iter_records(_event_files(), since=since, needle='"turn_health"'):
        if record.get("kind") != "turn_health":
            continue
        turn_count += 1
        if record.get("anomaly_count"):
            anomalous += 1

    return {
        "api_version": API_VERSION,
        "now": local_now().isoformat(),
        "kill_switch": {"engaged": engaged, "reason": reason},
        "budget": {
            "daily_budget_usd": budget,
            "spent_today_usd": round(spent, 4),
            "used_pct": round(spent / budget * 100, 1) if budget > 0 else None,
        },
        "last_24h": {"turns": turn_count, "anomalous_turns": anomalous},
    }


# --------------------------------------------------------------------------
# memory — the map, its links, and the review gate


def _project_2d(vectors) -> list:
    """PCA to two dimensions, normalised into [-1, 1].

    PCA rather than UMAP/t-SNE on purpose: it needs only numpy (already a
    dependency via opencv), it is DETERMINISTIC — the same facts land in
    the same place every load, so the map is a place you can learn rather
    than a fresh scatter each time — and on 43 facts the neighbourhood
    structure survives it. Revisit if the store grows past a few thousand.
    """
    import numpy as np

    matrix = np.asarray(vectors, dtype=float)
    if matrix.ndim != 2 or len(matrix) < 2:
        return [[0.0, 0.0] for _ in vectors]
    centred = matrix - matrix.mean(axis=0)
    # SVD, not an eigendecomposition of the covariance: 384 dimensions on
    # 43 rows makes the covariance matrix both huge and rank-deficient.
    _, _, components = np.linalg.svd(centred, full_matrices=False)
    coords = centred @ components[:2].T
    span = np.abs(coords).max(axis=0)
    span[span == 0] = 1.0
    return (coords / span).round(4).tolist()


def _kmeans(coords, k: int, rounds: int = 24) -> list:
    """Tiny deterministic k-means over the 2D projection.

    Seeded by evenly spaced points of the sorted input rather than at
    random, so the clustering — like the projection — is stable between
    loads. Colouring a map that reshuffles on every refresh teaches
    nothing.
    """
    import numpy as np

    points = np.asarray(coords, dtype=float)
    if len(points) <= k or k < 2:
        return [0] * len(points)
    order = np.lexsort((points[:, 1], points[:, 0]))
    centres = points[order[np.linspace(0, len(points) - 1, k).astype(int)]].copy()
    labels = np.zeros(len(points), dtype=int)
    for _ in range(rounds):
        distances = ((points[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        new_labels = distances.argmin(axis=1)
        if (new_labels == labels).all():
            break
        labels = new_labels
        for index in range(k):
            members = points[labels == index]
            if len(members):
                centres[index] = members.mean(axis=0)
    return labels.tolist()


def _demo_memory_map(include_inactive: bool) -> dict:
    """The same shape memory_map returns, from synthetic facts. Kept beside
    the real one so the two cannot drift into different payloads."""
    from kyraan.panel import demo

    rows, vectors = demo.facts()
    facts_out, usable_vectors = [], []
    for row, vector in zip(rows, vectors):
        if not include_inactive and not row["active"]:
            continue
        entry = {k: v for k, v in row.items() if k != "topic"}
        facts_out.append(entry)
        usable_vectors.append(vector)

    if len(usable_vectors) >= 2:
        coords = _project_2d(usable_vectors)
        clusters = _kmeans(coords, k=min(7, max(2, len(usable_vectors) // 30)))
        for entry, coord, cluster in zip(facts_out, coords, clusters):
            entry["x"], entry["y"] = coord
            entry["cluster"] = cluster

    census: dict = defaultdict(lambda: defaultdict(int))
    for entry in facts_out:
        for field in ("subject", "kind", "sphere", "era"):
            census[field][entry[field]] += 1
    return {"facts": facts_out,
            "census": {f: dict(c) for f, c in census.items()},
            "positioned": len(usable_vectors), "degraded": "", "demo": True}


def memory_map(limit: int = 400, include_inactive: bool = True) -> dict:
    """Every fact as a point: its 2D position, its cluster, and enough
    metadata to colour and read it.

    Reads Postgres, which is the only store holding the embeddings. When
    pg is down this returns the facts WITHOUT coordinates rather than
    nothing — the census and the list stay useful even with no map.
    """
    from kyraan.panel import demo
    if demo.enabled():
        return _demo_memory_map(include_inactive)

    from kyraan.store import pg

    limit = max(1, min(int(limit), 2000))
    rows, degraded = [], ""
    try:
        with pg.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT legacy_id, content, subject, kind, sphere, era, term, "
                "       importance, active, created_at, target, embedding "
                "FROM fact ORDER BY created_at DESC LIMIT %s", (limit,))   # newest first past the cap
            rows = cur.fetchall()
    except Exception as exc:                       # pg down, schema drift
        degraded = f"{type(exc).__name__}: {exc}"

    facts, vectors = [], []
    for row in rows:
        (legacy_id, content, subject, kind, sphere, era, term,
         importance, active, created_at, target, embedding) = row
        if not include_inactive and not active:
            continue
        facts.append({
            "id": legacy_id, "content": _clip(content, 300),
            "subject": subject or "owner", "kind": kind or "other",
            "sphere": sphere or "personal", "era": era or "current",
            "term": term or "long", "importance": importance or "normal",
            "active": bool(active), "target": target or "",
            "created": created_at.isoformat() if created_at else "",
        })
        # pgvector hands back its own type or a string depending on the
        # adapter registration; parse defensively rather than assume.
        if embedding is not None:
            if isinstance(embedding, str):
                try:
                    vectors.append(json.loads(embedding))
                except json.JSONDecodeError:
                    vectors.append(None)
            else:
                vectors.append(list(embedding))
        else:
            vectors.append(None)

    usable = [i for i, vector in enumerate(vectors) if vector]
    if len(usable) >= 2:
        coords = _project_2d([vectors[i] for i in usable])
        clusters = _kmeans(coords, k=min(6, max(2, len(usable) // 6)))
        for slot, index in enumerate(usable):
            facts[index]["x"], facts[index]["y"] = coords[slot]
            facts[index]["cluster"] = clusters[slot]
    for fact in facts:
        fact.setdefault("x", None)
        fact.setdefault("y", None)
        fact.setdefault("cluster", -1)

    census: dict = defaultdict(lambda: defaultdict(int))
    for fact in facts:
        for field in ("subject", "kind", "sphere", "era"):
            census[field][fact[field]] += 1
    return {
        "facts": facts,
        "census": {field: dict(counts) for field, counts in census.items()},
        "positioned": len(usable),
        "degraded": degraded,
    }


def memory_links(include_superseded: bool = False) -> dict:
    """Triples as edges — head → relation → tail — for the graph.

    Superseded facts are excluded by default. Their relations are history,
    not wiring: leaving them in made three restatements of Kiaan's
    birthday look like a live disagreement (found 2026-08-31 — the panel's
    own false positive, not bad data).
    """
    from kyraan.panel import demo
    if demo.enabled():
        rows, _ = demo.facts()
        links = [{**link, "active": True} for link in demo.triples(rows)]
        tails: dict = defaultdict(set)
        facts_per: dict = defaultdict(set)
        for link in links:
            key = (link["from"], link["rel"])
            tails[key].add(link["to"])
            facts_per[key].add(link["fact"])
        contested, variants = set(), set()
        for key, values in tails.items():
            if len(values) > 1:
                (contested if len(facts_per[key]) > 1 else variants).add(key)
        for link in links:
            key = (link["from"], link["rel"])
            link["contested"] = key in contested
            link["variant"] = key in variants
        return {"links": links, "degraded": "",
                "contested": sorted(f"{h} {r}" for h, r in contested),
                "variants": sorted(f"{h} {r}" for h, r in variants)}

    from kyraan.store import pg

    try:
        with pg.connection() as conn, conn.cursor() as cur:
            # triple stores fact_id (uuid); the map keys facts by their
            # legacy_id, so join rather than hand the page two id spaces.
            cur.execute(
                "SELECT f.legacy_id, t.head, t.relation, t.tail, f.active "
                "FROM triple t LEFT JOIN fact f ON f.id = t.fact_id "
                + ("" if include_superseded else "WHERE f.active ")
                + "ORDER BY t.head, t.relation")
            rows = cur.fetchall()
    except Exception as exc:
        return {"links": [], "degraded": f"{type(exc).__name__}: {exc}",
                "contested": [], "variants": []}

    links = [{"fact": fact or "", "from": head, "rel": relation, "to": tail,
              "active": bool(active)}
             for fact, head, relation, tail, active in rows]

    # Two different shapes hide behind "one head+relation, several tails",
    # and calling both a contradiction is what produced the false alarm:
    #
    #   CONTESTED — different facts disagree. The store is holding both
    #     sides of a real conflict; that is the review queue's job.
    #   VARIANT   — ONE fact yielded several spellings of one answer
    #     ("12-10-2025" and "12_october_2025"). That is extraction noise,
    #     worth tidying, but nothing is in dispute.
    tails: dict = defaultdict(set)
    facts_per: dict = defaultdict(set)
    for link in links:
        key = (link["from"], link["rel"])
        tails[key].add(link["to"])
        facts_per[key].add(link["fact"])

    contested, variants = set(), set()
    for key, values in tails.items():
        if len(values) < 2:
            continue
        (contested if len(facts_per[key]) > 1 else variants).add(key)
    for link in links:
        key = (link["from"], link["rel"])
        link["contested"] = key in contested
        link["variant"] = key in variants

    return {"links": links, "degraded": "",
            "contested": sorted(f"{h} {r}" for h, r in contested),
            "variants": sorted(f"{h} {r}" for h, r in variants)}


def memory_review() -> dict:
    """The review queue and the §6 sampling gate it feeds.

    The gate — 200 reviewed at >=90% trailing approval — is what family
    stage-2 waits on, so the panel states the distance to it plainly
    rather than leaving it to be counted by hand.
    """
    from kyraan.memory import review_scaling
    from kyraan.memory import store as memory_store

    pending = []
    try:
        for path in sorted(memory_store.PENDING_DIR.glob("*.md")):
            pending.append({"name": path.stem,
                            "text": _clip(path.read_text(errors="replace"), 400)})
    except OSError:
        pass

    stats = review_scaling._load()
    recent = list(stats.get("recent") or [])
    trailing = recent[-review_scaling.TRAILING:]
    approval = round(sum(trailing) / len(trailing), 3) if trailing else None
    total = int(stats.get("total_reviewed", 0))
    return {
        "pending": pending,
        "pending_count": len(pending),
        "total_reviewed": total,
        "needed": review_scaling.TOTAL_NEEDED,
        "remaining": max(0, review_scaling.TOTAL_NEEDED - total),
        "trailing_approval": approval,
        "rate_needed": review_scaling.RATE_NEEDED,
        "trailing_window": len(trailing),
        "gate_met": (total >= review_scaling.TOTAL_NEEDED
                     and approval is not None
                     and approval >= review_scaling.RATE_NEEDED),
    }


# --------------------------------------------------------------------------
# the brain — memories, tasks and skills as one wired graph

# Test fixtures that reached the real audit log before the KYRAAN_LOG_DIR
# isolation landed. They are not tools; drawn as neurons they would be
# lies with usage counts attached.
_NOT_A_TOOL = ("t.", "fake.", "test.")

# Edge weight below which a similarity link is noise rather than a
# thought. Tuned on the live store: at 0.45 the mesh connects related
# facts without becoming the complete graph (which shows nothing).
_SYNAPSE_FLOOR = 0.45
_SYNAPSES_PER_FACT = 3


def _tool_activity() -> tuple:
    """(usage counts, co-activation pairs) from the audit log.

    Co-activation is the honest version of "these skills work together":
    not a guess from their names, but the record of them firing in the
    same turn.
    """
    usage: Counter = Counter()
    per_turn: dict = defaultdict(set)
    for record in _iter_records(_event_files(), since=_since_iso(24 * 30)):
        if record.get("kind") not in ("tool_call", "agent_tool_call"):
            continue
        tool = record.get("tool")
        if not tool or tool.startswith(_NOT_A_TOOL):
            continue
        usage[tool] += 1
        if record.get("turn_id"):
            per_turn[record["turn_id"]].add(tool)

    pairs: Counter = Counter()
    for tools in per_turn.values():
        ordered = sorted(tools)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                pairs[(left, right)] += 1
    return usage, pairs


def _as_vector(value):
    """pgvector hands back its own type or a string depending on adapter
    registration; parse defensively rather than assume."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return list(value)


def _synapses_pg(table: str, id_col: str, ids: list, floor: float = _SYNAPSE_FLOOR) -> list | None:
    """The mesh, computed where the vectors live (2026-09-04). The numpy
    version below builds an n×n similarity matrix in Python — 200MB at
    5k rows, 5GB at 25k. pgvector answers "the k nearest to each row"
    with an index-backed lateral join and the matrix never exists.
    Same rule: top-k per row, floor, each pair once. None when the
    store cannot answer, so the caller falls back to numpy."""
    if len(ids) < 2:
        return []
    from kyraan.store import pg
    try:
        with pg.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT a.{id_col}, b.{id_col}, 1 - (a.embedding <=> b.embedding) AS sim "
                f"FROM {table} a "
                f"JOIN LATERAL (SELECT {id_col}, embedding FROM {table} "
                f"              WHERE {id_col} <> a.{id_col} AND embedding IS NOT NULL "
                f"                AND {id_col} = ANY(%s) "
                f"              ORDER BY embedding <=> a.embedding LIMIT %s) b ON true "
                f"WHERE a.embedding IS NOT NULL AND a.{id_col} = ANY(%s)",
                (list(ids), _SYNAPSES_PER_FACT, list(ids)))
            rows = cur.fetchall()
    except Exception:
        return None
    seen, edges = set(), []
    for a, b, sim in rows:
        if sim is None or float(sim) < floor:
            continue
        key = tuple(sorted((str(a), str(b))))
        if key in seen:
            continue
        seen.add(key)
        edges.append({"a": str(a), "b": str(b), "weight": round(float(sim), 4)})
    return edges


def _synapses(ids: list, vectors: list, floor: float = _SYNAPSE_FLOOR) -> list:
    """Top-k cosine neighbours per fact — the memory mesh.

    Mutual pairs only counted once; a fact's k strongest neighbours rather
    than every pair over a threshold, so a dense region does not become a
    solid blob that hides the structure inside it.
    """
    import numpy as np

    usable = [(i, v) for i, v in enumerate(vectors) if v]
    if len(usable) < 2:
        return []
    index = [i for i, _ in usable]
    matrix = np.asarray([v for _, v in usable], dtype=float)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    similarity = (matrix / norms) @ (matrix / norms).T
    np.fill_diagonal(similarity, -1.0)

    seen, edges = set(), []
    k = min(_SYNAPSES_PER_FACT, len(usable) - 1)
    for row in range(len(usable)):
        for column in np.argsort(-similarity[row])[:k]:
            weight = float(similarity[row][column])
            if weight < floor:
                continue
            a, b = ids[index[row]], ids[index[column]]
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            edges.append({"a": key[0], "b": key[1], "kind": "synapse",
                          "weight": round(weight, 3)})
    return edges


# Which tool family manages which kind of task. Not a guess about
# content — the task TYPE determines the tools that operate on it, which
# is what wires the skill lobe to the task lobe.
def _places() -> list:
    """Remembered places with their current visit state and the documents
    that name them. The trigger module is imported for its state readers
    only; it talks to nothing at import."""
    try:
        from kyraan.triggers import whereabouts
        state = whereabouts._load()
    except Exception:
        return []
    out = []
    visits = state.get("visits") or {}
    for name, place in (state.get("places") or {}).items():
        visit = visits.get(name) or {}
        entry = dict(place, name=name, inside=bool(visit.get("inside")), at=str(visit.get("at") or ""),
                     mentioned_by=[])
        try:
            from kyraan.store import pg
            with pg.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM document WHERE coalesce(suppressed_by, '{}') = '{}' "
                    "AND (caption ILIKE %s OR text ILIKE %s OR %s = ANY(entities)) LIMIT 20",
                    (f"%{name}%", f"%{name}%", name))
                entry["mentioned_by"] = [str(r[0]) for r in cur.fetchall()]
        except Exception:
            pass
        out.append(entry)
    return out


def _care_doses() -> list:
    """Kiaan's vaccination doses as the keeper sees them: done with a date
    and source, or upcoming with a due date and a status."""
    try:
        from kyraan.triggers import kiaan_keeper as keeper
        born = keeper.birth_date()
        if born is None:
            return []
        done = keeper.done_map()
        today = keeper.local_now().date()
        upcoming = keeper.upcoming(born, done, today)
    except Exception:
        return []
    labels = {row[0]: row[1] for row in keeper.SCHEDULE}
    out = []
    for sid, rec in done.items():
        out.append({"id": sid, "label": labels.get(sid, sid), "status": "done",
                    "date": str(rec.get("date", "")), "source": str(rec.get("source", "")),
                    "person": keeper.PERSON})
    for sid, label, due, status in upcoming:
        out.append({"id": sid, "label": label, "status": status, "due": due.isoformat(),
                    "person": keeper.PERSON})
    return out


def _code_jobs() -> list:
    """The coding-task ledger (tools/code_agent.JOBS_PATH), read, never
    imported: the tool module talks to the outside world at import."""
    path = Path(__file__).resolve().parents[3] / "data" / "code_jobs.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [j for j in data if isinstance(j, dict)] if isinstance(data, list) else []


_TASK_TOOLS = {
    "reminder": ("reminders.create", "reminders.list", "reminders.cancel"),
    "agent_task": ("tasks.create", "tasks.list", "tasks.cancel"),
    "goal": ("goals.create", "goals.list", "goals.show", "goals.update"),
}


_graph_cache: dict = {}
GRAPH_TTL_S = 30


def _vault_name() -> str:
    """The Obsidian vault's name is its folder's basename — that is what
    obsidian://open?vault= wants. Empty when no vault is configured."""
    try:
        from kyraan.store import notes
        root = notes.vault_root()
    except Exception:
        return ""
    return root.name if root else ""


def _obsidian_url(vault: str, source_path: str) -> str:
    """obsidian://open?vault=<name>&file=<vault-relative path, no .md>.
    Only a note in a configured vault can have one; facts live in the
    memory tree, which is not inside the vault, so they get a path and
    no link — saying so beats a link that opens nothing."""
    if not vault or not source_path:
        return ""
    rel = source_path[:-3] if source_path.endswith(".md") else source_path
    return f"obsidian://open?vault={quote(vault, safe='')}&file={quote(rel, safe='/')}"


def _slug(value: str) -> str:
    import re as _re
    return _re.sub(r"[\s,]+", "_", str(value or "").strip().lower())


def _contact_links(person_ids: set) -> tuple:
    """(total contacts, [links]) — a link only where a contact provably
    names a registry person. `is`: the whole name is a registry name or
    alias. `maybe`: one token of the name is an alias — a candidate, not
    a claim. Phones/emails ride along for the Selection panel and nowhere
    else."""
    from kyraan.store import pg
    try:
        from kyraan.store import persons
        name_map = persons.name_map()
    except Exception:
        name_map = {}
    try:
        with pg.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT resource, name, phones, emails FROM contact ORDER BY name")
            rows = cur.fetchall()
    except Exception:
        return 0, []

    aliases = {_slug(alias): pid for alias, pid in name_map.items()}
    links = []
    for resource, name, phones, emails in rows:
        whole = _slug(name)
        kind, person = None, None
        if whole in aliases:
            kind, person = "is", aliases[whole]
        else:
            tokens = [_slug(t) for t in str(name or "").split()]
            hits = {aliases[t] for t in tokens if t in aliases}
            if len(hits) == 1:
                kind, person = "maybe", hits.pop()
        if not kind or f"p:{person}" not in person_ids:
            continue
        links.append({
            "id": f"c:{resource}", "resource": resource, "name": name,
            "kind": kind, "person": f"p:{person}",
            "phones": list(phones or []), "emails": list(emails or []),
        })
    return len(rows), links


def contacts_search(query: str, limit: int = 8) -> dict:
    """The rest of the book, by name. For the search box: a name that has
    no neuron still answers, marked as outside the brain."""
    if not query.strip():
        return {"contacts": [], "query": query}
    try:
        from kyraan.store import contacts
        rows = contacts.find(query, limit=limit) or []
    except Exception as exc:
        return {"contacts": [], "query": query, "degraded": f"{type(exc).__name__}: {exc}"}
    return {"contacts": [{"name": r["name"], "phones": r["phones"], "emails": r["emails"]}
                         for r in rows], "query": query}


def brain_graph(synapse_floor: float = _SYNAPSE_FLOOR, fresh: bool = False) -> dict:
    """One graph over the whole second brain: memories, the people they
    are about, the scheduled work, and the skills that act.

    Every edge is evidence, never decoration:
      synapse      fact-fact cosine similarity over the stored embeddings
      subject      a fact is ABOUT this person
      relation     a stored triple (head -> tail)
      owns         this person's scheduled work
      managed_by   the tool family that operates on this kind of task
      coactivation these two tools fired in the same turn, N times
    """
    # The key includes demo mode: without it, flipping KYRAAN_PANEL_DEMO
    # served whichever graph was cached first — the live brain labelled as
    # demo, or worse, the reverse.
    from kyraan.panel import demo as _demo_mode
    cache_key = (round(synapse_floor, 3), _demo_mode.enabled())
    cached = _graph_cache.get(cache_key)
    # `fresh` is the page saying "the stream just told me the store
    # changed" — the one caller that knows better than a 30s memo.
    if cached and not fresh and time.monotonic() - cached[0] < GRAPH_TTL_S:
        return cached[1]

    nodes, edges = [], []

    # --- memory lobe ------------------------------------------------------
    memory = memory_map()
    fact_ids, vectors = [], []
    subjects = set()
    for fact in memory["facts"]:
        fact_ids.append(fact["id"])
        subjects.add(fact["subject"])
        nodes.append({
            "id": f"m:{fact['id']}", "type": "memory", "label": fact["content"],
            "lobe": "memory", "group": str(fact.get("cluster", -1)),
            "subject": fact["subject"], "kind": fact["kind"],
            "active": fact["active"], "importance": fact["importance"],
            "created": fact["created"],
            # short = forgotten 14 days after it was learned; the brain
            # draws it hollow (owner, 2026-09-04: "show the short-term
            # memories in the brain?" — they were there, indistinguishable).
            "term": fact.get("term") or "long",
        })

    # memory_map drops the raw vectors; re-read them for the mesh.
    from kyraan.panel import demo
    if demo.enabled():
        # NOT `vectors` — that name is the accumulator this function is
        # about to append to, and unpacking into it left 480 entries
        # against 240 ids.
        demo_rows, demo_vectors = demo.facts()
        raw = {row["id"]: vector for row, vector in zip(demo_rows, demo_vectors)}
    else:
        from kyraan.store import pg
        try:
            with pg.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT legacy_id, embedding FROM fact "
                            "ORDER BY created_at")
                raw = {legacy: vector for legacy, vector in cur.fetchall()}
        except Exception:
            raw = {}
    for fact_id in fact_ids:
        vector = raw.get(fact_id)
        if isinstance(vector, str):
            try:
                vector = json.loads(vector)
            except json.JSONDecodeError:
                vector = None
        vectors.append(list(vector) if vector is not None else None)

    fact_mesh = None if demo.enabled() else _synapses_pg("fact", "legacy_id", fact_ids, synapse_floor)
    for edge in (fact_mesh if fact_mesh is not None else _synapses(fact_ids, vectors, floor=synapse_floor)):
        edges.append({"a": f"m:{edge['a']}", "b": f"m:{edge['b']}",
                      "kind": "synapse", "weight": edge["weight"]})

    # --- people -----------------------------------------------------------
    # The registry, not just whoever a fact happens to be about. Kamal and
    # Titu have enrolled FACES and no facts; keyed off subjects alone they
    # had nowhere to attach and their faces floated unlinked.
    registry_people: dict = {}
    try:
        from kyraan.store import persons as _persons
        registry_people = {row[0]: row[1] for row in _persons.list_persons()}
    except Exception:
        pass
    # The owner's chat lives in the environment, not the registry row.
    import os as _os
    if "owner" in registry_people and not registry_people["owner"]:
        try:
            registry_people["owner"] = int(_os.environ.get("TELEGRAM_OWNER_ID", "0")) or None
        except ValueError:
            pass
    for person in sorted(set(subjects) | set(registry_people)):
        nodes.append({"id": f"p:{person}", "type": "person", "label": person,
                      "lobe": "memory", "group": person,
                      "registered": person in registry_people,
                      # A live model_call names a chat; this is how the page
                      # finds whose turn is being thought about.
                      "chat_id": registry_people.get(person)})
    for fact in memory["facts"]:
        edges.append({"a": f"m:{fact['id']}", "b": f"p:{fact['subject']}",
                      "kind": "subject", "weight": 0.5})

    links = memory_links()
    known = {p.lower() for p in subjects}
    # Several facts can assert the SAME relation (three separate memories
    # each say Kiaan is the owner's son). That is corroboration, not three
    # relationships — draw one edge and carry the count.
    relation_edges: dict = {}
    for link in links["links"]:
        head, tail = (link["from"] or "").lower(), (link["to"] or "").lower()
        if head not in known or tail not in known or head == tail:
            continue
        key = (head, tail, link["rel"])
        existing = relation_edges.get(key)
        if existing:
            existing["sources"] += 1
            continue
        relation_edges[key] = {
            "a": f"p:{head}", "b": f"p:{tail}", "kind": "relation",
            "weight": 0.8, "label": link["rel"], "sources": 1,
            "contested": link.get("contested", False),
            "variant": link.get("variant", False)}
    edges.extend(relation_edges.values())

    # --- recall, documents, faces ----------------------------------------
    # The brain was showing the SMALLER half of memory: 43 curated facts,
    # while the store also holds 179 episodes, 10 documents and the face
    # templates. All three carry embeddings, so they belong on the same
    # mesh rather than in a list somewhere else.
    from kyraan.panel import demo as _demo
    episodes, documents, faces, chunk_rows = [], [], [], []
    vault = _vault_name() if not _demo.enabled() else ""
    if not _demo.enabled():
        try:
            with pg.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id, day, participants, fact_refs, text, embedding "
                    "FROM episode ORDER BY created_at DESC LIMIT 400")
                episodes = cur.fetchall()
                cur.execute(
                    "SELECT d.id, d.kind, d.caption, d.filename, "
                    "       d.subject_persons, d.created_at, "
                    "       (SELECT count(*) FROM document_chunk c "
                    "        WHERE c.document_id = d.id), "
                    "       d.source_path, d.entities, d.event_date, "
                    # The index marks a live row with an EMPTY array, not
                    # NULL. IS NOT NULL called every note dead, including
                    # the current one.
                    "       coalesce(d.suppressed_by, '{}') <> '{}', "
                    "       d.related, d.uploaded_by "
                    "FROM document d ORDER BY d.created_at DESC")
                documents = cur.fetchall()
                cur.execute("SELECT slug, name, created_at FROM face_template "
                            "ORDER BY slug")
                faces = cur.fetchall()
                # Every chunk carries the same 384-d embedding the facts
                # do, so a document can earn synapses by the same rule.
                cur.execute("SELECT document_id, embedding FROM document_chunk "
                            "WHERE embedding IS NOT NULL")
                chunk_rows = cur.fetchall()
        except Exception:
            pass

    person_ids = {n["id"] for n in nodes if n["type"] == "person"}
    memory_node_ids = {n["id"] for n in nodes if n["type"] == "memory"}

    def _person_node(name: str) -> str | None:
        """Faces and documents name people in their own spelling. Resolve
        through the registry so an edge lands on THE person node."""
        if not name:
            return None
        candidate = str(name).strip().lower().replace("-", "_")
        try:
            from kyraan.store import persons
            candidate = persons.resolve(candidate) or candidate
        except Exception:
            pass
        return f"p:{candidate}" if f"p:{candidate}" in person_ids else None

    episode_ids, episode_vectors = [], []
    for row in episodes:
        (episode_id, day, participants, fact_refs, text, embedding) = row
        node_id = f"e:{episode_id}"
        episode_ids.append(str(episode_id))
        episode_vectors.append(_as_vector(embedding))
        nodes.append({
            "id": node_id, "type": "episode", "lobe": "recall",
            "label": _clip((text or "").replace("\n", " · "), 220),
            "group": str(day) if day else "undated",
            "day": str(day) if day else "",
            "participants": list(participants or []),
            "created": str(day or ""),
        })
        for who in (participants or []):
            target = _person_node(who)
            if target:
                edges.append({"a": node_id, "b": target, "kind": "spoke",
                              "weight": 0.35})
        # An episode that cites facts is the strongest link in the store:
        # it says this conversation is WHY that fact is known.
        for ref in (fact_refs or []):
            if f"m:{ref}" in memory_node_ids:
                edges.append({"a": node_id, "b": f"m:{ref}", "kind": "recalls",
                              "weight": 0.9})

    episode_mesh = None if _demo.enabled() else _synapses_pg("episode", "id", episode_ids, synapse_floor)
    for edge in (episode_mesh if episode_mesh is not None
                 else _synapses(episode_ids, episode_vectors, floor=synapse_floor)):
        edges.append({"a": f"e:{edge['a']}", "b": f"e:{edge['b']}",
                      "kind": "synapse", "weight": edge["weight"]})

    # Obsidian notes ride in the document table as kind='note' (migration
    # 018). They get their own lobe: a note is something the OWNER wrote,
    # which is a different kind of memory from a photo Kyraan was sent.
    # What the index stores for a note: the people it is about (registry
    # ids, in subject_persons), its #tags and relation: lines (entities),
    # an event date, and its vault-relative path — which is what makes an
    # obsidian:// deep link possible. Note-to-note wikilinks are NOT
    # stored, so none are drawn.
    tag_owners: dict = defaultdict(list)
    # A note that was edited four times is one note with four versions,
    # not four neurons. Collapse by vault path: keep the live row, or the
    # newest if every version is superseded (the note was deleted), and
    # carry the version count. Rows arrive newest first.
    seen_paths: dict = {}
    related_drawn: set = set()
    doc_kind = {str(row[0]): row[1] for row in documents}
    collapsed = []
    for row in documents:
        kind, source_path, suppressed = row[1], row[7], row[10]
        if kind != "note" or not source_path:
            collapsed.append(row)
            continue
        entry = seen_paths.get(source_path)
        if entry is None:
            seen_paths[source_path] = {"row": row, "versions": 1}
        else:
            entry["versions"] += 1
            if entry["row"][10] and not suppressed:    # a live one beats a dead newer
                entry["row"] = row
    note_versions = {id(e["row"]): e["versions"] for e in seen_paths.values()}
    collapsed.extend(e["row"] for e in seen_paths.values())

    for row in collapsed:
        (doc_id, kind, caption, filename, subjects, created, chunks,
         source_path, entities, event_date, suppressed, *rest) = row
        related = rest[0] if rest else []     # older fixtures carry no column
        uploaded_by = rest[1] if len(rest) > 1 else ""
        is_note = kind == "note"
        node_id = f"d:{doc_id}"
        node = {
            "id": node_id, "type": "note" if is_note else "document",
            "lobe": "notes" if is_note else "docs",
            "label": caption or filename or str(doc_id)[:8],
            "group": kind or "file", "doc_kind": kind or "file",
            "chunks": chunks, "filename": filename or "",
            "created": created.isoformat() if created else "",
        }
        if is_note:
            tags = [e for e in (entities or []) if str(e).startswith("#")]
            node.update({
                "path": source_path or "",
                "tags": tags,
                "relations": [e.split(":", 1)[1].strip() for e in (entities or [])
                              if str(e).startswith("relation:")],
                "event_date": event_date.isoformat() if event_date else "",
                # Every version superseded means the note is gone from the
                # vault: kept, dimmed. A live version is simply live.
                "active": not suppressed,
                "versions": note_versions.get(id(row), 1),
                "obsidian_url": _obsidian_url(vault, source_path),
            })
            for tag in tags:
                tag_owners[tag].append(node_id)
        else:
            # Photos and documents carry entities too (2026-09-02): the
            # named things the vision pass read off the image — brand,
            # product, place — plus one #category. Shown on the node,
            # and hub-joined exactly like note tags so two photos of the
            # same brand meet at one neuron.
            ents = [str(e) for e in (entities or [])]
            node.update({"tags": [e for e in ents if e.startswith("#")],
                         "entities": [e for e in ents if not e.startswith("#")]})
            for ent in ents:
                tag_owners[ent].append(node_id)
        nodes.append(node)
        for who in (subjects or []):
            target = _person_node(who)
            if target:
                edges.append({"a": node_id, "b": target, "kind": "about",
                              "weight": 0.6})
        if uploaded_by and uploaded_by not in (subjects or []):
            target = _person_node(uploaded_by)   # who sent it (2026-09-04)
            if target:
                edges.append({"a": target, "b": node_id, "kind": "uploaded",
                              "weight": 0.5})
        # A capture and the note it illustrates (documents.relate, owner
        # 2026-09-03): the milestone note and the milestone photo are one
        # story, drawn once (each row carries the other's id).
        for rid in (related or []):
            a, b = sorted((node_id, f"d:{rid}"))
            if (a, b) not in related_drawn:
                related_drawn.add((a, b))
                # two notes related to each other is an Obsidian [[wikilink]]
                # (notes.link_wikilinks, 2026-09-03); note<->capture is the
                # capture illustrating the note
                pair_kind = ("wikilink" if is_note and doc_kind.get(str(rid)) == "note"
                             else "illustrates")
                edges.append({"a": a, "b": b, "kind": pair_kind,
                              "weight": 0.7})

    # A tag becomes a node only when it joins notes: two or more sharing
    # #friend is a grouping worth a hub; one note's private tag is a
    # detail for its Selection panel, not a neuron.
    for tag, owners in tag_owners.items():
        if len(owners) < 2:
            continue
        tag_id = f"g:{tag}"
        nodes.append({"id": tag_id, "type": "tag", "lobe": "notes",
                      "label": tag, "group": "tag", "notes": len(owners)})
        for owner in owners:
            edges.append({"a": owner, "b": tag_id, "kind": "tagged", "weight": 0.5})

    # Documents and notes join the synapse mesh (owner 2026-09-03: "some
    # documents are not linked yet, they might have connections" — five
    # of 21 had nothing but their wire to the core). A document's vector
    # is the mean of its chunks' unit vectors; it is then meshed with the
    # facts, the episodes and the other documents under the SAME top-k /
    # floor rule as a fact, and only the edges that touch a document are
    # kept — the fact-fact and episode-episode meshes are already drawn.
    # So a vaccination card meets the fact about the vaccination and the
    # conversation it was sent in, by evidence in the store, not by name.
    doc_ids = [n["id"] for n in nodes if n["type"] in ("document", "note")]
    if doc_ids and chunk_rows:
        import numpy as np
        by_doc: dict = defaultdict(list)
        for doc_id, embedding in chunk_rows:
            vec = _as_vector(embedding)
            if vec:
                by_doc[f"d:{doc_id}"].append(vec)
        doc_vectors = {}
        for doc_id, vecs in by_doc.items():
            if doc_id not in doc_ids:
                continue
            dims = Counter(len(v) for v in vecs).most_common(1)[0][0]
            matrix = np.asarray([v for v in vecs if len(v) == dims], dtype=float)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            doc_vectors[doc_id] = (matrix / norms).mean(axis=0).tolist()
        if doc_vectors:
            dims = Counter(len(v) for v in doc_vectors.values()).most_common(1)[0][0]
            mesh_ids = [i for i, v in doc_vectors.items() if len(v) == dims]
            mesh_vectors = [doc_vectors[i] for i in mesh_ids]
            for prefix, ids_, vecs_ in (("m:", fact_ids, vectors),
                                        ("e:", episode_ids, episode_vectors)):
                for id_, vec in zip(ids_, vecs_):
                    if vec and len(vec) == dims:
                        mesh_ids.append(prefix + str(id_))
                        mesh_vectors.append(vec)
            doc_set = set(doc_vectors)
            for edge in _synapses(mesh_ids, mesh_vectors, floor=synapse_floor):
                if edge["a"] in doc_set or edge["b"] in doc_set:
                    edges.append({"a": edge["a"], "b": edge["b"],
                                  "kind": "synapse", "weight": edge["weight"]})

    seen_faces: dict = {}
    for (slug, name, created) in faces:
        # Several templates per person is normal (multiple enrolments);
        # one node per PERSON carrying the count is the useful reading.
        entry = seen_faces.setdefault(slug, {
            "id": f"f:{slug}", "type": "face", "lobe": "faces",
            "label": name or slug, "group": "face", "templates": 0,
            "created": created.isoformat() if created else "",
        })
        entry["templates"] += 1
    for entry in seen_faces.values():
        nodes.append(entry)
        # the DISPLAY name is what becomes a person's alias ("Akansha
        # (employee)" -> akansha, owner 2026-09-03); the slug is the
        # fallback for faces enrolled under a plain registry name
        target = _person_node(entry["label"]) or _person_node(entry["id"][2:])
        if target:
            edges.append({"a": entry["id"], "b": target, "kind": "recognises",
                          "weight": 0.8})

    # --- contacts ---------------------------------------------------------
    # 395 entries in the book; the brain knows a handful. Only the ones
    # that provably touch a registry person become neurons: an EXACT
    # full-name match is an `is` wire, a match on a single alias token
    # ("Habu New" → kamal via the alias "habu") is a `maybe` wire — listed
    # as a candidate, never asserted, because first names are ambiguous
    # in a book this size ("Suman Sutradhar" is not Suman Ghosh). The rest
    # stay out of the graph and reachable through search.
    contacts_total, contact_links = 0, []
    if not _demo.enabled():
        contacts_total, contact_links = _contact_links(person_ids)
        for link in contact_links:
            nodes.append({
                "id": link["id"], "type": "contact", "lobe": "contacts",
                "label": link["name"], "group": link["kind"],
                "phones": link["phones"], "emails": link["emails"],
                "resource": link["resource"], "match": link["kind"],
                "person": link["person"][2:],
            })
            edges.append({"a": link["id"], "b": link["person"],
                          "kind": link["kind"], "weight": 0.7 if link["kind"] == "is" else 0.3})

    # --- task lobe --------------------------------------------------------
    board = triggers()
    for item in board["triggers"]:
        if not item.get("id"):
            continue
        node_id = f"t:{item['type']}:{item['id']}"
        nodes.append({
            "id": node_id, "type": "task", "label": item["text"],
            "lobe": "task", "group": item["type"], "task_type": item["type"],
            "overdue": item["fire"]["overdue"], "repeat": item.get("repeat", ""),
            "fires_in": item["fire"]["in_seconds"],
        })
        owner = item.get("person") or "owner"
        if f"p:{owner}" in {n["id"] for n in nodes}:
            edges.append({"a": node_id, "b": f"p:{owner}", "kind": "owns",
                          "weight": 0.4})

    # Coding tasks (tools/code_agent, 2026-09-03) are work too: a job
    # Kyraan handed to Claude Code in its own worktree. They live in
    # data/code_jobs.json, not the trigger board, so they join the work
    # lobe from there — queued, running, done, failed or discarded — owned
    # by whoever asked, resolved through the registry's chat ids.
    by_chat = {str(chat): person for person, chat in registry_people.items() if chat}
    for job in _code_jobs():
        if not job.get("id"):
            continue
        node_id = f"t:code:{job['id']}"
        status = job.get("status", "?")
        nodes.append({
            "id": node_id, "type": "task", "label": _clip(job.get("task", ""), 140),
            "lobe": "task", "group": "code", "task_type": "code",
            "overdue": status == "failed", "repeat": "", "fires_in": None,
            "status": status, "branch": job.get("branch", ""),
            "created": str(job.get("started") or job.get("created") or ""),
        })
        owner = by_chat.get(str(job.get("chat_id", "")))
        if owner and f"p:{owner}" in {n["id"] for n in nodes}:
            edges.append({"a": node_id, "b": f"p:{owner}", "kind": "owns", "weight": 0.4})

    # --- places (whereabouts) ----------------------------------------------
    # A place the owner told Kyraan to remember ("remember this as the
    # office"): a neuron with its radius and since-when; `at` to the owner
    # while a visit says they are inside it; `mentions` from any document
    # or note that names it. Empty until the owner remembers a place.
    for place in _places():
        pid = f"l:{place['name']}"
        nodes.append({
            "id": pid, "type": "place", "lobe": "places", "label": place["name"],
            "group": "place", "radius_km": place.get("radius_km"),
            "created": place.get("since", ""), "inside": place.get("inside", False),
            "last_visit": place.get("at", ""),
        })
        if place.get("inside") and "p:owner" in {n["id"] for n in nodes}:
            edges.append({"a": pid, "b": "p:owner", "kind": "at", "weight": 0.6})
        for doc_id in place.get("mentioned_by", []):
            if f"d:{doc_id}" in {n["id"] for n in nodes}:
                edges.append({"a": f"d:{doc_id}", "b": pid, "kind": "mentions", "weight": 0.5})

    # --- care (Kiaan's keeper) ---------------------------------------------
    # The vaccination schedule the keeper owns: one neuron per dose, done
    # (dated, with its source: the card, a photo, the owner's word) or
    # due / due-soon / overdue / later from his birth date. Owned by the
    # child. Nothing until the keeper knows the birth date.
    for dose in _care_doses():
        did = f"k:{dose['id']}"
        nodes.append({
            "id": did, "type": "care", "lobe": "care", "label": dose["label"],
            "group": dose["status"], "status": dose["status"], "due": dose.get("due", ""),
            "done_on": dose.get("date", ""), "source": dose.get("source", ""),
            "overdue": dose["status"] == "overdue", "created": dose.get("date") or dose.get("due", ""),
        })
        if f"p:{dose['person']}" in {n["id"] for n in nodes}:
            edges.append({"a": did, "b": f"p:{dose['person']}", "kind": "owns", "weight": 0.5})

    # --- skill lobe -------------------------------------------------------
    usage, pairs = _tool_activity()
    registry = {}
    try:
        from kyraan.control_plane import config
        registry = config.load().get("tools") or {}
    except Exception:
        registry = {}

    for name in sorted(set(usage) | {t for t in registry
                                     if not t.startswith(_NOT_A_TOOL)}):
        spec = registry.get(name) or {}
        nodes.append({
            "id": f"s:{name}", "type": "skill", "label": name, "lobe": "skill",
            "group": spec.get("server") or name.split(".")[0],
            "permission": spec.get("permission", "—"),
            "side_effects": spec.get("side_effects", "—"),
            "uses": usage.get(name, 0),
            "registered": name in registry,
        })
    skill_ids = {n["id"] for n in nodes if n["type"] == "skill"}
    for (left, right), count in pairs.items():
        if f"s:{left}" in skill_ids and f"s:{right}" in skill_ids:
            edges.append({"a": f"s:{left}", "b": f"s:{right}",
                          "kind": "coactivation", "weight": count})

    for node in [n for n in nodes if n["type"] == "task"]:
        tools = (_TASK_TOOLS.get(node["task_type"], ()) if node["task_type"] != "code"
                 else [s[2:] for s in skill_ids if s.startswith("s:code.")])
        for tool in tools:
            if f"s:{tool}" in skill_ids:
                edges.append({"a": node["id"], "b": f"s:{tool}",
                              "kind": "managed_by", "weight": 0.3})

    # Two findings the graph can make that a list cannot:
    #   an ORPHAN memory has no synapse above the floor — either genuinely
    #   unique or badly embedded, and you cannot tell which from a list;
    #   a DEAD skill is registered but has never been called — capability
    #   that exists on paper only.
    wired = set()
    for edge in edges:
        if edge["kind"] == "synapse":
            wired.add(edge["a"])
            wired.add(edge["b"])
    for node in nodes:
        if node["type"] == "memory":
            node["orphan"] = node["id"] not in wired
        elif node["type"] == "skill":
            node["dead"] = node["registered"] and not node["uses"]

    # THE CORE (owner 2026-09-02: "put Kyraan at the centre of the brain,
    # from where signals trigger and are received"). One node for Kyraan
    # itself, wired last so every lobe exists: signals OUT to the skills
    # it acts through and the scheduled things it will fire; signals IN
    # from every photo, file and note it received or indexed; and the
    # owner it talks with both ways. No document can be an orphan — it
    # was received, and that is a connection. The physics centres it.
    nodes.append({"id": "k:kyraan", "type": "core", "label": "kyraan",
                  "lobe": "core", "group": "core"})
    for n in nodes:
        if n["id"] == "k:kyraan":
            continue
        kind = ("acts" if n["type"] == "skill" else
                "fires" if n["type"] == "task" else
                "received" if n["type"] in ("document", "note") else
                "talks" if n["id"] == "p:owner" else "")
        if kind:
            edges.append({"a": "k:kyraan", "b": n["id"], "kind": kind,
                          "weight": {"acts": 0.5, "fires": 0.4,
                                     "received": 0.15, "talks": 0.6}[kind]})

    counts: Counter = Counter(n["type"] for n in nodes)
    edge_counts: Counter = Counter(e["kind"] for e in edges)
    maybe_contacts = [n["label"] + " → " + n["person"]
                      for n in nodes if n["type"] == "contact" and n["match"] == "maybe"]
    result = {
        "nodes": nodes, "edges": edges,
        "counts": dict(counts), "edge_counts": dict(edge_counts),
        "contested": links.get("contested", []),
        "variants": links.get("variants", []),
        "orphans": [n["id"] for n in nodes if n.get("orphan")],
        "contacts_total": contacts_total,
        "vault": vault,
        "maybe_contacts": maybe_contacts,
        "dead_skills": [n["label"] for n in nodes if n.get("dead")],
        "synapse_floor": synapse_floor,
        "degraded": memory.get("degraded", ""),
        "demo": bool(memory.get("demo")),
    }
    _graph_cache[cache_key] = (time.monotonic(), result)
    return result


# --------------------------------------------------------------------------
# workload — what OUR OWN records say the models cost


def workload(hours: float = 24) -> dict:
    """Model calls grouped by model, with wall time as well as tokens.

    The host panel answers "what is holding the machine's memory" (the
    local model, by a mile). This answers the other half — "where does the
    TIME and the money go" — and only the audit log can, because the OS
    cannot see that a 6 GB llama-server process was serving a degraded
    fallback rather than a chosen call.
    """
    since = _since_iso(hours)
    by_model: dict = {}
    total_ms = 0
    for record in _iter_records(_event_files(), since=since, needle='"model_call"'):
        if record.get("kind") != "model_call":
            continue
        key = f"{record.get('provider', '?')}/{record.get('model', '?')}"
        row = by_model.setdefault(key, {
            "model": key, "tier": record.get("tier", "?"), "calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "cost_usd": 0.0, "ms": 0, "errors": 0,
        })
        row["calls"] += 1
        row["input_tokens"] += record.get("input_tokens") or 0
        row["output_tokens"] += record.get("output_tokens") or 0
        row["cached_tokens"] += record.get("cached_tokens") or 0
        row["cost_usd"] = round(row["cost_usd"] + (record.get("cost_usd") or 0), 6)
        latency = record.get("latency_ms") or 0
        row["ms"] += latency
        total_ms += latency

    for record in _iter_records(_event_files(), since=since,
                                needle='"model_call_error"'):
        if record.get("kind") != "model_call_error":
            continue
        key = f"{record.get('provider', '?')}/{record.get('model', '?')}"
        if key in by_model:
            by_model[key]["errors"] += 1

    rows = sorted(by_model.values(), key=lambda r: -r["ms"])
    for row in rows:
        row["ms_share"] = round(row["ms"] / total_ms * 100, 1) if total_ms else 0
        row["avg_ms"] = round(row["ms"] / row["calls"]) if row["calls"] else 0
    return {"models": rows, "total_ms": total_ms, "hours": hours}


def host_now() -> dict:
    from kyraan.panel import host
    host.ensure_sampler()
    return host.snapshot()


def host_history() -> dict:
    from kyraan.panel import host
    host.ensure_sampler()
    return host.history()


# --------------------------------------------------------------------------
# routines — today's timeline: what fired, what is next, what is queued

# Events that mean a scheduled thing actually HAPPENED. The trigger stores
# only hold what is still pending, so "did the 9am reminder go out?" was
# unanswerable from them — the store forgets a one-shot the moment it fires.
_FIRED_KINDS = {
    "reminder_sent": ("reminder", "fired"),
    "reminder_recurred": ("reminder", "fired"),
    "reminder_send_failed": ("reminder", "failed"),
    "reminder_overdue": ("reminder", "late"),
    "agent_task_ran": ("agent_task", "fired"),
    "brief_sent": ("brief", "fired"),
    "evening_brief_sent": ("brief", "fired"),
    "brief_send_failed": ("brief", "failed"),
    "evening_brief_send_failed": ("brief", "failed"),
    "goal_cycle_ran": ("goal", "fired"),
    # The four duties (2026-09-03): each speaks first on its own clock,
    # so each belongs on the timeline the moment it does.
    "kiaan_keeper_sent": ("duty", "fired"),
    "house_steward_sent": ("duty", "fired"),
    "chief_of_staff_sent": ("duty", "fired"),
    "chief_of_staff_morning_failed": ("duty", "failed"),
    "chief_of_staff_prep_failed": ("duty", "failed"),
    "whereabouts_sent": ("duty", "fired"),
}

# What a duty's event is called on the timeline. A uuid-less kind needs a
# name, not "duty —".
_DUTY_LABELS = {
    "brief_sent": "the morning brief",
    "brief_send_failed": "the morning brief failed",
    "evening_brief_send_failed": "the evening brief failed",
    "kiaan_keeper_sent": "Kiaan's keeper spoke",
    "house_steward_sent": "the house steward spoke",
    "chief_of_staff_sent": "the chief of staff spoke",
    "chief_of_staff_morning_failed": "the chief of staff's morning failed",
    "chief_of_staff_prep_failed": "the chief of staff's prep failed",
    "whereabouts_sent": "whereabouts: met the owner on the way",
}


def routines(hours: float = 24) -> dict:
    """Today's schedule as one timeline: fired, next, queued.

    The trigger board answers "what is coming". This answers "what
    happened", which is the question after a machine sleeps through
    something — and the stores cannot answer it, because a one-shot
    reminder leaves them the moment it fires.
    """
    from kyraan.control_plane.dnd import local_now

    # The fired events carry ids, not text. A timeline of uuids is not a
    # timeline — resolve what the stores still know, and say so plainly
    # when they no longer do (a one-shot is gone the moment it fires).
    names = {}
    try:
        from kyraan.triggers import agent_tasks
        from kyraan.triggers import store as reminder_store
        for reminder in reminder_store.list_pending():
            names[reminder.id] = reminder.text
        for task in agent_tasks.list_active():
            names[task.id] = task.instruction
    except (OSError, ValueError, TypeError, KeyError):
        pass

    rows = []
    for record in _iter_records(_event_files(), since=_since_iso(hours)):
        entry = _FIRED_KINDS.get(record.get("kind", ""))
        if not entry:
            continue
        kind, status = entry
        ref = record.get("reminder_id") or record.get("task_id") or ""
        label = (record.get("text") or record.get("instruction")
                 or names.get(ref) or "")
        if not label:
            label = (_DUTY_LABELS.get(record.get("kind", ""))
                     or ("the morning brief" if "morning" in record.get("kind", "")
                         else "the evening brief" if "evening" in record.get("kind", "")
                         else kind + " " + (ref[:8] or "—")))
        rows.append({
            "at": record.get("ts", ""), "type": kind, "status": status,
            "text": _clip(label, 120), "id": ref[:8],
        })

    upcoming = []
    for item in triggers()["triggers"]:
        if not item.get("id"):
            continue
        fire = item["fire"]
        upcoming.append({
            "at": fire["iso"], "type": item["type"],
            "status": "overdue" if fire["overdue"] else "queued",
            "text": item["text"], "id": item["id"][:8],
            "in_seconds": fire["in_seconds"],
        })
    # Soonest pending is NEXT; everything behind it is queued. Overdue keeps
    # its own status — it is not "next", it is late.
    pending = sorted((u for u in upcoming if u["status"] == "queued"),
                     key=lambda u: u["in_seconds"] if u["in_seconds"] is not None else 1e12)
    if pending:
        pending[0]["status"] = "next"

    rows.sort(key=lambda r: r["at"])
    upcoming.sort(key=lambda r: r["at"] or "")
    counts: Counter = Counter(r["status"] for r in rows + upcoming)
    return {
        "fired": rows, "upcoming": upcoming,
        "timeline": rows + upcoming,
        "counts": dict(counts),
        "now": local_now().isoformat(),
    }


# --------------------------------------------------------------------------
# actions — what Kyraan has actually DONE


def actions(limit: int = 200, days: float = 30, chat_id: int | None = None) -> dict:
    """The action log: every side-effectful tool call, with its inverse.

    The panel's other sectors answer what Kyraan KNOWS, what is SCHEDULED,
    what it CAN do and what it COST. This is the one that answers what it
    DID to the calendar, the reminders and the memory — which is the
    question an owner-reviewed system exists to answer.

    Read-only, like everything else here: `undoable` is a STATE, not a
    button. Undoing is a write and belongs to Phase C, through the kernel.
    """
    from kyraan.store import pg

    limit = max(1, min(int(limit), 1000))
    since = datetime.now(timezone.utc) - timedelta(days=max(0.1, days))
    rows, degraded = [], ""
    try:
        with pg.connection() as conn, conn.cursor() as cur:
            where = "WHERE done_at >= %s" + ("" if chat_id is None else " AND chat_id = %s")
            params = [since] if chat_id is None else [since, chat_id]
            cur.execute(
                "SELECT id, chat_id, tool, args, undo_tool, undo_args, "
                "       done_at, undone_at FROM action_log "
                + where + " ORDER BY done_at DESC LIMIT %s", (*params, limit))
            rows = cur.fetchall()
    except Exception as exc:
        degraded = f"{type(exc).__name__}: {exc}"

    out = []
    for (action_id, chat, tool, args, undo_tool, undo_args, done_at, undone_at) in rows:
        out.append({
            "id": str(action_id)[:8], "chat_id": chat, "tool": tool,
            "args": args if isinstance(args, dict) else {},
            "undo_tool": undo_tool or "",
            "undoable": bool(undo_tool) and undone_at is None,
            "undone": undone_at is not None,
            "at": done_at.isoformat() if done_at else "",
            "undone_at": undone_at.isoformat() if undone_at else "",
        })

    by_tool: Counter = Counter(r["tool"] for r in out)
    return {
        "actions": out,
        "by_tool": [{"tool": t, "count": n} for t, n in by_tool.most_common()],
        "total": len(out),
        "undoable": sum(1 for r in out if r["undoable"]),
        "undone": sum(1 for r in out if r["undone"]),
        "irreversible": sum(1 for r in out if not r["undo_tool"]),
        "days": days,
        "degraded": degraded,
    }
