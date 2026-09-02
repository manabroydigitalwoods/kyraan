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
API_VERSION = 11

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
                "FROM fact ORDER BY created_at LIMIT %s", (limit,))
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
_TASK_TOOLS = {
    "reminder": ("reminders.create", "reminders.list", "reminders.cancel"),
    "agent_task": ("tasks.create", "tasks.list", "tasks.cancel"),
    "goal": ("goals.create", "goals.list", "goals.show", "goals.update"),
}


_graph_cache: dict = {}
GRAPH_TTL_S = 30


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

    for edge in _synapses(fact_ids, vectors, floor=synapse_floor):
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
    episodes, documents, faces = [], [], []
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
                    "        WHERE c.document_id = d.id) "
                    "FROM document d ORDER BY d.created_at DESC")
                documents = cur.fetchall()
                cur.execute("SELECT slug, name, created_at FROM face_template "
                            "ORDER BY slug")
                faces = cur.fetchall()
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

    for edge in _synapses(episode_ids, episode_vectors, floor=synapse_floor):
        edges.append({"a": f"e:{edge['a']}", "b": f"e:{edge['b']}",
                      "kind": "synapse", "weight": edge["weight"]})

    for (doc_id, kind, caption, filename, subjects, created, chunks) in documents:
        node_id = f"d:{doc_id}"
        nodes.append({
            "id": node_id, "type": "document", "lobe": "docs",
            "label": caption or filename or str(doc_id)[:8],
            "group": kind or "file", "doc_kind": kind or "file",
            "chunks": chunks, "filename": filename or "",
            "created": created.isoformat() if created else "",
        })
        for who in (subjects or []):
            target = _person_node(who)
            if target:
                edges.append({"a": node_id, "b": target, "kind": "about",
                              "weight": 0.6})

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
        target = _person_node(entry["id"][2:])
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
        for tool in _TASK_TOOLS.get(node["task_type"], ()):
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
    "goal_cycle_ran": ("goal", "fired"),
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
            label = ("the morning brief" if "morning" in record.get("kind", "")
                     else "the evening brief" if "evening" in record.get("kind", "")
                     else kind + " " + (ref[:8] or "—"))
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
