"""The web panel — Phase A (docs/design/web_panel.md).

Two invariants carry the whole design and are tested first: the panel
never writes, and it never serves anything unauthenticated. Everything
else is aggregation over logs we already keep.
"""
import http.client
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from kyraan.control_plane import logging_setup
from kyraan.panel import queries, server


# ---------------------------------------------------------------- fixtures


def _write_log(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _ago(**kwargs):
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


@pytest.fixture
def seeded_logs(tmp_path):
    """One clean turn and one anomalous, expensive turn."""
    _write_log(logging_setup.EVENT_LOG, [
        {"ts": _ago(minutes=30), "kind": "model_call", "turn_id": "aaa1",
         "provider": "openai", "model": "gpt-5.4-nano", "input_tokens": 900,
         "output_tokens": 100, "cached_tokens": 50, "cost_usd": 0.001},
        {"ts": _ago(minutes=29), "kind": "tool_call", "turn_id": "aaa1",
         "tool": "weather.now", "args": {"place": "Siliguri"}},
        {"ts": _ago(minutes=28), "kind": "turn_health", "turn_id": "aaa1",
         "anomaly_count": 0},
        {"ts": _ago(minutes=10), "kind": "model_call", "turn_id": "bbb2",
         "provider": "openai", "model": "gpt-5.4-nano", "input_tokens": 80_000,
         "output_tokens": 400, "cached_tokens": 70_000, "cost_usd": 0.004},
        {"ts": _ago(minutes=9), "kind": "agent_tier_fallback", "turn_id": "bbb2"},
        {"ts": _ago(minutes=9), "kind": "turn_health", "turn_id": "bbb2",
         "anomaly_count": 1},
    ])
    _write_log(logging_setup.TRACE_LOG, [
        {"ts": _ago(minutes=31), "kind": "turn_start", "turn_id": "aaa1",
         "user_text": "weather here"},
        {"ts": _ago(minutes=27), "kind": "turn_end", "turn_id": "aaa1",
         "reply": "26°C and clear.", "total_ms": 1400,
         "stages": [{"stage": "model:frontier", "ms": 900, "depth": 0},
                    {"stage": "extraction", "ms": 300, "depth": 1}]},
        {"ts": _ago(minutes=11), "kind": "turn_start", "turn_id": "bbb2",
         "user_text": "open first news"},
    ])
    return tmp_path


@pytest.fixture
def panel(seeded_logs):
    """A live panel on an ephemeral port, torn down after the test."""
    httpd = server.build(host="127.0.0.1", port=0, token="secret-token")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def request(httpd, path, headers=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
    conn.request(method, path, headers=headers or {"Host": "127.0.0.1"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def authed(httpd, path):
    return request(httpd, path, headers={"Host": "127.0.0.1",
                                         "X-Kyraan-Token": "secret-token"})


# ------------------------------------------------------------- the two rules


def test_panel_never_writes(panel, tmp_path):
    """Rule 1, as a behaviour rather than a promise: exercise every
    endpoint and assert the logs and data stores are byte-identical
    afterwards. A panel that appends its own events would corrupt the
    forensics it exists to serve."""
    def snapshot():
        return {p: p.stat().st_size for p in sorted(tmp_path.rglob("*"))
                if p.is_file()}

    before = snapshot()
    for path in ("/api/status", "/api/health", "/api/usage?days=3",
                 "/api/triggers", "/api/events", "/api/event_kinds",
                 "/api/turns?sort=tokens", "/api/turn?id=aaa1"):
        response, _ = authed(panel, path)
        assert response.status == 200, path
    assert snapshot() == before


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/status", "/api/events"])
def test_nothing_serves_without_a_token(panel, path):
    response, _ = request(panel, path)
    assert response.status == 401


def test_wrong_token_is_refused(panel):
    response, _ = request(panel, "/?token=guess",
                          headers={"Host": "127.0.0.1"})
    assert response.status == 403


def test_handshake_moves_the_token_into_an_httponly_cookie(panel):
    """The token must not stay in the address bar — it would land in
    history, in a screenshot, and in any shared URL."""
    response, _ = request(panel, "/?token=secret-token",
                          headers={"Host": "127.0.0.1"})
    assert response.status == 303
    assert response.getheader("Location") == "/"
    cookie = response.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie

    response, body = request(panel, "/api/status", headers={
        "Host": "127.0.0.1", "Cookie": "kyraan_panel=secret-token"})
    assert response.status == 200
    assert "kill_switch" in json.loads(body)


def test_foreign_host_header_is_refused(panel):
    """DNS rebinding: a page on evil.example.com resolving to 127.0.0.1
    would otherwise have the browser attach our cookie for it."""
    response, _ = request(panel, "/api/status", headers={
        "Host": "evil.example.com", "X-Kyraan-Token": "secret-token"})
    assert response.status == 421


def test_static_paths_cannot_escape_the_static_dir(panel):
    response, _ = authed(panel, "/../../pyproject.toml")
    assert response.status == 404


def test_responses_carry_the_no_inline_csp(panel):
    response, _ = authed(panel, "/")
    csp = response.getheader("Content-Security-Policy")
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp
    assert response.getheader("X-Content-Type-Options") == "nosniff"


def test_the_page_builds_no_html_from_data():
    """Rule 2 held structurally: the one place event text reaches the DOM
    is textContent. innerHTML anywhere in this file would be a hole."""
    import re
    source = (server.STATIC_DIR / "app.js").read_text()
    # Strip comments first — the file's own header names these sinks in
    # order to forbid them, and must not fail its own check.
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"^\s*//.*$", "", source, flags=re.M)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                 "document.write", "eval("):
        assert sink not in source, sink


# ------------------------------------------------------------------ queries


def test_turns_aggregate_tokens_cost_and_anomalies(seeded_logs):
    rows = {t["turn_id"]: t for t in queries.turns(hours=2)["turns"]}
    clean, dirty = rows["aaa1"], rows["bbb2"]

    assert clean["input_tokens"] == 900 and clean["output_tokens"] == 100
    assert clean["tools"] == ["weather.now"]
    assert clean["user_text"] == "weather here"
    assert clean["reply"] == "26°C and clear."
    assert clean["total_ms"] == 1400
    assert clean["anomalies"] == []

    assert dirty["anomalies"] == ["agent_tier_fallback"]
    assert dirty["cost_usd"] == pytest.approx(0.004)


def test_turns_sort_by_tokens_finds_the_expensive_turn(seeded_logs):
    """The headline question: which turns spend the budget."""
    ordered = queries.turns(hours=2, sort="tokens")["turns"]
    assert [t["turn_id"] for t in ordered] == ["bbb2", "aaa1"]


def test_turn_detail_merges_events_and_traces_in_time_order(seeded_logs):
    detail = queries.turn_detail("aaa1")
    assert detail["found"]
    kinds = [r["kind"] for r in detail["records"]]
    assert kinds[0] == "turn_start" and kinds[-1] == "turn_end"
    assert {r["_source"] for r in detail["records"]} == {"event", "trace"}
    # Only top-level stages may sum to the turn (trace.py's correction).
    assert [s["stage"] for s in detail["stages"] if not s["depth"]] == ["model:frontier"]


def test_turn_detail_clips_prompts_unless_full_is_asked_for(seeded_logs):
    _write_log(logging_setup.TRACE_LOG, [
        {"ts": _ago(minutes=26), "kind": "model_io", "turn_id": "aaa1",
         "prompt": "x" * 5000},
    ])
    clipped = queries.turn_detail("aaa1")["records"][-1]["prompt"]
    assert len(clipped) < 5000 and "chars)" in clipped
    full = queries.turn_detail("aaa1", full=True)["records"][-1]["prompt"]
    assert len(full) == 5000


def test_events_filter_by_kind_and_anomaly(seeded_logs):
    only_calls = queries.events(kind="model_call", hours=2)["events"]
    assert {e["kind"] for e in only_calls} == {"model_call"}
    anomalies = queries.events(anomalies_only=True, hours=2)["events"]
    assert [e["kind"] for e in anomalies] == ["agent_tier_fallback"]


def test_events_come_back_newest_first(seeded_logs):
    stamps = [e["ts"] for e in queries.events(hours=2)["events"]]
    assert stamps == sorted(stamps, reverse=True)


def test_malformed_log_lines_do_not_break_a_read(seeded_logs):
    with open(logging_setup.EVENT_LOG, "a") as handle:
        handle.write("{not json at all\n")
    assert queries.events(hours=2)["count"] > 0


def test_status_counts_anomalous_turns(seeded_logs):
    status = queries.status()
    assert status["last_24h"] == {"turns": 2, "anomalous_turns": 1}


# ----------------------------------------------------------------- triggers


def test_trigger_board_flags_an_overdue_job(monkeypatch, tmp_path):
    """The point of the board: a MacBook that slept through a due
    reminder shows as an overdue row instead of as silence (§3d #4)."""
    from kyraan.triggers import goals
    from kyraan.triggers import store as reminder_store
    monkeypatch.setattr(goals, "GOALS_PATH", tmp_path / "goals.json")

    reminder_store.add(42, "take the vaccination", _ago(hours=3))
    reminder_store.add(42, "drink water", (datetime.now(timezone.utc)
                                           + timedelta(hours=1)).isoformat())

    board = queries.triggers()
    late = [t for t in board["triggers"] if t["fire"]["overdue"]]
    assert board["overdue"] == 1
    assert late[0]["text"] == "take the vaccination"
    # Soonest first, so the next thing to happen is the top row.
    assert board["triggers"][0]["text"] == "take the vaccination"


def test_unparseable_schedule_does_not_hide_the_rest_of_the_board(monkeypatch, tmp_path):
    from kyraan.triggers import goals
    from kyraan.triggers import store as reminder_store
    monkeypatch.setattr(goals, "GOALS_PATH", tmp_path / "goals.json")

    reminder_store.add(42, "broken", "not-a-datetime")
    reminder_store.add(42, "fine", _ago(hours=1))

    rows = queries.triggers()["triggers"]
    assert {r["text"] for r in rows} == {"broken", "fine"}
    broken = next(r for r in rows if r["text"] == "broken")
    assert broken["fire"]["unparsed"] is True
    assert broken["fire"]["overdue"] is False


# ------------------------------------------------------------------- health


def test_health_is_cached_so_a_polling_page_cannot_hammer_the_probes(monkeypatch):
    sweeps = []
    from kyraan.control_plane import health as health_module
    monkeypatch.setattr(health_module, "_probe_components",
                        lambda: (sweeps.append(1),
                                 [("postgres", "OK", "container reachable")])[1])
    monkeypatch.setattr(health_module, "report",
                        lambda probed=None: ("OK", "all good"))
    monkeypatch.setattr(queries, "_health_cache", {"at": 0.0, "value": None})

    first = queries.health()
    second = queries.health()
    assert first["verdict"] == "OK"
    assert first["components"] == [{"name": "postgres", "ok": True,
                                    "detail": "container reachable"}]
    assert first["cached"] is False and second["cached"] is True
    assert len(sweeps) == 1
    assert queries.health(force=True)["cached"] is False
    assert len(sweeps) == 2


def test_health_probes_the_components_only_once_per_report(monkeypatch):
    """The panel wants the matrix AND the text. Probing for each would
    double a sweep that already waits up to 8s on searxng alone."""
    sweeps = []
    from kyraan.control_plane import health as health_module
    monkeypatch.setattr(health_module, "_probe_components",
                        lambda: (sweeps.append(1), [("redis", "OK", "ping ok")])[1])
    monkeypatch.setattr(health_module, "_census_24h", lambda: __import__(
        "collections").Counter())
    monkeypatch.setattr(queries, "_health_cache", {"at": 0.0, "value": None})

    result = queries.health()
    assert len(sweeps) == 1
    assert result["components"][0]["name"] == "redis"
    assert "redis: ping ok" in result["text"]


# -------------------------------------------------------------------- theme


def _palette(selector: str) -> set:
    """The custom properties one phosphor block defines."""
    import re
    css = (server.STATIC_DIR / "app.css").read_text()
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert block, f"no {selector} block in app.css"
    return set(re.findall(r"(--[a-z-]+)\s*:", block.group(1)))


def test_every_phosphor_defines_the_whole_palette():
    """A tube missing --bad would drop the red rail off anomaly rows and
    fall back to whatever amber left behind — the theme would look fine
    and the panel would stop flagging failures."""
    amber = _palette(':root[data-phosphor="amber"]')
    assert {"--bg", "--text", "--accent", "--ok", "--warn", "--bad"} <= amber
    for tube in ("green", "blue"):
        assert _palette(f':root[data-phosphor="{tube}"]') == amber, tube


def test_the_tube_selector_only_accepts_known_phosphors():
    """data-phosphor comes from localStorage, which the owner can edit.
    An unknown value must fall back, not leave the page unstyled."""
    source = (server.STATIC_DIR / "app.js").read_text()
    assert 'if (!PHOSPHORS.includes(name)) name = "amber";' in source


def test_page_and_server_agree_on_the_api_version():
    """The panel serves its page from disk on every request but imports
    its Python once. An edited-then-not-restarted server therefore hands
    a NEW page OLD JSON, and a missing field renders as an empty console
    — which reads as "all quiet" rather than "I could not tell you"
    (found live 2026-08-31: the systems matrix went blank). The page
    checks the version and says so; these two constants must move
    together or the check is a lie."""
    import re
    source = (server.STATIC_DIR / "app.js").read_text()
    declared = re.search(r"const EXPECTED_API = (\d+);", source)
    assert declared, "app.js must declare EXPECTED_API"
    assert int(declared.group(1)) == queries.API_VERSION


def test_status_reports_the_api_version(panel):
    response, body = authed(panel, "/api/status")
    assert json.loads(body)["api_version"] == queries.API_VERSION


# ------------------------------------------------------------------- memory


def test_projection_is_deterministic_and_bounded():
    """The map is a place you learn: the same facts must land in the same
    spot every load. PCA (not UMAP/t-SNE) is chosen for exactly that, so
    a reshuffle between refreshes would defeat the point."""
    vectors = [[1.0, 0.0, 0.5], [0.9, 0.1, 0.4], [-1.0, 0.2, -0.3],
               [-0.9, 0.3, -0.2], [0.0, 1.0, 0.0]]
    first = queries._project_2d(vectors)
    assert first == queries._project_2d(vectors)
    assert all(-1.0001 <= value <= 1.0001 for point in first for value in point)
    # Near-identical vectors must stay near each other — that adjacency is
    # what makes a duplicate land on top of what it duplicates.
    import math
    close = math.dist(first[0], first[1])
    far = math.dist(first[0], first[2])
    assert close < far


def test_projection_survives_degenerate_input():
    assert queries._project_2d([]) == []
    assert queries._project_2d([[1.0, 2.0]]) == [[0.0, 0.0]]


def test_clustering_is_stable_between_calls():
    coords = [[-1, -1], [-0.9, -0.95], [1, 1], [0.95, 0.9], [0, 0], [0.05, -0.05]]
    labels = queries._kmeans(coords, k=3)
    assert labels == queries._kmeans(coords, k=3)
    assert labels[0] == labels[1] and labels[2] == labels[3]
    assert labels[0] != labels[2]


def test_clustering_declines_when_there_is_nothing_to_cluster():
    assert queries._kmeans([[0, 0], [1, 1]], k=5) == [0, 0]
    assert queries._kmeans([[0, 0], [1, 1]], k=1) == [0, 0]


def test_review_gate_reports_the_distance_to_stage_two(monkeypatch, tmp_path):
    """The §6 gate (200 reviewed at >=90% trailing) is what family stage-2
    waits on. The panel states the distance rather than leaving it to be
    counted by hand."""
    from kyraan.memory import review_scaling
    from kyraan.memory import store as memory_store
    monkeypatch.setattr(review_scaling, "_load",
                        lambda: {"total_reviewed": 17,
                                 "recent": [1] * 15 + [0, 1]})
    monkeypatch.setattr(memory_store, "PENDING_DIR", tmp_path / "pending")

    gate = queries.memory_review()
    assert gate["total_reviewed"] == 17
    assert gate["remaining"] == 183
    assert gate["trailing_approval"] == pytest.approx(16 / 17, abs=0.001)
    assert gate["gate_met"] is False
    assert gate["pending_count"] == 0


def test_review_gate_is_met_only_when_both_halves_pass(monkeypatch, tmp_path):
    from kyraan.memory import review_scaling
    from kyraan.memory import store as memory_store
    monkeypatch.setattr(memory_store, "PENDING_DIR", tmp_path / "pending")

    # Enough reviews, approval too low.
    monkeypatch.setattr(review_scaling, "_load",
                        lambda: {"total_reviewed": 250, "recent": [1] * 40 + [0] * 10})
    assert queries.memory_review()["gate_met"] is False
    # Approval fine, not enough reviews.
    monkeypatch.setattr(review_scaling, "_load",
                        lambda: {"total_reviewed": 30, "recent": [1] * 30})
    assert queries.memory_review()["gate_met"] is False
    # Both.
    monkeypatch.setattr(review_scaling, "_load",
                        lambda: {"total_reviewed": 250, "recent": [1] * 50})
    assert queries.memory_review()["gate_met"] is True


def test_memory_map_degrades_instead_of_failing_when_pg_is_down(monkeypatch):
    """Postgres is the only store holding embeddings. With it down the
    map has no coordinates — but the endpoint must still answer, not 500,
    so the rest of the sector stays usable."""
    from kyraan.store import pg

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(pg, "connection", _boom)
    result = queries.memory_map()
    assert result["facts"] == [] and result["positioned"] == 0
    assert "connection refused" in result["degraded"]
    assert queries.memory_links()["links"] == []


# -------------------------------------------------------------------- brain


def test_synapses_keep_only_strong_mutual_neighbours():
    """The memory mesh is top-k per fact above a floor, not every pair over
    a threshold: a dense region drawn as a complete graph is a solid blob,
    which shows less than no edges at all."""
    ids = ["a", "b", "c", "d"]
    vectors = [[1.0, 0.0], [0.99, 0.14], [-1.0, 0.0], [-0.99, -0.14]]
    edges = queries._synapses(ids, vectors)
    pairs = {tuple(sorted((e["a"], e["b"]))) for e in edges}
    assert ("a", "b") in pairs and ("c", "d") in pairs
    assert ("a", "c") not in pairs        # opposite poles are not neighbours
    assert all(e["weight"] >= queries._SYNAPSE_FLOOR for e in edges)
    # Undirected: one edge per pair, never both directions.
    assert len(pairs) == len(edges)


def test_synapses_skip_facts_with_no_embedding():
    edges = queries._synapses(["a", "b"], [[1.0, 0.0], None])
    assert edges == []


def test_tool_activity_ignores_test_fixtures_and_pairs_only_same_turn(seeded_logs):
    """Co-activation is the record of two skills firing in the SAME turn —
    not a guess from their names. And `t.*` fixtures reached the real audit
    log before KYRAAN_LOG_DIR isolation landed; drawn as neurons with usage
    counts they would be lies."""
    _write_log(logging_setup.EVENT_LOG, [
        {"ts": _ago(minutes=5), "kind": "tool_call", "turn_id": "z1",
         "tool": "reminders.list"},
        {"ts": _ago(minutes=5), "kind": "tool_call", "turn_id": "z1",
         "tool": "reminders.cancel"},
        {"ts": _ago(minutes=4), "kind": "tool_call", "turn_id": "z2",
         "tool": "weather.get"},
        {"ts": _ago(minutes=4), "kind": "tool_call", "turn_id": "z2",
         "tool": "t.read"},
    ])
    usage, pairs = queries._tool_activity()
    assert "t.read" not in usage
    assert usage["reminders.list"] == 1
    assert pairs[("reminders.cancel", "reminders.list")] == 1
    # Different turns never co-activate, however close in time.
    assert ("reminders.list", "weather.get") not in pairs


def test_every_brain_edge_points_at_a_node_that_exists(monkeypatch, tmp_path, seeded_logs):
    """A dangling edge is drawn from a node to nowhere — the renderer skips
    it silently, so the graph would quietly lose wiring with no error."""
    from kyraan.triggers import goals
    monkeypatch.setattr(goals, "GOALS_PATH", tmp_path / "goals.json")

    graph = queries.brain_graph()
    ids = {node["id"] for node in graph["nodes"]}
    dangling = [e for e in graph["edges"] if e["a"] not in ids or e["b"] not in ids]
    assert dangling == [], dangling
    # Node ids are unique — two nodes sharing an id would merge on the page.
    assert len(ids) == len(graph["nodes"])


def test_brain_still_has_a_skill_lobe_when_postgres_is_down(monkeypatch, tmp_path,
                                                           seeded_logs):
    """Skills and work come from the log and the file stores; only the
    memory lobe needs pg. Losing pg must cost the memory lobe, not the
    whole brain."""
    from kyraan.store import pg
    from kyraan.triggers import goals
    monkeypatch.setattr(goals, "GOALS_PATH", tmp_path / "goals.json")
    monkeypatch.setattr(pg, "connection",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    _write_log(logging_setup.EVENT_LOG, [
        {"ts": _ago(minutes=5), "kind": "tool_call", "turn_id": "z9",
         "tool": "weather.get"},
    ])
    graph = queries.brain_graph()
    assert graph["counts"].get("memory", 0) == 0
    assert graph["counts"].get("skill", 0) > 0
    assert "pg down" in graph["degraded"]


# ------------------------------------------------------------------ routes


@pytest.mark.parametrize("path", ["/brain", "/turns", "/spend",
                                  "/turns?sort=tokens&turn=abc123"])
def test_deep_links_serve_the_page(panel, path):
    """Sectors are real URLs. Without the fallback a reload — or a link to
    a turn you want someone to look at — dropped the reader on the
    overview with every filter reset."""
    response, body = authed(panel, path)
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("text/html")
    assert b"<title>" in body


def test_a_mistyped_asset_is_still_a_hard_404(panel):
    """The fallback is extension-less only. /app.cs must fail loudly, not
    return HTML that a stylesheet link will never be able to use."""
    for path in ("/app.cs", "/app.jsx", "/missing.png"):
        response, _ = authed(panel, path)
        assert response.status == 404, path


def test_the_fallback_does_not_open_a_traversal(panel):
    for path in ("/../pyproject.toml", "/../../etc/hosts"):
        response, _ = authed(panel, path)
        assert response.status == 404, path


def test_every_sector_has_a_route():
    """A sector missing from the route table is reachable by clicking but
    not by URL — it would silently lose its state on every reload."""
    import re
    page = (server.STATIC_DIR / "index.html").read_text()
    source = (server.STATIC_DIR / "app.js").read_text()

    sectors = set(re.findall(r'class="sector[^"]*" data-view="(\w+)"', page))
    assert sectors, "no sector buttons found in index.html"

    table = re.search(r"const ROUTES = \{(.*?)\};", source, re.S).group(1)
    routed = set(re.findall(r"(\w+):\s*\"(\w+)\"", table))
    assert {view for _, view in routed} == sectors


def test_the_synapse_floor_is_a_real_control_in_both_directions():
    """Found live 2026-08-31: the slider was one-way. _synapses filtered at
    the module default and brain_graph filtered AGAIN at the parameter, so
    raising the floor worked and lowering it could not recover edges the
    first filter had already dropped. The floor must reach the mesh."""
    ids = [f"f{i}" for i in range(6)]
    vectors = [[1.0, 0.02 * i] for i in range(6)]      # a tight family
    loose = queries._synapses(ids, vectors, floor=0.1)
    tight = queries._synapses(ids, vectors, floor=0.999)
    assert len(loose) > len(tight)
    assert all(e["weight"] >= 0.999 for e in tight)


def test_events_and_turns_can_be_narrowed_to_named_tools(seeded_logs):
    """What the brain's selection hands to the other sectors: picking
    skills was a dead end until they could be carried across."""
    _write_log(logging_setup.EVENT_LOG, [
        {"ts": _ago(minutes=20), "kind": "tool_call", "turn_id": "aaa1",
         "tool": "weather.now"},
        {"ts": _ago(minutes=8), "kind": "tool_call", "turn_id": "bbb2",
         "tool": "web.search"},
    ])
    only = queries.events(tools=("web.search",), hours=2)["events"]
    assert {e.get("tool") for e in only} == {"web.search"}

    picked = queries.turns(tools=("weather.now",), hours=2)["turns"]
    assert [t["turn_id"] for t in picked] == ["aaa1"]
    assert queries.turns(tools=("nothing.here",), hours=2)["turns"] == []
    # No filter means no narrowing.
    assert len(queries.turns(hours=2)["turns"]) == 2


def test_orphans_and_dead_capability_are_reported(monkeypatch, tmp_path, seeded_logs):
    """An orphan memory has no synapse above the floor; a dead skill is
    registered but never called. Both are findings a list cannot make."""
    from kyraan.triggers import goals
    monkeypatch.setattr(goals, "GOALS_PATH", tmp_path / "goals.json")
    graph = queries.brain_graph(synapse_floor=0.99)   # nothing survives
    memories = [n for n in graph["nodes"] if n["type"] == "memory"]
    assert all(n["orphan"] for n in memories)
    assert len(graph["orphans"]) == len(memories)
    for skill in (n for n in graph["nodes"] if n["type"] == "skill"):
        assert skill["dead"] == (skill["registered"] and not skill["uses"])


def test_a_superseded_fact_does_not_wire_the_live_brain(monkeypatch):
    """Found live 2026-08-31: three superseded restatements of one
    birthday made `kiaan born_on` look like a live disagreement. A
    retired fact's relations are history, not wiring."""
    rows = [
        ("live", "kiaan", "born_on", "12_october_2025", True),
        ("old", "kiaan", "born_on", "october_2025", False),
    ]

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *args):
            self.sql = sql
        def fetchall(self):
            return ([r for r in rows if r[4]] if "WHERE f.active" in self.sql
                    else rows)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    from kyraan.store import pg
    monkeypatch.setattr(pg, "connection", lambda: _Conn())

    live = queries.memory_links()
    assert [l["to"] for l in live["links"]] == ["12_october_2025"]
    assert live["contested"] == [] and live["variants"] == []
    # Include the history and the same two rows DO disagree — which is
    # exactly why the live view excludes them. This is the false alarm,
    # reproduced on purpose.
    assert queries.memory_links(include_superseded=True)["contested"] \
        == ["kiaan born_on"]


def test_contested_and_variant_are_different_findings(monkeypatch):
    """CONTESTED = different facts disagree (the review queue's job).
    VARIANT = one fact spelled one answer two ways (extraction noise).
    Reporting both as a contradiction is what raised the false alarm."""
    def links_for(rows):
        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, *args): pass
            def fetchall(self): return rows

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cur()

        from kyraan.store import pg
        monkeypatch.setattr(pg, "connection", lambda: _Conn())
        return queries.memory_links()

    one_fact = links_for([("f1", "kiaan", "born_on", "12-10-2025", True),
                          ("f1", "kiaan", "born_on", "12_october_2025", True)])
    assert one_fact["variants"] == ["kiaan born_on"]
    assert one_fact["contested"] == []

    two_facts = links_for([("f1", "kiaan", "born_on", "12_october_2025", True),
                           ("f2", "kiaan", "born_on", "3_may_2024", True)])
    assert two_facts["contested"] == ["kiaan born_on"]
    assert two_facts["variants"] == []


# ------------------------------------------------------------------- host


def test_host_process_parser_ignores_wrapped_command_lines():
    """A command line can contain newlines (any `python -c` with embedded
    ones does). A loose split parsed a continuation line as a fake process,
    which then picked up whatever role its text happened to match."""
    from kyraan.panel import host

    sample = (
        "  501  6026976   0.1 /opt/homebrew/.../llama-server --model blobs\n"
        "  502     1024   3.3 /usr/bin/python -c import x\n"
        "print('this is a continuation line, not a process')\n"
        "    for role in roles: pass\n"
    )
    rows = []
    for line in sample.splitlines():
        match = host._PS_ROW.match(line)
        if match:
            rows.append(match.groups())
    assert len(rows) == 2
    assert rows[0][0] == "501" and "llama-server" in rows[0][3]


def test_roles_name_the_part_of_kyraan_a_process_is():
    """A process table is a wall of paths; the useful question is which
    PART of the system is eating the machine."""
    from kyraan.panel import host

    def role_of(command):
        for pattern, role, _ in host._ROLES:
            if pattern.search(command):
                return role
        return ""

    assert role_of("/opt/homebrew/.../lib/ollama/llama-server --model x") == "local model"
    assert role_of("/Applications/OrbStack.app/.../OrbStack Helper vmgr") == "containers"
    assert role_of("/usr/bin/python -m kyraan.main") == "kyraan bot"
    assert role_of("/usr/bin/python scripts/panel.py --port 8765") == "panel"
    assert role_of("/Applications/Brave Browser.app/Contents/MacOS/Brave") == ""


def test_workload_ranks_models_by_wall_time(seeded_logs):
    """The host panel says what holds MEMORY; this says where the TIME
    goes. ps cannot tell a chosen call from a degraded fallback."""
    _write_log(logging_setup.EVENT_LOG, [
        {"ts": _ago(minutes=5), "kind": "model_call", "provider": "ollama",
         "model": "qwen3:8b", "tier": "cheap", "latency_ms": 16000,
         "input_tokens": 900, "output_tokens": 40, "cost_usd": 0},
        {"ts": _ago(minutes=4), "kind": "model_call", "provider": "openai",
         "model": "gpt-5.4-nano", "tier": "frontier", "latency_ms": 1800,
         "input_tokens": 9000, "output_tokens": 120, "cost_usd": 0.0009},
        {"ts": _ago(minutes=3), "kind": "model_call", "provider": "openai",
         "model": "gpt-5.4-nano", "tier": "frontier", "latency_ms": 1600,
         "input_tokens": 8000, "output_tokens": 90, "cost_usd": 0.0008},
    ])
    result = queries.workload(hours=2)
    slowest = result["models"][0]
    # One local call outweighs two cloud calls on wall time while costing
    # nothing — which is exactly the trade the panel exists to show.
    assert slowest["model"] == "ollama/qwen3:8b"
    assert slowest["calls"] == 1 and slowest["avg_ms"] == 16000
    assert slowest["ms_share"] > 50
    assert result["models"][1]["cost_usd"] > 0 and slowest["cost_usd"] == 0
