"use strict";
/* Every node in this file is built with createElement + textContent.
   There is no innerHTML, no insertAdjacentHTML, no template literal that
   becomes markup — the rows below carry web-search snippets, MCP server
   output and mail subject lines, and this page runs next to the kernel.
   Keep it that way (docs/design/web_panel.md, rule 2). */

/* The API shape this page was written against. The server serves these
   files from disk but loads its Python once, so an edited-then-not-
   restarted panel would otherwise render new consoles against old JSON
   and simply drop whatever is missing. Must match queries.API_VERSION. */
const EXPECTED_API = 12;

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  const console_ = node.closest && node.closest(".console");
  if (console_) console_.classList.remove("is-empty");
}

function empty(node, message) {
  clear(node);
  node.appendChild(el("div", "empty", message));
  // An empty console holds its frame at full padding for a one-line
  // placeholder. Mark it so the side column can let it shrink.
  const console_ = node.closest && node.closest(".console");
  if (console_) console_.classList.add("is-empty");
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

/* Logs are written in UTC; the owner thinks in wall-clock time, and the
   header's own clock is local. Showing a row as 10:16 next to a header
   saying 15:53 makes every timestamp on the page a small puzzle, so parse
   and convert. On the machine itself the browser zone IS KYRAAN_TIMEZONE;
   from a phone in another zone the reader gets their own, which is what
   "when did this happen" means to them. */
function hhmmss(ts) {
  if (typeof ts !== "string" || ts.length < 19) return "";
  const when = new Date(ts);
  if (isNaN(when.getTime())) return ts.slice(11, 19);
  return when.toLocaleTimeString([], { hour12: false });
}

function relative(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const past = seconds < 0;
  let n = Math.abs(seconds), unit = "s";
  if (n >= 86400) { n = Math.round(n / 86400); unit = "d"; }
  else if (n >= 3600) { n = Math.round(n / 3600); unit = "h"; }
  else if (n >= 60) { n = Math.round(n / 60); unit = "m"; }
  else { n = Math.round(n); }
  return past ? `${n}${unit} ago` : `in ${n}${unit}`;
}

const tokens = (n) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0);

/* One-line summary of an event, chosen per kind so the stream reads as
   prose instead of JSON. Anything unrecognised falls back to its fields. */
function summarize(event) {
  const skip = new Set(["ts", "kind", "turn_id"]);
  switch (event.kind) {
    case "tool_call": {
      const args = Object.entries(event.args || {})
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join(" ");
      return `${event.tool || event.skill || "?"} ${args}`;
    }
    case "tool_result":
      return `${event.tool || event.skill || "?"} ${event.ok ? "ok" : "FAILED: " + (event.error || "")}` +
             (event.duration_ms != null ? ` ${event.duration_ms}ms` : "");
    case "model_call":
      return `${event.provider || "?"}/${event.model || "?"} in=${event.input_tokens || 0} ` +
             `out=${event.output_tokens || 0} cached=${event.cached_tokens || 0} ` +
             `$${(event.cost_usd || 0).toFixed(5)}`;
    case "turn_health":
      return event.anomaly_count ? `${event.anomaly_count} anomalies` : "clean";
    case "reminder_recurred":
      return "next " + shortWhen(event.next);
    case "wake_armed":
      return "armed " + shortWhen(event.at) + " for " + shortWhen(event.due);
    case "episodes_ingested":
      return `${event.episodes} episodes` +
             (event.days ? ` (${[].concat(event.days).join(", ")})` : "");
    case "history_seeded":
      return `${event.chats} chats`;
    default:
      return compact(event, skip);
  }
}

/* Braces, quotes and colons are 30% of a JSON row and none of it is
   information. A stream you read for hours should read as fields, not as
   a serialisation format. Ids are truncated — they exist to be matched,
   not read — and timestamps become clock times. */
function compact(event, skip) {
  const parts = [];
  for (const [key, value] of Object.entries(event)) {
    if (skip.has(key) || value === null || value === undefined) continue;
    let text;
    if (Array.isArray(value)) text = value.join(", ");
    else if (typeof value === "object") text = JSON.stringify(value);
    else text = String(value);
    if (text === "") continue;
    if (key.endsWith("_id")) text = text.slice(0, 8);
    else if (ISO_LIKE.test(text)) text = shortWhen(text);
    if (text.length > 58) text = text.slice(0, 58) + "…";
    parts.push(key + " " + text);
  }
  return parts.join("   ");
}

const ISO_LIKE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;

/* A time today shows as a clock; another day keeps its date. */
function shortWhen(value) {
  if (typeof value !== "string") return String(value ?? "");
  const when = new Date(value.replace(" ", "T"));
  if (isNaN(when.getTime())) return value;
  const today = new Date();
  const sameDay = when.toDateString() === today.toDateString();
  const clock = when.toLocaleTimeString([], { hour12: false,
                                              hour: "2-digit", minute: "2-digit" });
  return sameDay ? clock : `${when.toLocaleDateString()} ${clock}`;
}

function eventRow(event, anomalyKinds) {
  const bad = anomalyKinds.has(event.kind);
  const row = el("div", "row" + (bad ? " bad" : ""));
  row.appendChild(el("span", "ts", hhmmss(event.ts)));
  row.appendChild(el("span", "kind" + (bad ? " bad" : ""), event.kind || "?"));
  row.appendChild(el("span", "body", summarize(event)));
  if (event.turn_id) {
    const tag = el("span", "tag", event.turn_id.slice(0, 8));
    tag.title = "turn " + event.turn_id;
    row.appendChild(tag);
  }
  return row;
}

/* --------------------------------------------------------------- tube */

/* The phone breakpoint, shared with app.css. matchMedia is absent only
   in a test harness, where nothing is a phone. */
const PHONE = window.matchMedia ? window.matchMedia("(max-width: 720px)") : { matches: false };

const PHOSPHORS = ["amber", "green", "blue"];
const TUBE_KEY = "kyraan.phosphor";

function setPhosphor(name, remember) {
  if (!PHOSPHORS.includes(name)) name = "amber";
  document.documentElement.setAttribute("data-phosphor", name);
  for (const button of document.querySelectorAll("#phosphor button")) {
    button.classList.toggle("on", button.dataset.phosphor === name);
  }
  // Private windows and blocked site data throw on access, and a panel
  // that cannot remember a colour must still render in one.
  if (remember) { try { localStorage.setItem(TUBE_KEY, name); } catch (_) {} }
}

function initPhosphor() {
  let saved = null;
  try { saved = localStorage.getItem(TUBE_KEY); } catch (_) {}
  setPhosphor(saved || "amber", false);
  for (const button of document.querySelectorAll("#phosphor button")) {
    button.addEventListener("click", () => setPhosphor(button.dataset.phosphor, true));
  }
}

/* ---------------------------------------------------------------- header */

function readout(label, value, level) {
  const box = el("div", "readout" + (level ? " " + level : ""));
  box.appendChild(el("span", "label", label));
  box.appendChild(el("span", "value", value));
  return box;
}

/* Level from a percentage, used by both the budget readout and its
   console so the two can never disagree about what "hot" means. */
function budgetLevel(pct) {
  if (pct == null) return "";
  return pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok";
}

let lastStatus = null;

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    lastStatus = status;
    const rail = $("readouts");
    clear(rail);

    if (status.api_version !== EXPECTED_API) {
      const stale = readout("stale server",
        `api v${status.api_version || "?"} · page v${EXPECTED_API}`, "bad");
      stale.title = "The running panel predates this page. Restart "
                  + "scripts/panel.py — consoles may be missing data.";
      rail.appendChild(stale);
    }

    const kill = status.kill_switch || {};
    const killBox = readout("state", kill.engaged ? "HALTED" : "RUNNING",
                            kill.engaged ? "bad" : "ok");
    if (kill.engaged && kill.reason) killBox.title = kill.reason;
    rail.appendChild(killBox);

    const budget = status.budget || {};
    const pct = budget.used_pct;
    rail.appendChild(readout("spend / day",
      `$${(budget.spent_today_usd || 0).toFixed(4)} / $${budget.daily_budget_usd}` +
      (pct == null ? "" : `  ${pct}%`), budgetLevel(pct)));

    const day = status.last_24h || {};
    const rate = day.turns ? Math.round(day.anomalous_turns / day.turns * 100) : 0;
    rail.appendChild(readout("turns 24h", String(day.turns || 0)));
    rail.appendChild(readout("anomalous",
      `${day.anomalous_turns || 0}  ${rate}%`,
      rate >= 25 ? "bad" : rate > 0 ? "warn" : "ok"));

    // The rail carries the same two facts between sectors, because the
    // rail is what the eye rests on when the header is not.
    const railState = $("rail-state");
    if (railState) {
      railState.textContent = kill.engaged ? "HALT" : "RUN";
      railState.parentElement.className =
        "rail-stat " + (kill.engaged ? "bad" : "ok");
    }
    const railBudget = $("rail-budget");
    if (railBudget) {
      railBudget.style.width = Math.min(100, pct || 0) + "%";
      railBudget.className = budgetLevel(pct) === "ok" ? "" : budgetLevel(pct);
      railBudget.parentElement.title =
        `$${(budget.spent_today_usd || 0).toFixed(4)} of $${budget.daily_budget_usd} today`;
    }

    $("stamp").textContent = hhmmss(status.now);
    if (currentView === "overview") { renderBudgetConsole(); }
  } catch (err) {
    $("stamp").textContent = "status failed: " + err.message;
  }
}

/* ------------------------------------------------------------- overview */
/* Sector 00: every console at once. A command centre's whole premise is
   that you do not navigate to find out something is wrong. */

const deckState = { usage: null, health: null };

function verdictInto(id, text, level) {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.className = "verdict" + (level ? " " + level : "");
}

/* -- systems console -- */

function renderSystemsConsole(health) {
  const body = $("system-body");
  clear(body);
  // An empty matrix must SAY it is empty. A blank console reads as "all
  // quiet" when it actually means "this panel could not tell you".
  if (!health.components || !health.components.length) {
    empty(body, "no component data — restart scripts/panel.py to pick up "
                + "server changes");
    verdictInto("system-verdict", "no data", "bad");
    return;
  }
  const matrix = el("div", "matrix");
  for (const component of health.components || []) {
    const lamp = el("div", "lamp " + (component.ok ? "up" : "down"));
    lamp.appendChild(el("span", "bulb", component.ok ? "●" : "◉"));
    lamp.appendChild(el("span", "name", component.name));
    const detail = el("span", "detail", component.detail);
    detail.title = component.detail;
    lamp.appendChild(detail);
    matrix.appendChild(lamp);
  }
  body.appendChild(matrix);
  const down = (health.components || []).filter((c) => !c.ok).length;
  verdictInto("system-verdict",
    health.verdict + (down ? ` · ${down} down` : ""),
    health.verdict === "OK" ? "ok" : health.verdict === "WARN" ? "warn" : "bad");
}

/* -- budget console -- */

/* Sparkline built with createElementNS — the no-innerHTML rule applies to
   SVG exactly as to HTML, and an <svg> assembled from a string would be
   the easiest place to forget it. */
function sparkline(values, level) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 100 30");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "spark");
  if (values.length < 2) return svg;

  const peak = Math.max(...values, 0.000001);
  const step = 100 / (values.length - 1);
  const y = (v) => 29 - (v / peak) * 27;
  const line = values.map((v, i) => `${(i * step).toFixed(2)},${y(v).toFixed(2)}`);

  const area = document.createElementNS(NS, "polygon");
  area.setAttribute("points", `0,30 ${line.join(" ")} 100,30`);
  area.setAttribute("fill", "currentColor");
  area.setAttribute("opacity", "0.16");
  svg.appendChild(area);

  const path = document.createElementNS(NS, "polyline");
  path.setAttribute("points", line.join(" "));
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.4");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);

  const last = document.createElementNS(NS, "circle");
  last.setAttribute("cx", "100");
  last.setAttribute("cy", y(values[values.length - 1]).toFixed(2));
  last.setAttribute("r", "1.8");
  last.setAttribute("fill", "currentColor");
  svg.appendChild(last);

  svg.style.color = level === "bad" ? "var(--bad)"
                  : level === "warn" ? "var(--warn)" : "var(--accent)";
  return svg;
}

function renderBudgetConsole() {
  const body = $("budget-body");
  const status = lastStatus, usage = deckState.usage;
  if (!status) return;
  clear(body);

  const budget = status.budget || {};
  const pct = budget.used_pct;
  const level = budgetLevel(pct);

  body.appendChild(el("div", "figure " + level,
    "$" + (budget.spent_today_usd || 0).toFixed(4)));
  body.appendChild(el("div", "sub",
    `of $${budget.daily_budget_usd} cap${pct == null ? "" : ` · ${pct}%`}`));

  const bar = el("div", "bar");
  const fill = el("span", level === "ok" ? "" : level);
  fill.style.width = Math.min(100, pct || 0) + "%";
  bar.appendChild(fill);
  body.appendChild(bar);

  if (usage && usage.days && usage.days.length > 1) {
    body.appendChild(sparkline(usage.days.map((d) => d.cost_usd), level));
    body.appendChild(el("div", "sub", `${usage.days.length}-day spend`));
  }
  verdictInto("budget-verdict", pct == null ? "—" : pct + "% of cap", level);
}

/* -- routines console -- */
/* The trigger board answers "what is coming". This answers "what
   happened", which is the question after the machine sleeps through
   something — and the stores cannot answer it, because a one-shot leaves
   them the moment it fires. */

function renderRoutines(data) {
  const body = $("routines-body");
  clear(body);
  const timeline = data.timeline;
  if (!timeline.length) { empty(body, "nothing scheduled today"); return; }

  const rows = el("div", "rows");
  for (const item of timeline) {
    const row = el("div", "row" + (item.status === "next" ? " now" : ""));
    row.appendChild(el("span", "ts", hhmmss(item.at).slice(0, 5) || "--:--"));
    row.appendChild(el("span", "body", item.text));
    row.appendChild(el("span", "tag", item.type));
    row.appendChild(el("span", "routine-status " + item.status, item.status));
    rows.appendChild(row);
  }
  body.appendChild(rows);

  const counts = data.counts || {};
  const late = (counts.overdue || 0) + (counts.late || 0) + (counts.failed || 0);
  verdictInto("routines-verdict",
    `${counts.fired || 0} fired · ${(counts.queued || 0) + (counts.next || 0)} ahead`
    + (late ? ` · ${late} late` : ""),
    late ? "warn" : "ok");
}

/* -- top consumers console -- *//* -- top consumers console -- */

function renderConsumersConsole(data) {
  const body = $("consumers-body");
  clear(body);
  if (!data.turns.length) { empty(body, "no turns in the window"); return; }
  const rows = el("div", "rows");
  for (const turn of data.turns.slice(0, 6)) {
    const row = el("div", "row clickable" + (turn.anomalies.length ? " bad" : ""));
    row.appendChild(el("span", "ts", hhmmss(turn.ts)));
    row.appendChild(el("span", "body",
      turn.user_text || turn.reply || "(no chat text — scheduled or internal)"));
    row.appendChild(el("span", "num", tokens(turn.input_tokens + turn.output_tokens)));
    row.appendChild(el("span", "num", "$" + turn.cost_usd.toFixed(5)));
    // Straight from the deck into the forensics view — the point of
    // noticing an expensive turn is looking at what it did.
    row.addEventListener("click", () => { showView("turns"); showTurn(turn.turn_id); });
    rows.appendChild(row);
  }
  body.appendChild(rows);
}

/* -- anomaly census console -- */

function renderAnomalyConsole(data) {
  const body = $("anomaly-body");
  clear(body);
  const anomalies = data.kinds.filter((k) => k.anomaly);
  if (!anomalies.length) {
    empty(body, "no anomalies in 24h");
    verdictInto("anomaly-verdict", "clean", "ok");
    return;
  }
  const worst = Math.max(...anomalies.map((k) => k.count));
  const rows = el("div", "rows");
  for (const kind of anomalies.slice(0, 6)) {
    const row = el("div", "row bad");
    row.appendChild(el("span", "kind bad", kind.kind));
    // Inline magnitude bar: the shape of a census is the point, and a
    // column of bare numbers hides which one dominates.
    const gauge = el("span", "body");
    const bar = el("div", "bar");
    const fill = el("span", "bad");
    fill.style.width = Math.round(kind.count / worst * 100) + "%";
    bar.appendChild(fill);
    gauge.appendChild(bar);
    row.appendChild(gauge);
    row.appendChild(el("span", "num", "×" + kind.count));
    rows.appendChild(row);
  }
  body.appendChild(rows);
  const total = anomalies.reduce((sum, k) => sum + k.count, 0);
  verdictInto("anomaly-verdict", `${total} in 24h`, "warn");
}

/* -- the hub ---------------------------------------------------------- */
/* Memories are the core, skills orbit them. Not decoration: ring position
   is stable per tool (hashed from its name, so a tool keeps its seat),
   node size is its call count, and a firing tool lights its own seat off
   the same SSE the stream uses. The core's particle count IS the fact
   count. Click it and you are in sector 06 with the real graph. */

const hub = { view: { x: 0, y: 0, scale: 1 }, raf: null, fitted: 0 };

/* One fetch, one graph. Both the hub and sector 06 read brain.nodes, so
   whichever the reader opens first pays for it and the other is instant —
   and, more importantly, they can never disagree about what the brain
   contains. */
let graphPromise = null;

function ensureGraph(force) {
  if (graphPromise && !force) return graphPromise;
  graphPromise = (async () => {
    const graph = await api("/api/brain?floor=" + brain.floor.toFixed(2));
    brain.nodes = graph.nodes;
    brain.edges = graph.edges;
    brain.byId = new Map(graph.nodes.map((n) => [n.id, n]));
    brain.contactsTotal = graph.contacts_total || 0;
    seedPositions();
    buildPalette();
    reheat(1);
    hub.fitted = 0;                 // reframe once the layout has settled
    return graph;
  })();
  return graphPromise;
}

/* Live memory. The graph was fetched once per page load, so a fact
   promoted or an episode ingested after you opened the page did not
   exist in the brain until a reload. Now a store-changing event on the
   stream — the same stream that lights the neurons — schedules one
   refetch, and the result is MERGED: every neuron you can already see
   keeps its exact position, only the new ones are seeded, at their
   lobe's edge, lit as they arrive. A reload would have re-seeded the
   whole layout, which is the "reset" the owner just asked to be rid of. */
const STORE_CHANGE_KINDS = new Set([
  "memory_promoted_via_chat", "memory_auto_approved", "memory_forgotten",
  "memory_unforgotten", "memory_superseded", "memory_consolidated",
  "memory_short_term_expired", "episodes_ingested", "episodes_suppressed",
  "triples_extracted", "document_ingested", "document_renamed",
  "face_enrolled", "person_enrolled", "person_episodes_deleted",
  "contacts_synced", "note_indexed", "vault_synced", "person_registered_from_note",
]);
const REFRESH_SETTLE_MS = 2500;      // let the write land; coalesce a burst
let refreshTimer = null;
// (brain.refreshes lives in the brain literal. A top-level assignment here
// ran before `const brain` was declared, threw in the temporal dead zone,
// and took the entire script down with it — "brain is not defined" on a
// page where nothing worked. node --check cannot see that; a loaded page
// can.)

function scheduleGraphRefresh(reason) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshGraph(reason), REFRESH_SETTLE_MS);
}

const POSITION_FIELDS = ["x", "y", "z", "vx", "vy", "vz", "pinned"];

function mergeGraph(graph) {
  const seen = new Set();
  const added = [];
  const merged = graph.nodes.map((incoming) => {
    seen.add(incoming.id);
    const existing = brain.byId.get(incoming.id);
    if (existing) {
      // Fresh metadata, same place: the label, counts and flags may have
      // changed; where it sits is the reader's, not the server's.
      for (const key of Object.keys(incoming)) {
        if (!POSITION_FIELDS.includes(key)) existing[key] = incoming[key];
      }
      return existing;
    }
    added.push(incoming);
    return incoming;
  });
  // Seed only the newcomers, at their lobe's edge with a deterministic
  // jitter, so they visibly drift in rather than appear mid-cluster.
  added.forEach((node, i) => {
    const anchor = anchorFor(node);
    const angle = (i * 137.508) * Math.PI / 180;
    node.x = anchor[0] + Math.cos(angle) * 0.42;
    node.y = anchor[1] + Math.sin(angle) * 0.42;
    node.z = (anchor[2] || 0) + Math.sin(i * 0.7) * 0.2;
    node.vx = 0; node.vy = 0; node.vz = 0; node.pinned = false;
  });
  const removed = brain.nodes.filter((n) => !seen.has(n.id)).map((n) => n.id);
  for (const id of removed) brain.selection.delete(id);

  brain.nodes = merged;
  brain.edges = graph.edges;
  brain.byId = new Map(merged.map((n) => [n.id, n]));
  brain.signals = brain.signals.filter((sg) => brain.byId.has(sg.from) && brain.byId.has(sg.to));
  brain.callWires = brain.callWires.filter((w) => brain.byId.has(w.from) && brain.byId.has(w.to));
  return { added, removed };
}

async function refreshGraph(reason) {
  if (!brain.nodes.length) return;            // nothing loaded yet; the load will be fresh
  let graph;
  try {
    graph = await api("/api/brain?fresh=1&floor=" + brain.floor.toFixed(2));
  } catch (_) {
    return;                                   // the next event will try again
  }
  graphPromise = Promise.resolve(graph);      // later ensureGraph() callers see this
  const { added, removed } = mergeGraph(graph);
  brain.contactsTotal = graph.contacts_total || 0;
  brain.refreshes++;
  buildPalette();
  if (brain.query) runSearch(brain.query);
  renderLegend();
  renderFindings(graph);
  renderCensus(graph);
  renderMemories();
  renderSelection();
  refreshPickSummaries();
  try {
    renderGate(await api("/api/memory/review"));
  } catch (_) { /* the gate keeps its last reading */ }
  const note = $("mem-note");
  if (note) {
    note.textContent = (graph.demo ? "DEMO DATA · " : "")
      + `${graph.nodes.length} neurons · ${graph.edges.length} connections`
      + ` · mesh ${brain.floor.toFixed(2)}`
      + (graph.contested.length ? ` · ${graph.contested.length} contested` : "");
  }
  const sub = $("hub-sub");
  if (sub) {
    const c = graph.counts || {};
    sub.textContent = `${c.memory || 0} memories · ${c.skill || 0} skills · `
      + `${c.task || 0} queued · ${graph.demo ? "DEMO DATA" : "live"}`;
  }
  // The newcomers light up: they are evidence of exactly the event that
  // brought them, and the reader should be able to find them.
  for (const node of added) brain.fired.set(node.id, performance.now());
  if (added.length || removed.length) {
    logLive("got", `memory changed · ${reason}`,
      `${added.length} new · ${removed.length} gone`);
    reheat(0.35);                             // newcomers settle; the rest barely move
  }
}

/* The turn card. Every stream event is stamped with a turn_id, so a
   whole turn can be assembled live into one card on the canvas: what was
   asked (the user's words, from the trace via /api/turn — one fetch per
   turn), each step as it happens, what came back, and the effort — tokens,
   cost, wall time, and any corrections the rails had to make. Nothing is
   inferred: a step is an event, an effort figure is a sum of events. */
const CARD_LINGER_MS = 12000;
const CORRECTION_KINDS = new Set([
  "agent_contract_corrected", "agent_tier_fallback", "agent_false_success_corrected",
  "agent_deflection_corrected", "agent_referent_corrected", "tool_loop_detected",
  "web_search_budget", "model_call_error", "agent_all_tiers_failed",
]);
const turnCard = { turnId: null, steps: [], totals: null, asked: "", replied: "",
                   startedAt: 0, endedAt: 0, pinned: false, timer: null, host: null,
                   // Folded to one line on a phone until opened (owner,
                   // 2026-09-03); the choice sticks for the session.
                   folded: PHONE.matches };

function cardHost() {
  return currentView === "memory" ? document.querySelector("#view-memory .starfield")
       : currentView === "overview" ? document.querySelector(".hub") : null;
}

function newTurn(turnId) {
  turnCard.turnId = turnId;
  turnCard.steps = [];
  turnCard.totals = { model_calls: 0, tools: 0, input_tokens: 0, output_tokens: 0,
                      cost_usd: 0, model_ms: 0, corrections: 0 };
  turnCard.asked = ""; turnCard.replied = "";
  turnCard.startedAt = performance.now(); turnCard.endedAt = 0;
  turnCard.pinned = false;
  clearTimeout(turnCard.timer);
  // The user's words are in the trace, not the events: fetch them once.
  api("/api/turn?id=" + encodeURIComponent(turnId)).then((detail) => {
    if (turnCard.turnId !== turnId) return;
    const start = (detail.records || []).find((r) => r.kind === "turn_start");
    if (start && start.user_text) { turnCard.asked = start.user_text; renderTurnCard(); }
  }).catch(() => {});
}

function trackTurn(event) {
  const turnId = event.turn_id;
  if (!turnId) return;
  if (turnId !== turnCard.turnId) newTurn(turnId);
  const t = turnCard.totals;
  const kind = event.kind || "";
  let step = null;

  if (kind === "model_call") {
    t.model_calls++;
    t.input_tokens += event.input_tokens || 0;
    t.output_tokens += event.output_tokens || 0;
    t.cost_usd += event.cost_usd || 0;
    t.model_ms += event.latency_ms || 0;
    step = { phase: "think", text: `${event.model || event.provider || "model"} · ${event.tier || ""}`.trim(),
             detail: `${event.input_tokens ?? "?"} in · ${event.output_tokens ?? "?"} out · ${event.latency_ms ?? "?"}ms` };
  } else if (kind === "episode_rag") {
    step = { phase: "got", text: `recall → ${event.injected ?? 0} episodes`,
             detail: event.best_sim != null ? `best match ${Number(event.best_sim).toFixed(2)}` : "" };
  } else if (kind === "agent_tool_call" && event.tool) {
    t.tools++;
    step = { phase: "try", text: event.tool, detail: String(event.consider || "").slice(0, 160) };
  } else if (kind === "tool_call" && event.tool) {
    step = { phase: "try", text: `${event.tool} ${compactArgs(event.args)}`.trim(), detail: "" };
  } else if (kind === "tool_result" && event.tool) {
    step = { phase: "got", text: event.ok ? `${event.tool} → ok · ${event.duration_ms ?? "?"}ms`
                                          : `${event.tool} → failed`,
             detail: event.ok ? "" : String(event.error || "").slice(0, 160) };
  } else if (CORRECTION_KINDS.has(kind)) {
    t.corrections++;
    step = { phase: "fix", text: kind.replace(/_/g, " "),
             detail: String(event.reason || event.error || event.draft || "").slice(0, 160) };
  } else if (kind === "agent_reply") {
    step = { phase: "reply", text: `replied · ${event.steps ?? "?"} steps · ${event.tier || ""}`.trim(), detail: "" };
    turnCard.endedAt = performance.now();
    api("/api/turn?id=" + encodeURIComponent(turnId)).then((detail) => {
      if (turnCard.turnId !== turnId) return;
      const end = (detail.records || []).find((r) => r.kind === "turn_end");
      if (end && end.reply) { turnCard.replied = end.reply; renderTurnCard(); }
    }).catch(() => {});
    if (!turnCard.pinned) {
      clearTimeout(turnCard.timer);
      turnCard.timer = setTimeout(() => { if (!turnCard.pinned) hideTurnCard(); }, CARD_LINGER_MS);
    }
  }
  if (step) { step.at = new Date(); turnCard.steps.push(step); }
  renderTurnCard();
}

function hideTurnCard() {
  const card = $("turn-card");
  if (card) card.remove();
}

function renderTurnCard() {
  const host = cardHost();
  if (!host || !turnCard.turnId) return;
  let card = $("turn-card");
  if (!card) {
    card = el("div", "turn-card");
    card.id = "turn-card";
    card.addEventListener("mouseenter", () => clearTimeout(turnCard.timer));
    card.addEventListener("mouseleave", () => {
      if (turnCard.endedAt && !turnCard.pinned) {
        turnCard.timer = setTimeout(hideTurnCard, CARD_LINGER_MS / 2);
      }
    });
  }
  if (card.parentElement !== host) host.appendChild(card);
  clear(card);
  card.classList.toggle("folded", turnCard.folded);

  const head = el("div", "tc-head");
  // The head is the fold's handle: a tap anywhere on it that is not a
  // button or the id opens or closes the card.
  head.addEventListener("click", (event) => {
    if (event.target.closest("button, .tag")) return;
    turnCard.folded = !turnCard.folded;
    renderTurnCard();
  });
  head.appendChild(el("span", "tc-title", turnCard.endedAt ? "turn" : "turn · live"));
  const idTag = el("span", "tag literal", turnCard.turnId.slice(0, 8));
  idTag.title = "open in forensics";
  idTag.style.cursor = "pointer";
  idTag.addEventListener("click", () => { showView("turns"); showTurn(turnCard.turnId); });
  head.appendChild(idTag);
  // One line when folded: what was asked, then the latest step.
  const last = turnCard.steps[turnCard.steps.length - 1];
  const summary = el("span", "tc-summary",
    [turnCard.asked ? "“" + turnCard.asked.slice(0, 40) + "”" : "",
     last ? last.phase.toUpperCase() + " " + last.text : ""].filter(Boolean).join(" · "));
  head.appendChild(summary);
  const fold = el("button", "tc-btn tc-fold", turnCard.folded ? "▾" : "▴");
  fold.title = turnCard.folded ? "open" : "fold to one line";
  fold.addEventListener("click", () => { turnCard.folded = !turnCard.folded; renderTurnCard(); });
  head.appendChild(fold);
  const pin = el("button", "tc-btn", turnCard.pinned ? "unpin" : "pin");
  pin.addEventListener("click", () => {
    turnCard.pinned = !turnCard.pinned; clearTimeout(turnCard.timer); renderTurnCard();
  });
  head.appendChild(pin);
  const close = el("button", "tc-btn", "\u00d7");
  close.addEventListener("click", hideTurnCard);
  head.appendChild(close);
  card.appendChild(head);

  if (turnCard.asked) {
    const asked = el("div", "tc-asked", turnCard.asked.slice(0, 200));
    asked.title = turnCard.asked;
    card.appendChild(asked);
  }

  const steps = el("ol", "tc-steps");
  for (const step of turnCard.steps) {
    const li = el("li", "tc-step " + step.phase);
    li.appendChild(el("span", "tc-phase", step.phase));
    const body = el("span", "tc-text", step.text);
    if (step.detail) body.title = step.detail;
    li.appendChild(body);
    if (step.detail) li.appendChild(el("div", "tc-detail", step.detail));
    steps.appendChild(li);
  }
  card.appendChild(steps);

  if (turnCard.replied) {
    const replied = el("div", "tc-replied", turnCard.replied.slice(0, 220));
    replied.title = turnCard.replied;
    card.appendChild(replied);
  }

  // Effort: sums of the turn's own events, nothing estimated.
  const t = turnCard.totals;
  const wall = ((turnCard.endedAt || performance.now()) - turnCard.startedAt) / 1000;
  const effort = el("div", "tc-effort");
  const cells = [
    [`${t.model_calls}`, "model calls"], [`${t.tools}`, "tools"],
    [`${(t.input_tokens + t.output_tokens) >= 1000 ? ((t.input_tokens + t.output_tokens) / 1000).toFixed(1) + "k" : t.input_tokens + t.output_tokens}`, "tokens"],
    [`$${t.cost_usd.toFixed(4)}`, "cost"],
    [`${(t.model_ms / 1000).toFixed(1)}s`, "model time"],
    [`${wall.toFixed(1)}s`, "wall"],
  ];
  if (t.corrections) cells.push([`${t.corrections}`, "corrections"]);
  for (const [value, label] of cells) {
    const cell = el("div", "tc-cell" + (label === "corrections" ? " warn" : ""));
    cell.appendChild(el("b", null, value));
    cell.appendChild(el("span", null, label));
    effort.appendChild(cell);
  }
  card.appendChild(effort);
}

async function loadHub() {
  try {
    const graph = await ensureGraph();
    const counts = graph.counts || {};
    $("hub-sub").textContent =
      `${counts.memory || 0} memories · ${counts.skill || 0} skills · `
      + `${counts.task || 0} queued · ${graph.demo ? "DEMO DATA" : "live"}`;
  } catch (err) {
    $("hub-sub").textContent = "brain unavailable";
  }
  // The force layout needs a moment before the framing is worth keeping.
  setTimeout(() => { hub.fitted = 0; }, 1600);
  if (hub.raf) cancelAnimationFrame(hub.raf);
  drawHub();
}

function wireHub() {
  const canvas = $("hub-canvas");
  if (!canvas) return;
  // Hover names a neuron; clicking one opens sector 06 with it selected,
  // so the hub is a way IN rather than a picture to admire.
  canvas.addEventListener("mousemove", (event) => {
    brain.hover = nodeAt(canvas, event, hub.view);
    canvas.style.cursor = brain.hover ? "pointer" : "default";
  });
  canvas.addEventListener("mouseleave", () => { brain.hover = null; });
  canvas.addEventListener("click", (event) => {
    const hit = nodeAt(canvas, event, hub.view);
    if (hit) {
      brain.selection = new Set([hit.id]);
      showView("memory");
      renderSelection();
    } else {
      showView("memory");
    }
  });
  const open = $("hub-open");
  if (open) open.addEventListener("click", () => showView("memory"));
}

/* -- the ticker shares the stream's buffer, so one SSE feeds both -- */

function renderTicker() {
  const body = $("ticker-body");
  if (!body || currentView !== "overview") return;
  clear(body);
  if (!stream.rows.length) { empty(body, "waiting for events"); return; }
  const rows = el("div", "rows");
  for (const event of stream.rows.slice(0, 12)) {
    rows.appendChild(eventRow(event, stream.anomalyKinds));
  }
  body.appendChild(rows);
  verdictInto("ticker-verdict",
    stream.source && stream.source.readyState === 1 ? "live" : "not live",
    stream.source && stream.source.readyState === 1 ? "ok" : "warn");
}

/* -- deck assembly -- */

async function refreshDeck() {
  // Each console fails alone: a dead searxng probe must not blank the
  // budget figure next to it.
  const jobs = [
    ["/api/health", (d) => { deckState.health = d; renderSystemsConsole(d); },
     "system-body", "system-verdict"],
    ["/api/usage?days=7", (d) => { deckState.usage = d; renderBudgetConsole(); },
     "budget-body", "budget-verdict"],
    ["/api/routines", renderRoutines, "routines-body", "routines-verdict"],
    ["/api/turns?limit=6&sort=tokens&hours=24", renderConsumersConsole,
     "consumers-body", null],
    ["/api/event_kinds?hours=24", renderAnomalyConsole, "anomaly-body",
     "anomaly-verdict"],
  ];
  await Promise.all(jobs.map(async ([path, render, bodyId, verdictId]) => {
    try {
      render(await api(path));
    } catch (err) {
      empty($(bodyId), "unavailable: " + err.message);
      if (verdictId) verdictInto(verdictId, "error", "bad");
    }
  }));
}

async function loadOverview() {
  renderBudgetConsole();
  await Promise.all([refreshDeck(), loadStream(), loadHub()]);
  renderTicker();
}

/* ---------------------------------------------------------------- stream */

const stream = {
  rows: [],           // newest first
  anomalyKinds: new Set(),
  source: null,
  MAX: 500,
};

function streamMatches(event) {
  if ($("stream-anomalies").checked && !stream.anomalyKinds.has(event.kind)) return false;
  if (toolFilter.stream.length
      && !toolFilter.stream.includes(event.tool || event.skill)) return false;
  const q = $("stream-q").value.trim().toLowerCase();
  if (!q) return true;
  return JSON.stringify(event).toLowerCase().includes(q);
}

function renderStream() {
  const container = $("stream-rows");
  const visible = stream.rows.filter(streamMatches);
  if (!visible.length) {
    empty(container, stream.rows.length ? "nothing matches that filter" : "no events yet");
  } else {
    clear(container);
    for (const event of visible.slice(0, 300)) {
      container.appendChild(eventRow(event, stream.anomalyKinds));
    }
  }
  $("stream-note").textContent =
    `${visible.length} shown of ${stream.rows.length} held` +
    (stream.source && stream.source.readyState === 1 ? " · live" : " · not live");
  renderTicker();   // one SSE connection feeds both the sector and the deck
}

function pushEvent(event) {
  stream.rows.unshift(event);
  if (stream.rows.length > stream.MAX) stream.rows.length = stream.MAX;
}

function connectStream() {
  if (stream.source) { stream.source.close(); stream.source = null; }
  if (!$("stream-live").checked) { renderStream(); return; }
  const source = new EventSource("/api/stream");
  stream.source = source;
  source.addEventListener("log", (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch (_) { return; }
    pushEvent(event);
    brainActivate(event);
    renderStream();
  });
  source.onopen = renderStream;
  source.onerror = () => {
    // EventSource reconnects on its own; just tell the truth meanwhile.
    $("stream-note").textContent = "stream dropped — retrying";
  };
}

async function loadStream() {
  renderToolChips("stream");
  if (stream.source) { renderStream(); return; }   // already tailing
  try {
    const kinds = await api("/api/event_kinds?hours=24");
    stream.anomalyKinds = new Set(kinds.kinds.filter((k) => k.anomaly).map((k) => k.kind));
    const tools = toolFilter.stream.length
      ? "&tools=" + encodeURIComponent(toolFilter.stream.join(",")) : "";
    const recent = await api("/api/events?limit=200&hours=24" + tools);
    stream.rows = recent.events;
    renderStream();
    connectStream();
  } catch (err) {
    empty($("stream-rows"), "could not load events: " + err.message);
  }
}

/* ----------------------------------------------------------------- turns */

function turnRow(turn) {
  const row = el("div", "row clickable" + (turn.anomalies.length ? " bad" : " ok"));
  row.appendChild(el("span", "ts", hhmmss(turn.ts)));
  row.appendChild(el("span", "tag", turn.turn_id.slice(0, 8)));
  row.appendChild(el("span", "body", turn.user_text || turn.reply || "(no chat text — scheduled or internal)"));
  row.appendChild(el("span", "num", tokens(turn.input_tokens + turn.output_tokens)));
  row.appendChild(el("span", "num", "$" + turn.cost_usd.toFixed(5)));
  row.appendChild(el("span", "num", turn.total_ms != null ? turn.total_ms + "ms" : "—"));
  const tools = el("span", "tag", turn.tools.length ? turn.tools.join(", ") : "no tools");
  tools.title = turn.models.join(", ");
  row.appendChild(tools);
  if (turn.anomalies.length) {
    const flag = el("span", "tag", turn.anomalies.length + " ⚠");
    flag.title = turn.anomalies.join("\n");
    row.appendChild(flag);
  }
  row.addEventListener("click", () => showTurn(turn.turn_id));
  return row;
}

async function loadTurns() {
  const container = $("turns-rows");
  renderToolChips("turns");
  $("turns-note").textContent = "loading…";
  try {
    const tools = toolFilter.turns.length
      ? "&tools=" + encodeURIComponent(toolFilter.turns.join(",")) : "";
    const data = await api(`/api/turns?limit=100&sort=${encodeURIComponent($("turns-sort").value)}` +
                           `&hours=${encodeURIComponent($("turns-hours").value)}` + tools);
    if (!data.turns.length) { empty(container, "no turns in this window"); }
    else {
      clear(container);
      for (const turn of data.turns) container.appendChild(turnRow(turn));
    }
    $("turns-note").textContent = `${data.turns.length} of ${data.total_turns} turns`;
  } catch (err) {
    empty(container, "could not load turns: " + err.message);
    $("turns-note").textContent = "";
  }
}

function detailLine(record) {
  const parts = [hhmmss(record.ts), record.kind || "?"];
  switch (record.kind) {
    case "turn_start": parts.push("USER: " + (record.user_text || "")); break;
    case "turn_end": parts.push(`REPLY (${record.total_ms}ms): ` + (record.reply || "")); break;
    case "model_io":
      parts.push(`${record.provider}/${record.model} ${record.latency_ms}ms ` +
                 `in=${record.input_tokens} cached=${record.cached_tokens}`);
      parts.push("→ " + (record.response || ""));
      break;
    case "stage": parts.push(`${record.stage} ${record.ms}ms`); break;
    default: {
      const rest = {};
      for (const [k, v] of Object.entries(record)) {
        if (!["ts", "kind", "turn_id", "_source"].includes(k)) rest[k] = v;
      }
      parts.push(JSON.stringify(rest));
    }
  }
  return parts.join("  ");
}

async function showTurn(turnId) {
  const panel = $("turn-detail");
  openTurnId = turnId;
  syncUrl(false);
  clear(panel);
  const heading = el("h2", null, "turn ");
  // The id is DATA — it goes into a grep — so it opts out of the header's
  // uppercase chrome (same rule as .tag).
  heading.appendChild(el("span", "literal", turnId));
  const close = el("button", null, "close");
  close.addEventListener("click", () => {
    clear(panel);
    openTurnId = null;
    syncUrl(false);
  });
  heading.appendChild(close);
  panel.appendChild(heading);
  const body = el("pre", null, "loading…");
  panel.appendChild(body);
  try {
    const data = await api("/api/turn?id=" + encodeURIComponent(turnId));
    if (!data.found) { body.textContent = "no records (older than the window, or archived)"; return; }
    const lines = data.records.map(detailLine);
    if (data.stages && data.stages.length) {
      lines.push("");
      lines.push("—— timing ——");
      // Only top-level stages sum to the turn: a nested stage's ms is
      // already inside its parent (same correction as scripts/trace.py).
      for (const stage of data.stages.filter((s) => !s.depth).sort((a, b) => b.ms - a.ms)) {
        lines.push(`${String(stage.ms).padStart(7)}ms  ${stage.stage} ${stage.provider || ""}`);
      }
    }
    body.textContent = lines.join("\n");
  } catch (err) {
    body.textContent = "could not load turn: " + err.message;
  }
}

/* -------------------------------------------------------------- triggers */

function triggerRow(item) {
  const fire = item.fire || {};
  const row = el("div", "row" + (fire.overdue ? " bad" : item.undelivered ? " warn" : ""));
  row.appendChild(el("span", "kind", item.type));
  row.appendChild(el("span", "body", item.text));
  if (item.steps_total) {
    row.appendChild(el("span", "tag", `${item.steps_done}/${item.steps_total} steps`));
  }
  if (item.repeat) row.appendChild(el("span", "tag", item.repeat));
  if (item.undelivered) row.appendChild(el("span", "tag", "undelivered result"));
  if (fire.overdue) row.appendChild(el("span", "tag", "OVERDUE"));
  const when = el("span", "num", relative(fire.in_seconds));
  when.title = fire.iso || "";
  row.appendChild(when);
  return row;
}

async function loadTriggers() {
  const container = $("triggers-rows");
  try {
    const data = await api("/api/triggers");
    if (!data.triggers.length) { empty(container, "nothing scheduled"); }
    else {
      clear(container);
      for (const item of data.triggers) container.appendChild(triggerRow(item));
    }
    $("triggers-note").textContent = data.overdue
      ? `${data.overdue} overdue — the machine was asleep or the job failed`
      : "all on schedule";
  } catch (err) {
    empty(container, "could not load triggers: " + err.message);
  }
}

/* ------------------------------------------------------------------ cost */

async function loadCost() {
  const container = $("cost-body");
  clear(container);
  try {
    const data = await api("/api/usage?days=" + encodeURIComponent($("cost-days").value));
    const budget = data.budget || {};
    const pct = budget.budget_used_pct;

    const head = el("div");
    head.appendChild(el("div", null,
      `today $${budget.spent_today_usd} of $${budget.daily_budget_usd}` +
      (pct == null ? "" : ` — ${pct}%`)));
    const bar = el("div", "bar");
    const fill = el("span", pct >= 90 ? "bad" : pct >= 70 ? "warn" : "");
    fill.style.width = Math.min(100, pct || 0) + "%";
    bar.appendChild(fill);
    head.appendChild(bar);
    container.appendChild(head);

    const table = el("table");
    const header = el("tr");
    for (const label of ["day", "calls", "in", "out", "cached", "cost", "models"]) {
      header.appendChild(el("th", null, label));
    }
    table.appendChild(header);
    for (const day of [...data.days].reverse()) {
      const tr = el("tr");
      tr.appendChild(el("td", null, day.date));
      tr.appendChild(el("td", null, day.calls));
      tr.appendChild(el("td", null, tokens(day.input_tokens)));
      tr.appendChild(el("td", null, tokens(day.output_tokens)));
      tr.appendChild(el("td", null, tokens(day.cached_tokens)));
      tr.appendChild(el("td", null, "$" + day.cost_usd.toFixed(4)));
      tr.appendChild(el("td", null, Object.entries(day.by_model)
        .map(([model, n]) => `${model}×${n}`).join(", ")));
      table.appendChild(tr);
    }
    container.appendChild(table);
    $("cost-note").textContent = "spend is the ledger's; per-call detail is the event log's";
  } catch (err) {
    empty(container, "could not load usage: " + err.message);
  }
}

/* ----------------------------------------------------------------- brain */
/* Sector 06 — the second brain as one graph: what it REMEMBERS, who those
   memories are about, what WORK is queued, and what it can DO, wired by
   evidence rather than decoration:

     synapse      two facts whose stored embeddings are close
     subject      this fact is about this person
     relation     a stored triple (head -> tail)
     owns         this person's scheduled work
     managed_by   the tool family that operates on this kind of task
     coactivation these two tools fired in the SAME TURN, N times

   Three layouts over the same nodes. BRAIN is force-directed with the
   three lobes anchored apart — structure emerges from the wiring.
   COSMOS drops the memory lobe onto its PCA projection, where distance
   is meaning. SPIRAL orders memories by age, oldest at the centre.

   Canvas, not SVG: ~100 nodes simulated per frame, and no DOM built from
   fact text (rule 2 comes free). */

/* Lobe anchors. People sit near the CENTRE on purpose: memories are
   about them and work belongs to them, so they are the hub the other
   lobes hang off, not a fourth island. */
/* Seven lobes. People sit near the CENTRE on purpose: memories are about
   them, conversations happen with them, documents concern them and faces
   recognise them — they are the hub the rest hangs off. */
/* Anchors are [x, y, z]. Depth is spread deliberately so the lobes form
   a VOLUME: with every anchor on one plane the orbit only ever showed a
   sheet turning edge-on, which is a flat brain with extra steps. */
/* Where each lobe lives. The core is at the origin; PEOPLE are a ring
   around it (anchorFor — each person has its own point on the ring, so
   nine people are nine neurons rather than one knot); every other lobe
   is ~1.15 out on the sphere, spread so no two neighbours share a side
   (owner, 2026-09-03: "the centre is cluttered, use the right spaces
   and positions for each group"). Before this five lobes anchored
   within 0.6 of the core and their captions overprinted each other. */
/* Laid out like a brain seen from the side (owner, 2026-09-04: "make it
   more like a human brain, and no dense single point"): x runs front(+)
   to back(−), y up, z out to the sides. Skills and work sit forward
   (frontal: what it can do and will do); facts on top (parietal: what it
   knows); recall out to the side (temporal: what it has lived); documents
   and faces at the back (occipital: what it has seen); notes and tags
   below and behind (the cerebellum: what the owner wrote); contacts at
   the stem. The core is the thalamus, and the people ring around it at
   0.55 so nothing else sits on it. */
/* The cortex. Every neuron but the core and the people ring lives on an
   ellipsoid SHELL (CORTEX below): a brain is one solid oval, not islands
   in space. Lobes are regions of that surface, anchored where a brain
   keeps them, and a groove down the midline (|z| pushed out) splits it
   into two hemispheres — a lobe straddles the fissure like a real one.
   Anchors sit on the shell; canvas y grows DOWNWARD, so "up" is negative. */
const CORTEX = { a: 1.05, b: 0.80, c: 0.78, inner: 0.80, groove: 0 };
const LOBES = {
  // Ten regions on the vertices of an icosahedron (evenly spaced in 3D),
  // scaled onto the shell — a globe of regions, readable from any angle
  // (owner, 2026-09-04: "only two sides are used; use 360°"). The two
  // flat sheets before came from anchoring every lobe on the midline.
  core:     { anchor: [0.0, 0.0, 0.0], label: "core" },
  person:   { anchor: [0.00, 0.04, 0.00], ring: 0.36, label: "people" },
  memory:   { anchor: [-0.56, -0.68, 0.00], label: "facts" },      // top, back        (52)
  task:     { anchor: [0.56, -0.68, 0.00], label: "work" },        // top, front        (4)
  skill:    { anchor: [0.89, 0.00, 0.41], label: "skills" },       // front, one side  (70)
  document: { anchor: [-0.89, 0.00, 0.41], label: "documents" },   // back, one side   (25)
  face:     { anchor: [-0.89, 0.00, -0.41], label: "faces" },      // back, other side  (5)
  episode:  { anchor: [0.00, 0.42, 0.66], label: "recall" },       // low, one flank  (215)
  note:     { anchor: [0.00, 0.42, -0.66], label: "notes" },       // low, other flank  (8)
  contact:  { anchor: [0.56, 0.68, 0.00], label: "contacts" },     // bottom, front    (11)
  tag:      { anchor: [-0.56, 0.68, 0.00], label: "tags" },        // bottom, back      (6)
  // The two spare vertices, for the lobes that fill as the duties run.
  place:    { anchor: [0.00, -0.42, 0.66], label: "places" },      // upper, one flank
  care:     { anchor: [0.00, -0.42, -0.66], label: "care" },       // upper, other flank
};

/* A node's own anchor: the lobe's point, or its own place on the lobe's
   ring, evenly spaced by position among its kind. */
function anchorFor(node) {
  const lobe = LOBES[node.type] || LOBES.memory;
  // A phone canvas is taller than wide and the layout is wider than
  // tall; the fit squeezed the whole brain into the middle third. On a
  // portrait canvas the layout turns 90° and stands up instead.
  const a = brain.portrait ? [lobe.anchor[1], -lobe.anchor[0], lobe.anchor[2]] : lobe.anchor;
  if (!lobe.ring) return a;
  if (!brain.ringIndex || brain.ringIndex.n !== brain.nodes.length) {
    const kin = brain.nodes.filter((n) => (LOBES[n.type] || {}).ring);
    brain.ringIndex = { n: brain.nodes.length, of: new Map(kin.map((n, i) => [n.id, i])),
                        count: Object.fromEntries(Object.keys(LOBES).filter((t) => LOBES[t].ring)
                          .map((t) => [t, kin.filter((n) => n.type === t).length])) };
  }
  const i = brain.ringIndex.of.get(node.id) || 0;
  const count = Math.max(1, brain.ringIndex.count[node.type] || 1);
  const angle = (i / count) * Math.PI * 2 + 0.4;
  return brain.portrait
    ? [a[0], a[1] + Math.cos(angle) * lobe.ring, a[2] + Math.sin(angle) * lobe.ring]
    : [a[0] + Math.cos(angle) * lobe.ring, a[1], a[2] + Math.sin(angle) * lobe.ring];
}

const EDGE_STYLE = {
  synapse:     { alpha: 0.34, width: 1.0, rest: 90,  key: "--accent" },
  subject:     { alpha: 0.16, width: 0.8, rest: 120, key: "--dim" },
  relation:    { alpha: 0.7,  width: 1.6, rest: 100, key: "--ok" },
  owns:        { alpha: 0.4,  width: 1.1, rest: 150, key: "--dim" },
  managed_by:  { alpha: 0.22, width: 0.9, rest: 210, key: "--dim" },
  coactivation:{ alpha: 0.4,  width: 1.2, rest: 80,  key: "--accent" },
  // A conversation that cites a fact is the strongest link in the store:
  // it says this exchange is WHY that fact is known.
  recalls:     { alpha: 0.8,  width: 1.7, rest: 70,  key: "--ok" },
  spoke:       { alpha: 0.10, width: 0.7, rest: 260, key: "--dim" },
  about:       { alpha: 0.45, width: 1.1, rest: 140, key: "--dim" },
  recognises:  { alpha: 0.6,  width: 1.3, rest: 90,  key: "--warn" },
  // A contact IS a person (exact name) or MAYBE is (one alias token):
  // the second is drawn dashed, because it is a candidate, not a claim.
  is:          { alpha: 0.65, width: 1.3, rest: 80,  key: "--ok" },
  tagged:      { alpha: 0.35, width: 0.9, rest: 90,  key: "--dim" },
  maybe:       { alpha: 0.5,  width: 1.1, rest: 110, key: "--warn", dash: true },
  // The core's wiring (server: k:kyraan). ACTS through a skill, FIRES a
  // scheduled thing, RECEIVED a file or note, TALKS with the owner.
  acts:        { alpha: 0.14, width: 0.8, rest: 150, key: "--accent" },
  fires:       { alpha: 0.3,  width: 1.0, rest: 170, key: "--warn" },
  received:    { alpha: 0.07, width: 0.7, rest: 230, key: "--dim" },
  talks:       { alpha: 0.85, width: 2.0, rest: 110, key: "--ok" },
  at:          { alpha: 0.8,  width: 1.6, rest: 120, key: "--ok" },      // the owner is inside this place now
  mentions:    { alpha: 0.3,  width: 0.9, rest: 140, key: "--dim" },     // a document names the place
  // A capture and the note it illustrates (server: document.related).
  illustrates: { alpha: 0.6,  width: 1.3, rest: 90,  key: "--ok" },
  // Two notes joined by an Obsidian [[wikilink]] — the owner's own edge.
  wikilink:    { alpha: 0.7,  width: 1.4, rest: 85,  key: "--accent" },
};
const CORE = "k:kyraan";
const SHORT_TERM_DAYS = 14;        // memory/engine._SHORT_TERM_DAYS

const brain = {
  nodes: [], edges: [], byId: new Map(),
  colour: "lobe",
  since: 0,                 // ms; 0 = the whole brain, else only what was learned inside
  showType: { core: true, memory: true, person: true, episode: true, face: true,
              document: true, task: true, skill: true, contact: true,
              note: true, tag: true, place: true, care: true },
  showEdge: Object.fromEntries(Object.keys(EDGE_STYLE).map((k) => [k, true])),
  view: { x: 0, y: 0, scale: 1 },
  pan: null, orbit: null, nodeDrag: null, band: null,
  // Far in the past, not 0: "now - 0 < 12000" is true for the first
  // twelve seconds of a page's life, which silently held the spin off
  // until the page was old enough.
  lastTouch: -1e12, dragMode: "orbit",
  keys: { space: false, zoom: false },   // held modifiers: Space = pan, Cmd/Ctrl = zoom
  zoomDrag: null,
  lobeFired: new Map(),    // lobe type -> time it last received activity
  live: null,              // the last live event, for the ticker
  liveLog: [],             // the recent sequence: trying → got
  callWires: [],           // transient person↔skill wires: asked, answered
  refreshes: 0,            // merge-refreshes since load (the tests count these)
  contactsTotal: 0,        // the whole book; only the linked ones are neurons
  hover: null, selection: new Set(),
  alpha: 1, raf: null, palette: new Map(), review: null, census: null,
  fired: new Map(),        // node id -> performance.now() of its last firing
  floor: 0.45,             // synapse threshold; a control, not a constant
  findings: { orphans: [], dead: [], contested: [] },
  signals: [],             // action potentials in flight
  query: "",               // search: dims what does not match
  matches: new Set(),
  focusFor: null,          // hover focus: the hovered id the focus set was built for
  focusSet: new Set(),     // hovered node + its neighbours over visible wires
  focusMix: 0,             // 0 → nothing dimmed, 1 → full focus; eased per frame
};

/* Search dims rather than hides. Removing the misses would leave the hits
   floating with nothing around them — and in a graph the answer to "where
   is this" is mostly "next to what", so the context has to stay on screen. */
let contactLookup = null;

/* The book beyond the brain. A name with no neuron still answers the
   search, marked as outside the brain — that is what "connect contacts"
   means for the 388 that touch nothing yet. */
function searchContactBook(query) {
  clearTimeout(contactLookup);
  const body = $("contacts-body");
  if (!body) return;
  if (!query) { clear(body); verdictInto("contacts-verdict", "—"); return; }
  contactLookup = setTimeout(async () => {
    try {
      const data = await api("/api/contacts?q=" + encodeURIComponent(query));
      clear(body);
      if (!data.contacts.length) { empty(body, `no contact matches "${query}"`); verdictInto("contacts-verdict", "0"); return; }
      const inBrain = new Set(brain.nodes.filter((n) => n.type === "contact").map((n) => n.label.toLowerCase()));
      const rows = el("div", "rows");
      for (const c of data.contacts) {
        const row = el("div", "row");
        row.appendChild(el("span", "body", c.name));
        row.appendChild(el("span", "tag", inBrain.has(c.name.toLowerCase()) ? "in brain" : "outside"));
        row.title = [(c.phones || []).join(", "), (c.emails || []).join(", ")].filter(Boolean).join(" · ");
        rows.appendChild(row);
      }
      body.appendChild(rows);
      verdictInto("contacts-verdict", `${data.contacts.length} in the book`);
    } catch (_) { /* the book is optional */ }
  }, 250);
}

function runSearch(text) {
  brain.query = (text || "").trim().toLowerCase();
  brain.matches = new Set();
  searchContactBook(brain.query);
  if (!brain.query) { renderLegend(); renderMemories(); return; }
  for (const node of brain.nodes) {
    const hay = `${node.label} ${node.type} ${node.group || ""} `
              + `${node.subject || ""} ${node.kind || ""}`;
    if (hay.toLowerCase().includes(brain.query)) brain.matches.add(node.id);
  }
  renderMemories();
  const note = $("mem-note");
  if (note) {
    note.textContent = brain.matches.size
      ? `${brain.matches.size} of ${brain.nodes.length} match "${brain.query}"`
      : `nothing matches "${brain.query}"`;
  }
}

function matchAlpha(node) {
  let alpha = 1;
  if (brain.query) alpha *= brain.matches.has(node.id) ? 1 : 0.12;
  // "What's new": with a since-window set, what the brain learned inside
  // it stays lit and the rest falls back. The core never dims; a neuron
  // with no date (a skill, a contact, a person) dims by half — its age
  // is unknown, not old.
  if (brain.since) {
    if (node.type === "core") return alpha;
    const born = Date.parse(node.created || "");
    if (!isFinite(born)) alpha *= 0.5;
    else if (Date.now() - born > brain.since) alpha *= 0.12;
  }
  return alpha;
}

/* How many neurons were learned inside the since-window. */
function freshCount() {
  if (!brain.since) return 0;
  const now = Date.now();
  return brain.nodes.filter((n) => {
    const born = Date.parse(n.created || "");
    return isFinite(born) && now - born <= brain.since;
  }).length;
}

function setSince(days) {
  brain.since = days > 0 ? days * 86400000 : 0;
  const box = $("mem-since");
  if (box) box.value = String(days);
  const count = $("mem-since-count");
  if (count) count.textContent = brain.since ? `${freshCount()} new` : "";
}

/* Hover focus, the way Obsidian's graph does it: the hovered neuron, its
   neighbours over visible wires, and the wires between them stay lit;
   everything else falls back. Built once per hovered node, not per
   frame, and eased in and out so the graph does not snap. */
const FOCUS_DIM = 0.10;

function focusSetFor(node) {
  const set = new Set([node.id]);
  // A day stands for its exchanges: its wiring is theirs, seen through
  // the fold (a wire to a folded neuron lands on that neuron's day).
  const heads = node.dayNode ? new Set(node.members) : new Set([node.id]);
  for (const edge of brain.edges) {
    if (!brain.showEdge[edge.kind]) continue;
    if (heads.has(edge.a)) set.add(standIn(edge.b));
    else if (heads.has(edge.b)) set.add(standIn(edge.a));
  }
  return set;
}

/* Wires a neuron has, through the fold: for a day, its exchanges' wires
   to anything outside the day, each counted once. */
function wireCount(node) {
  if (!node.dayNode) return brain.edges.filter((e) => e.a === node.id || e.b === node.id).length;
  const members = new Set(node.members);
  const seen = new Set();
  for (const e of brain.edges) {
    const inA = members.has(e.a), inB = members.has(e.b);
    if (inA === inB) continue;
    const other = standIn(inA ? e.b : e.a);
    if (other === node.id) continue;
    seen.add(other + "|" + e.kind);
  }
  return seen.size;
}

/* What the focus follows: the hovered neuron, else the SELECTION. A
   phone has no hover, so a tap must do what the mouse does — light the
   wires of what was tapped and dim the rest (owner, 2026-09-03). On the
   desktop the same rule means a click keeps its neighbourhood lit after
   the mouse moves away; Esc clears it. */
function updateFocus() {
  const hover = brain.hover;
  const heads = hover ? [hover] : [...brain.selection].map((id) => brain.byId.get(id)).filter(Boolean);
  const key = heads.map((n) => n.id).sort().join("|");
  if (heads.length && brain.focusFor !== key) {
    brain.focusFor = key;
    brain.focusHeads = new Set(heads.flatMap((n) => n.dayNode ? [n.id, ...n.members] : [n.id]));
    brain.focusSet = new Set();
    for (const head of heads) for (const id of focusSetFor(head)) brain.focusSet.add(id);
  }
  if (!heads.length) { brain.focusFor = null; brain.focusHeads = new Set(); }
  const target = heads.length ? 1 : 0;
  brain.focusMix += (target - brain.focusMix) * (REDUCED_MOTION ? 1 : 0.28);
  if (Math.abs(brain.focusMix - target) < 0.01) brain.focusMix = target;
}

function focusAlpha(node) {
  if (brain.focusMix === 0) return 1;
  if (brain.focusSet.has(node.id)) return 1;
  return 1 - (1 - FOCUS_DIM) * brain.focusMix;
}

function focusEdgeAlpha(edge) {
  if (brain.focusMix === 0) return 1;
  const heads = brain.focusHeads || new Set();
  if (heads.has(edge.a) || heads.has(edge.b)) return 1;
  return 1 - (1 - FOCUS_DIM * 0.6) * brain.focusMix;
}

/* Signalling. A firing tool does not just light its own soma — it sends a
   pulse down every edge it actually has, and each neuron the pulse
   reaches re-fires along ITS edges, twice, decaying.

   This is still evidence, not decoration: the edges are the real ones
   (co-activation from the audit log, synapses from the embeddings), and a
   pulse only starts when a real event arrives on the stream. Nothing
   fires on a timer, so a quiet assistant shows a quiet brain — which is
   the truthful picture and the one worth being able to see. */
const SIGNAL_SPEED = { coactivation: 1.7, synapse: 1.25, subject: 1.0,
                       relation: 1.1, owns: 0.9, managed_by: 0.8 };
const MAX_SIGNALS = 60;
const MAX_HOPS = 2;                // reachable only when an emit asks to cascade

/* Send a FEW pulses out of a node along the wires an event actually
   means. The first version sampled forty of the owner's ~220 edges and
   re-fired every neuron it reached, two hops deep — one thought became a
   firework, and a firework tells you nothing. Now: a kind filter, a small
   limit, a stagger so a burst reads as a wave, and no cascade unless
   asked for (and then only one hop, three wires, along the mesh). */
function emitFrom(nodeId, opts) {
  opts = opts || {};
  const limit = opts.limit ?? 6;
  const strength = opts.strength ?? 1;
  const hop = opts.hop ?? 1;
  const kinds = opts.kinds ? new Set(opts.kinds) : null;
  if (hop > MAX_HOPS || strength < 0.18) return 0;

  const candidates = brain.edges.filter((e) =>
    (e.a === nodeId || e.b === nodeId)
    && brain.showEdge[e.kind]
    && (!kinds || kinds.has(e.kind)));
  if (!candidates.length) return 0;
  // Deterministic spread through the candidates rather than the first N,
  // so the same event lights different wires each time it happens.
  const stride = Math.max(1, Math.floor(candidates.length / limit));
  const offset = brain.signals.length % stride;
  let sent = 0;
  for (let i = offset; i < candidates.length && sent < limit; i += stride) {
    const edge = candidates[i];
    const from = nodeId, to = edge.a === nodeId ? edge.b : edge.a;
    const bNode = brain.byId.get(to), aNode = brain.byId.get(from);
    if (!aNode || !bNode || !brain.showType[aNode.type] || !brain.showType[bNode.type]) continue;
    if (brain.signals.length >= MAX_SIGNALS) break;
    brain.signals.push({
      from, to, kind: edge.kind, hop, strength,
      ca: edge.a, cb: edge.b,                       // the wire as DRAWN, a→b
      t: -sent * 0.09,                              // stagger: a wave, not a flash
      speed: (SIGNAL_SPEED[edge.kind] || 1) * 0.9,
      cascade: !!opts.cascade,
      bounce: !!opts.bounce,                        // a round trip: out, then back
    });
    sent++;
  }
  return sent;
}

/* Every pulse is a round trip from the core (owner, 2026-09-03: "the
   pulses should fire from the centre, then come back to the centre"):
   a route is a path of node ids starting at the core; the pulse walks it
   hop by hop along the wires as drawn, and when it reaches the end it
   walks the same path back. Between two nodes with no stored wire (the
   core and a person it has no `talks` edge to) it rides a transient call
   wire, so it is never seen crossing empty space. */
function edgeBetween(u, v) {
  return brain.edges.find((e) => (e.a === u && e.b === v) || (e.a === v && e.b === u)) || null;
}

function routeSegment(route, idx, strength, back) {
  const from = route[idx], to = route[idx + 1];
  const edge = edgeBetween(from, to);
  if (!edge) callWire(from, to);
  return {
    from, to, kind: edge ? edge.kind : "call", hop: idx + 1, strength,
    ca: edge ? edge.a : from, cb: edge ? edge.b : to,
    t: 0, speed: (edge ? SIGNAL_SPEED[edge.kind] || 1 : 1.3) * 0.9,
    route, idx, back,
  };
}

function emitRoute(route, opts) {
  opts = opts || {};
  route = route.map(standIn);
  route = route.filter((id, i) => i === 0 || id !== route[i - 1]);
  if (route.length < 2 || brain.signals.length >= MAX_SIGNALS) return false;
  for (const id of route) if (!brain.byId.has(id) || !brain.showType[brain.byId.get(id).type]) return false;
  const segment = routeSegment(route, 0, opts.strength ?? 1, false);
  segment.t = -(opts.delay || 0);
  brain.signals.push(segment);
  brain.fired.set(route[0], performance.now());
  return true;
}

function advanceSignals(dt) {
  if (!brain.signals.length) return;
  const arrived = [];
  brain.signals = brain.signals.filter((signal) => {
    signal.t += signal.speed * dt;
    if (signal.t < 1) return true;
    arrived.push(signal);
    return false;
  });
  const now = performance.now();
  for (const signal of arrived) {
    // Arriving lights the far neuron, dimmer than a real firing so the
    // difference between "this ran" and "this is connected" stays visible.
    const existing = brain.fired.get(signal.to);
    if (existing === undefined || now - existing > FIRE_MS * 0.6) {
      brain.fired.set(signal.to, now - FIRE_MS * (1 - signal.strength * 0.55));
    }
    if (signal.cascade) {
      emitFrom(signal.to, { hop: signal.hop + 1, strength: signal.strength * 0.5,
                            kinds: ["synapse", "coactivation"], limit: 3 });
    }
    // A routed pulse: next hop out, then the same path home.
    if (signal.route && brain.signals.length < MAX_SIGNALS) {
      const last = signal.route.length - 2;
      if (!signal.back && signal.idx < last) {
        brain.signals.push(routeSegment(signal.route, signal.idx + 1, signal.strength, false));
      } else if (!signal.back) {
        const home = [...signal.route].reverse();
        brain.signals.push(routeSegment(home, 0, signal.strength * 0.85, true));
      } else if (signal.idx < last) {
        brain.signals.push(routeSegment(signal.route, signal.idx + 1, signal.strength, true));
      }
      continue;
    }
    // A recall is a round trip: the thought reaches into memory and what
    // it finds comes back. Same wire, same orientation, walked the other
    // way — one return, never a ping-pong.
    if (signal.bounce && brain.signals.length < MAX_SIGNALS) {
      brain.signals.push({ ...signal, from: signal.to, to: signal.from,
                           t: 0, bounce: false, cascade: false,
                           strength: signal.strength * 0.85 });
    }
  }
  // Call wires expire on their own clock.
  brain.callWires = brain.callWires.filter((w) => now - w.born < w.ttl);
}

/* A call is a wire that does not exist in the store: nothing links a
   person to a skill, yet "this person's turn called this tool" is exactly
   what the event says. So the call is drawn as a transient wire — out to
   the skill when it is tried, back to the person when the result comes —
   with one pulse riding it. That is the story the old firework hid:
   asked, reached, answered. */
const CALL_TTL = 2600;

function callWire(fromId, toId) {
  if (!brain.byId.has(fromId) || !brain.byId.has(toId)) return;
  brain.callWires.push({ from: fromId, to: toId, born: performance.now(), ttl: CALL_TTL });
}

/* Current on a curve. `at(t)` gives the point at t along the wire. The
   stretch behind the head brightens and thickens toward it — charged
   wire — and the head is a white-hot core in a bloom. That reads as
   electricity flowing; a dot with a short tail read as a dot. */
function drawCurrent(ctx, at, tt, colour, ratio, scale, strength) {
  const back = Math.max(0, tt - 0.42);
  const steps = 14;
  let prev = at(back);
  for (let i = 1; i <= steps; i++) {
    const f = i / steps;
    const p = at(back + (tt - back) * f);
    ctx.strokeStyle = colour(Math.min(1, strength * (0.12 + 0.88 * f * f)));
    ctx.lineWidth = (0.5 + 2.4 * f) * ratio * scale;
    ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(prev.x, prev.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    prev = p;
  }
  const head = at(tt);
  const r = 3.0 * ratio * scale;
  const bloom = ctx.createRadialGradient(head.x, head.y, 0, head.x, head.y, r * 3.6);
  bloom.addColorStop(0, colour(Math.min(1, strength)));
  bloom.addColorStop(1, "transparent");
  ctx.fillStyle = bloom;
  ctx.beginPath(); ctx.arc(head.x, head.y, r * 3.6, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#fff";
  ctx.globalAlpha = Math.min(1, 0.5 + strength * 0.5);
  ctx.beginPath(); ctx.arc(head.x, head.y, r * 0.55, 0, Math.PI * 2); ctx.fill();
  ctx.globalAlpha = 1;
}

/* The same quadratic the edges are DRAWN with, so a pulse rides the wire
   instead of cutting across near it. */
function edgeControl(pa, pb, aId, bId) {
  // A wire out of the core is bundled: it bends through the centre of
  // the lobe it goes to, so seventy wires to the skills leave the core
  // as one tract and fan out inside the lobe, instead of a starburst
  // meeting at one point. Pulses ride the same curve.
  if (brain.lobeScreen && (aId === CORE || bId === CORE)) {
    const far = brain.byId.get(aId === CORE ? bId : aId);
    const centre = far && brain.lobeScreen[far.type];
    if (centre) {
      return { x: (pa.x + pb.x) / 2 * 0.25 + centre.x * 0.75,
               y: (pa.y + pb.y) / 2 * 0.25 + centre.y * 0.75 };
    }
  }
  // Any other wire runs OVER the cortex, not through it: the 3D midpoint
  // of its two ends, pushed out to just above the shell, is the curve's
  // control point — so a synapse between two facts arcs across the
  // surface like a gyrus instead of cutting a chord through the middle.
  // Pulses ride the same arc. Needs the current canvas and view, which
  // the draw pass leaves in brain.frame.
  const fr = brain.frame;
  if (fr && aId && bId) {
    const a = brain.byId.get(aId), b = brain.byId.get(bId);
    if (a && b && a.type !== "core" && b.type !== "core") {
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, mz = (a.z + b.z) / 2;
      const r = Math.hypot(mx / CORTEX.a, my / CORTEX.b, mz / CORTEX.c) || 1e-3;
      const chord = Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
      const lift = Math.min(1.35, 1.0 + chord * 0.35) / r;   // longer wire, higher arc
      const c = toScreen(fr.canvas, { x: mx * lift, y: my * lift, z: mz * lift }, fr.view);
      return { x: c.x, y: c.y };
    }
  }
  return { x: (pa.x + pb.x) / 2 - (pb.y - pa.y) * 0.12,
           y: (pa.y + pb.y) / 2 + (pb.x - pa.x) * 0.12 };
}

function bezierAt(pa, c, pb, t) {
  const u = 1 - t;
  return { x: u * u * pa.x + 2 * u * t * c.x + t * t * pb.x,
           y: u * u * pa.y + 2 * u * t * c.y + t * t * pb.y };
}

// How long a neuron stays lit after it fires. Long enough to catch out of
// the corner of your eye, short enough that a busy turn does not leave the
// whole skill lobe permanently on.
const FIRE_MS = 4200;
const LOBE_MS = 3600;            // how long a lobe stays lit after activity

/* Live activation. The SSE tail already carries every tool call, so the
   brain can show what is firing RIGHT NOW rather than only what exists —
   the difference between an anatomy diagram and an EEG. Fed from the same
   one connection the stream sector uses; costs nothing extra. */
/* Whose turn is this? Live events name a chat; person nodes carry theirs.
   With no chat on the event, it is the owner's — that is who talks to
   Kyraan almost every time. */
function personForChat(chatId) {
  const people = brain.nodes.filter((n) => n.type === "person");
  const hit = chatId != null && people.find((n) => n.chat_id === chatId);
  return hit || people.find((n) => n.label === "owner") || null;
}

function lightLobe(type) {
  brain.lobeFired.set(type, performance.now());
}

function lobeHeat(type) {
  const at = brain.lobeFired.get(type);
  if (at === undefined) return 0;
  return Math.max(0, 1 - (performance.now() - at) / LOBE_MS);
}

/* The ids wired to a node over the given kinds, in store order. */
function neighbourIds(id, kinds) {
  const want = new Set(kinds);
  const out = [];
  for (const e of brain.edges) {
    if (!want.has(e.kind) || !brain.showEdge[e.kind]) continue;
    if (e.a === id) out.push(e.b); else if (e.b === id) out.push(e.a);
  }
  return out;
}

function fireNode(id, emit) {
  if (!brain.byId.has(id)) return false;
  brain.fired.set(id, performance.now());
  if (emit) emitFrom(id, emit);
  return true;
}

/* One live event → what it means in the brain. Each mapping is literal:
     a MODEL CALL is the person's turn being thought about, so their node
       fires and the thought runs out along their memory wiring;
     EPISODE RAG is the recall lobe being searched — the event carries a
       count but not which episodes, so the lobe glows rather than
       inventing which neurons;
     a memory.* TOOL is the fact lobe being read;
     any other tool is its own skill firing, as before;
     the REPLY is the person's node closing the loop.
   Nothing here fires on a timer. A quiet assistant shows a quiet brain. */
function compactArgs(args) {
  return Object.entries(args || {})
    .filter(([k]) => k !== "chat_id")
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" ");
}

/* The live log is the sequence, not just the latest line: TRYING (the
   model's stated intent, the exact query) followed by GOT (the outcome).
   The events carry all of it — consider, args, ok/duration/error, how
   many episodes recall returned and how close the best was. What they do
   NOT carry is the result body; that only enters the next prompt, so the
   log says "ok · 1493ms" and never pretends to know what came back. */
const LIVE_LOG_MAX = 12;

function logLive(phase, text, detail) {
  brain.liveLog.unshift({ at: performance.now(), clock: new Date(), phase, text, detail: detail || "" });
  if (brain.liveLog.length > LIVE_LOG_MAX) brain.liveLog.length = LIVE_LOG_MAX;
  brain.live = { text, at: performance.now() };
  const line = $("mem-live");
  if (line) { line.textContent = "● " + text; line.style.opacity = "1"; }
  renderLiveLog();
}

function brainActivate(event) {
  const kind = event.kind || "";
  const who = personForChat(event.chat_id);
  if (STORE_CHANGE_KINDS.has(kind)) scheduleGraphRefresh(kind);
  trackTurn(event);

  if (kind === "model_call") {
    // Thinking reads its facts about you: a few pulses out along the
    // person's own subject wires into the fact lobe. Not into everything.
    // Thinking: the core reaches through the person to a few of the
    // facts about them and the thought comes home. Core → person → fact
    // → person → core, on the real subject wires.
    fireNode(CORE);
    if (who) {
      const facts = neighbourIds(who.id, ["subject", "relation"]).slice(0, 4);
      facts.forEach((fact, i) => emitRoute([CORE, who.id, fact], { delay: i * 0.12 }));
      if (!facts.length) emitRoute([CORE, who.id]);
    }
    lightLobe("memory");
    logLive("think", `thinking · ${event.model || event.provider || "model"}`,
      `${event.input_tokens ?? "?"} tokens in · ${event.latency_ms ?? "?"}ms`);
  } else if (kind === "episode_rag") {
    // Reaching into recall: as many wires as episodes came back (a couple
    // even when none did — it looked), along the spoke wires only.
    lightLobe("episode");
    fireNode(CORE);
    const n = Number(event.injected) || 0;
    if (who) {
      neighbourIds(who.id, ["spoke"]).slice(0, Math.max(2, Math.min(6, n)))
        .forEach((ep, i) => emitRoute([CORE, who.id, ep], { delay: i * 0.1, strength: 0.9 }));
    }
    logLive("got", `recall → ${n} episodes`,
      event.best_sim != null ? `best match ${Number(event.best_sim).toFixed(2)}` : "");
  } else if (kind === "agent_tool_call" && event.tool) {
    // The ask: one wire out from the person to the skill, and the skill
    // fires. The model's own WANT/HAVE/NEED line is the reason.
    const skill = "s:" + event.tool;
    // The ask leaves the core for the skill; the result (below) brings
    // it back. One round trip, split across the two events.
    callWire(CORE, skill);
    fireNode(CORE);
    emitRoute([CORE, skill]);
    logLive("try", `${event.tool}`, String(event.consider || "").slice(0, 140));
  } else if (kind === "tool_call" && event.tool) {
    const skill = "s:" + event.tool;
    // A scheduled run calls tools with no agent_tool_call first; draw the
    // ask then, but not twice when the loop already did.
    const recent = brain.callWires.some((w) => w.to === skill
      && performance.now() - w.born < 1500);
    if (!recent) { callWire(CORE, skill); fireNode(CORE); emitRoute([CORE, skill]); }
    // And, lightly, the two or three it habitually fires with: core →
    // skill → co-fired skill and home.
    neighbourIds(skill, ["coactivation"]).slice(0, 2)
      .forEach((other, i) => emitRoute([CORE, skill, other], { strength: 0.6, delay: 0.2 + i * 0.12 }));
    if (event.tool.startsWith("memory.")) lightLobe("memory");
    if (event.tool.startsWith("memory.recall")) lightLobe("episode");
    if (event.tool.startsWith("documents.")) lightLobe("document");
    if (event.tool.startsWith("faces.") || event.tool.startsWith("persons.")) lightLobe("face");
    logLive("try", `${event.tool} ${compactArgs(event.args)}`.trim());
  } else if (kind === "tool_result" && event.tool) {
    // The answer: one wire back from the skill to the person.
    const skill = "s:" + event.tool;
    fireNode(skill);
    callWire(skill, CORE);
    // The answer comes home: one segment, skill → core.
    brain.signals.push(routeSegment([skill, CORE], 0, 1, true));
    fireNode(CORE);
    logLive("got", event.ok
      ? `${event.tool} → ok · ${event.duration_ms ?? "?"}ms`
      : `${event.tool} → failed`,
      event.ok ? "" : String(event.error || "").slice(0, 140));
  } else if (kind === "agent_reply" || kind === "turn_health") {
    if (who && kind === "agent_reply") { callWire(CORE, who.id); fireNode(CORE); emitRoute([CORE, who.id]); }
    if (who) brain.fired.set(who.id, performance.now());
    if (kind === "agent_reply") logLive("reply", "replied",
      `${event.steps ?? "?"} steps · ${event.tier || ""}`.trim());
  } else if (event.reminder_id) {
    callWire(CORE, "t:reminder:" + event.reminder_id);
    fireNode(CORE);
    emitRoute([CORE, "t:reminder:" + event.reminder_id]);
    logLive("got", "reminder fired");
  }
}

function renderLiveLog() {
  const body = $("live-body");
  if (!body) return;
  clear(body);
  if (!brain.liveLog.length) { empty(body, "waiting — nothing is happening"); return; }
  const rows = el("div", "rows");
  for (const entry of brain.liveLog) {
    const row = el("div", "row live-" + entry.phase);
    row.appendChild(el("span", "ts", entry.clock.toLocaleTimeString([], { hour12: false })));
    row.appendChild(el("span", "live-phase", entry.phase));
    const body_ = el("span", "body", entry.text);
    if (entry.detail) body_.title = entry.detail;
    row.appendChild(body_);
    rows.appendChild(row);
    if (entry.detail) {
      const detail = el("div", "row live-detail");
      detail.appendChild(el("span", "body", entry.detail));
      rows.appendChild(detail);
    }
  }
  body.appendChild(rows);
  verdictInto("live-verdict", brain.liveLog.length + " recent", "ok");
}

const REDUCED_MOTION = window.matchMedia
  && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* -- palette: stay inside the tube ------------------------------------- */

function hslFromAccent() {
  const base = (getComputedStyle(document.documentElement)
    .getPropertyValue("--accent").trim() || "#ffb000");
  const hex = base.startsWith("#") ? base : "#ffb000";
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  const l = (max + min) / 2;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
  }
  return { h, s: Math.max(0.4, s), l };
}

/* N colours spread around the tube's own hue. Shared by the brain's
   palette and the host graph's stacked bands, so a role and a lobe are
   coloured by the same rule. (This existed, was inlined into the brain's
   buildPalette during a rewrite, and the host graph then called a name
   that no longer resolved — a throw inside a requestAnimationFrame loop
   fails silently, which is why the graph was simply blank.) */
function tubeVariants(count) {
  const base = hslFromAccent();
  const out = [];
  for (let i = 0; i < Math.max(1, count); i++) {
    const spread = count > 1 ? (i / (count - 1) - 0.5) : 0;
    out.push({ h: (base.h + spread * 82 + 360) % 360,
               s: Math.round(base.s * 100),
               l: Math.round(48 + spread * 15) });
  }
  return out;
}

function groupKey(node) {
  if (brain.colour === "lobe") return node.type;
  if (brain.colour === "group") return node.group || node.type;
  return node[brain.colour] || node.type;
}

function buildPalette() {
  const keys = [...new Set(brain.nodes.map(groupKey))].sort();
  const variants = tubeVariants(keys.length);
  brain.palette = new Map(keys.map((key, i) => [key, variants[i]]));
  // The core has its own colour (--core, one per tube), whatever the
  // colour mode says: it is not a lobe, it is the thing that has lobes.
  brain.palette.set("core", hslFromVar("--core", "#8fefff"));
}

/* The core's own colour, from the stylesheet's --core (one per tube). */
function hslFromVar(name, fallback) {
  const raw = (getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback);
  const hex = raw.startsWith("#") && raw.length === 7 ? raw : fallback;
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  const l = (max + min) / 2;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
  }
  return { h, s: Math.round(s * 100), l: Math.round(l * 100) };
}

/* The heartbeat: lub, dub, rest. 1150ms at rest; 650ms while the core
   has fired in the last four seconds — it quickens when it is working,
   which is the one thing the beat says that the wires do not. Returns
   the beat amplitude 0..1 and the phase 0..1 of the current cycle. */
function coreBeat() {
  if (REDUCED_MOTION) return { b: 0, ph: 0, quick: false };
  const fired = brain.fired.get(CORE);
  const quick = fired !== undefined && performance.now() - fired < 4000;
  const period = quick ? 650 : 1150;
  const ph = (performance.now() % period) / period;
  const b = Math.exp(-Math.pow(ph - 0.06, 2) / 0.003)
          + 0.55 * Math.exp(-Math.pow(ph - 0.30, 2) / 0.004);
  return { b: Math.min(1, b), ph, quick };
}

function nodeColour(node, alpha) {
  alpha *= matchAlpha(node) * focusAlpha(node);
  const hsl = brain.palette.get(groupKey(node)) || { h: 40, s: 80, l: 50 };
  // Superseded memories stay in the brain — they are part of what it
  // holds — but they no longer speak, so they are dimmed.
  const lightness = node.active === false ? Math.round(hsl.l * 0.45) : hsl.l;
  return `hsla(${hsl.h}, ${hsl.s}%, ${lightness}%, ${alpha})`;
}

function nodeRadius(node) {
  if (node.type === "core") return 12;
  if (node.type === "place") return 6.5;
  if (node.type === "care") return node.status === "done" ? 4.2 : 5.2;
  if (node.dayNode) return 4 + Math.log2((node.members || []).length + 1) * 2.2;
  if (node.type === "person") return 7.5;
  if (node.type === "task") return 6.5;
  // An episode is one exchange — small and numerous by nature.
  if (node.type === "episode") return 2.8;
  if (node.type === "document") return 4 + Math.min(4, (node.chunks || 1) * 0.6);
  if (node.type === "face") return 5.5 + (node.templates || 1) * 0.7;
  if (node.type === "contact") return 4.2;
  if (node.type === "note") return 4.8 + Math.min(3, (node.chunks || 1) * 0.5);
  if (node.type === "tag") return 3.6 + Math.min(4, (node.notes || 2) * 0.6);
  if (node.type === "skill") {
    // Log scale: home.get_state ran 1236 times and calendar.list_events 69.
    // Linear would make one node the size of the lobe.
    return 3.4 + Math.log10((node.uses || 0) + 1) * 1.9;
  }
  return node.importance === "high" ? 5.4 : 4.0;
}

/* -- layout ------------------------------------------------------------ */

function visibleNodes() {
  return brain.nodes.filter((n) => brain.showType[n.type] && !n.hidden);
}

/* Semantic zoom for the recall lobe. 212 episodes were the hairball at
   the centre, and the lobe grows by ~15 a day. Zoomed out, the episodes
   fold into their DAYS — one neuron per day, sized by how many exchanges
   it holds, placed at the centroid of the exchanges it stands for; zoom
   in past the threshold and the day opens into its episodes again. The
   wires an episode has are drawn to its day while it is folded. Hysteresis
   so the boundary does not flicker: fold below 1.15, open above 1.35.
   Only when the lobe is big enough to need it (> 60 episodes). */
const FOLD_BELOW = 0.95, OPEN_ABOVE = 1.15, FOLD_WHEN_MORE_THAN = 60;

function dayLabel(day) {
  const d = new Date(day + "T00:00:00");
  if (!isFinite(d)) return day;
  return d.getDate() + " " + ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()];
}

function updateCollapse(scale) {
  const episodes = brain.nodes.filter((n) => n.type === "episode" && !n.dayNode);
  const key = episodes.length + ":" + (episodes[0] || {}).id + ":" + (episodes[episodes.length - 1] || {}).id;
  if (brain.dayKey !== key) {
    // (Re)build the day neurons for this set of episodes.
    brain.nodes = brain.nodes.filter((n) => !n.dayNode);
    const byDay = new Map();
    for (const ep of episodes) {
      const day = ep.day || "undated";
      if (!byDay.has(day)) byDay.set(day, []);
      byDay.get(day).push(ep.id);
    }
    brain.dayOf = new Map();
    for (const [day, members] of byDay) {
      const id = "e:day:" + day;
      brain.nodes.push({
        id, type: "episode", dayNode: true, lobe: "recall",
        label: `${dayLabel(day)} · ${members.length}`,
        day, group: day, members, created: day, hidden: true,
        x: 0, y: 0, z: 0, vx: 0, vy: 0, vz: 0, pinned: true,
      });
      for (const m of members) brain.dayOf.set(m, id);
    }
    brain.byId = new Map(brain.nodes.map((n) => [n.id, n]));
    brain.dayKey = key;
    brain.folded = false;
  }
  const want = episodes.length > FOLD_WHEN_MORE_THAN
    && (brain.folded ? scale < OPEN_ABOVE : scale < FOLD_BELOW);
  if (want !== brain.folded) {
    brain.folded = want;
    for (const ep of episodes) ep.hidden = want;
    for (const n of brain.nodes) if (n.dayNode) n.hidden = !want;
  }
  if (brain.folded) {
    // A day sits where its exchanges are.
    for (const n of brain.nodes) {
      if (!n.dayNode) continue;
      let x = 0, y = 0, z = 0, k = 0;
      for (const m of n.members) { const e = brain.byId.get(m); if (e) { x += e.x; y += e.y; z += e.z; k++; } }
      if (k) { n.x = x / k; n.y = y / k; n.z = z / k; }
    }
  }
}

/* The neuron that stands for `id` right now: itself, or its day while
   the recall lobe is folded. */
function standIn(id) {
  const node = brain.byId.get(id);
  if (node && node.hidden && brain.dayOf && brain.dayOf.has(id)) return brain.dayOf.get(id);
  return id;
}

function seedPositions() {
  const spread = 0.34;
  brain.nodes.forEach((node, i) => {
    const anchor = anchorFor(node);
    // Deterministic jitter: the same brain comes back the same way, so
    // the layout is somewhere you can learn rather than a new scatter.
    const angle = (i * 137.508) * Math.PI / 180;      // golden angle
    const radius = spread * Math.sqrt((i % 40) / 40);
    node.x = anchor[0] + Math.cos(angle) * radius;
    node.y = anchor[1] + Math.sin(angle) * radius;
    // Deterministic depth jitter too, or every lobe starts as a disc.
    node.z = (anchor[2] || 0) + Math.sin(i * 0.7) * spread * 0.6;
    node.vx = 0; node.vy = 0; node.vz = 0; node.pinned = false;
  });
}

/* One step of a plain spring/repulsion simulation. 100-odd nodes makes
   the O(n²) repulsion about 10k cheap operations a frame — a quadtree
   would be more code than the whole sector deserves at this size. */
function simulate() {
  const nodes = visibleNodes();
  if (!nodes.length) return;
  // Tuned against the live graph (102 nodes): with a weaker anchor the
  // repulsion won and the four lobes smeared into one cloud, which is
  // exactly the picture a brain view must not produce.
  const REPEL = 0.00011, SPRING = 0.009, ANCHOR = 0.08, DAMP = 0.86;

  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
      let d2 = dx * dx + dy * dy + dz * dz;
      if (d2 < 1e-6) { dx = (i - j) * 1e-3; dy = 1e-3; dz = 1e-3; d2 = 1e-6; }
      // Region tension: a neuron pushes a neighbour from ANOTHER lobe
      // away 2.4× harder than one of its own, so regions keep clear
      // edges on the shared shell instead of bleeding into each other.
      const tension = a.type === b.type ? 0.85 : 2.4;
      const force = Math.min(REPEL * tension / d2, 0.06);
      const d = Math.sqrt(d2);
      a.vx += (dx / d) * force; a.vy += (dy / d) * force; a.vz += (dz / d) * force;
      b.vx -= (dx / d) * force; b.vy -= (dy / d) * force; b.vz -= (dz / d) * force;
    }
  }

  for (const edge of brain.edges) {
    if (!brain.showEdge[edge.kind]) continue;
    const a = brain.byId.get(edge.a), b = brain.byId.get(edge.b);
    if (!a || !b || !brain.showType[a.type] || !brain.showType[b.type] || a.hidden || b.hidden) continue;
    const style = EDGE_STYLE[edge.kind] || EDGE_STYLE.synapse;
    const rest = style.rest / 900;
    const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
    const d = Math.hypot(dx, dy, dz) || 1e-6;
    const pull = (d - rest) * SPRING * Math.min(1.4, 0.35 + (edge.weight || 0.5));
    a.vx += (dx / d) * pull; a.vy += (dy / d) * pull; a.vz += (dz / d) * pull;
    b.vx -= (dx / d) * pull; b.vy -= (dy / d) * pull; b.vz -= (dz / d) * pull;
  }

  // A big lobe generates far more internal repulsion than a small one, so
  // a flat anchor let the 179-node recall lobe blow across the whole
  // canvas and swallow the 43-node fact lobe. Scale the pull with the
  // crowd it has to hold together.
  const lobeSize = {};
  for (const node of nodes) lobeSize[node.type] = (lobeSize[node.type] || 0) + 1;

  for (const node of nodes) {
    // A day neuron sits at its exchanges' centroid (updateCollapse).
    if (node.dayNode) { node.vx = 0; node.vy = 0; node.vz = 0; continue; }
    // The core does not drift: everything else arranges itself around it.
    if (node.type === "core") {
      node.x = 0; node.y = 0; node.z = 0; node.vx = 0; node.vy = 0; node.vz = 0;
      continue;
    }
    const anchor = anchorFor(node);
    // Nothing but the ring may sit on the core: inside 0.45 of the
    // origin a neuron is pushed back out. This is the "dense single
    // point" — the wires converged there and so did the neurons.
    {
      const d0 = Math.hypot(node.x, node.y, node.z);
      if (d0 < 0.30 && !(LOBES[node.type] || {}).ring) {
        const push = (0.45 - d0) * 0.08 / (d0 || 1e-3);
        node.vx += node.x * push; node.vy += node.y * push; node.vz += node.z * push;
      }
    }
    // A ring node is held to its own point a little harder: the ring is
    // the layout, not a suggestion.
    const onRing = !!(LOBES[node.type] || {}).ring;
    const dragged = !!(brain.nodeDrag && brain.nodeDrag.group
                       && (brain.nodeDrag.group.includes ? brain.nodeDrag.group.includes(node)
                                                          : brain.nodeDrag.group.has && brain.nodeDrag.group.has(node)));
    if (onRing && !dragged) {
      // The ring is the layout, not a suggestion: the owner's 251 wires
      // pulled it out to 0.94 while the others sat at 0.3–0.5. Ease each
      // person back to its own point; the wires still bend toward it.
      node.x += (anchor[0] - node.x) * 0.3;
      node.y += (anchor[1] - node.y) * 0.3;
      node.z += (anchor[2] - node.z) * 0.3;
      node.vx *= 0.5; node.vy *= 0.5; node.vz *= 0.5;
      continue;
    }
    const pull = ANCHOR * (1 + 0.55 * Math.log10(lobeSize[node.type] || 1));
    node.vx += (anchor[0] - node.x) * pull;
    node.vy += (anchor[1] - node.y) * pull;
    node.vz += ((anchor[2] || 0) - node.z) * pull;
    if (node.pinned) { node.vx = 0; node.vy = 0; node.vz = 0; continue; }
    node.vx *= DAMP; node.vy *= DAMP; node.vz *= DAMP;
    node.x += node.vx * brain.alpha;
    node.y += node.vy * brain.alpha;
    node.z += node.vz * brain.alpha;
    // The cortex: hold the neuron in the shell between `inner` and 1 of
    // the ellipsoid, and off the midline by `groove`, so the cloud is
    // one oval with a fissure — the shape of the thing, made of the
    // evidence itself.
    if (!node.dayNode) {
      const { a, b, c, inner, groove } = CORTEX;
      const r = Math.hypot(node.x / a, node.y / b, node.z / c) || 1e-3;
      const want = r < inner ? inner : r > 1 ? 1 : r;
      if (want !== r) {
        const k = want / r;                       // onto the shell: the springs must not win
        node.x *= k; node.y *= k; node.z *= k;
      }
      if (groove && Math.abs(node.z) < groove) node.z += (node.z < 0 ? -1 : 1) * (groove - Math.abs(node.z)) * 0.5;
    }
  }
  brain.alpha = Math.max(0.02, brain.alpha * 0.99);
}

function reheat(to = 1) { brain.alpha = to; }

/* -- projection to screen ---------------------------------------------- */

/* The camera. Yaw around Y then pitch around X, then a perspective
   divide: near things grow, far things shrink and dim, and the draw order
   is far-to-near so a neuron in front actually occludes one behind.
   Shared by the brain and the hub — same graph, same eye.

   No library. A 3D graph is a rotation matrix and a divide; three.js
   would be 600KB of vendored script to do two multiplies. */
const brainCam = { yaw: -0.7, pitch: 0.3, roll: 0, spin: true };
const SPIN_RATE = 0.0006;        // ~2°/s: a slow turn you can read, not a spinner
// Two gains, opposite signs, both by the owner's hand. Sideways, a drag
// turns the GRAPH: drag right and the near face goes right, like a globe
// under a finger (negative). Up and down, a drag tilts the CAMERA: drag
// down and you look down onto the top (positive). Mouse and touch share
// both.
const YAW_GAIN = -0.0022;        // radians per pixel of sideways drag
const PITCH_GAIN = 0.0022;       // radians per pixel of vertical drag
const IDLE_BEFORE_SPIN_MS = 12000;
const FOCAL = 4.2;
const HOME_CAM = { yaw: -0.7, pitch: 0.3 };     // three-quarter: a globe of regions

function rotateToCamera(node) {
  const cy = Math.cos(brainCam.yaw), sy = Math.sin(brainCam.yaw);
  const cp = Math.cos(brainCam.pitch), sp = Math.sin(brainCam.pitch);
  const z = node.z || 0;
  const x1 = node.x * cy + z * sy;
  const z1 = -node.x * sy + z * cy;
  return { x: x1, y: node.y * cp - z1 * sp, z: node.y * sp + z1 * cp };
}

function toCamera(node) {
  const r = rotateToCamera(node);
  // Roll: a turn about the viewing axis, the two-finger twist on a
  // phone. Applied in the camera plane, before the pan offset, so a
  // pan still follows the finger whichever way the brain is rolled.
  const cr = Math.cos(brainCam.roll || 0), sr = Math.sin(brainCam.roll || 0);
  const rx = r.x * cr - r.y * sr, ry = r.x * sr + r.y * cr;
  const p = FOCAL / (FOCAL + r.z);       // perspective factor
  return { x: rx * p, y: ry * p, z: r.z, p };
}

function toScreen(canvas, node, view) {
  view = view || brain.view;
  const size = Math.min(canvas.width, canvas.height) * 0.42;
  const c = toCamera(node);
  return {
    x: canvas.width / 2 + (c.x * size + view.x) * view.scale,
    y: canvas.height / 2 + (c.y * size + view.y) * view.scale,
    p: c.p, z: c.z,
  };
}

/* 0 at the front of the volume, 1 at the back — for size and fade. */
/* The far side of the globe falls back (owner, 2026-09-04: "the cluster
   behind makes the front less readable"). Camera-space depth z is 0 at
   the centre plane; from −0.12 to +0.32 the factor eases from 1 down to
   0.2, so what lies behind the centre reads as the back of an opaque
   thing rather than a second layer printed over the front. */
function backFade(z) {
  const t = Math.max(0, Math.min(1, (z + 0.12) / 0.44));
  const s = t * t * (3 - 2 * t);
  return 1 - 0.8 * s;
}

function depthOf(z) {
  // Steeper than the volume alone would need: the far hemisphere must
  // fall back behind the near one for the mass to read as a solid.
  return Math.pow(Math.max(0, Math.min(1, (z + 1.8) / 3.6)), 0.7);
}

/* A drag in the camera plane, expressed in world axes: the inverse of
   the rotation above, so a dragged neuron follows the pointer whichever
   way the brain is turned. */
function screenDeltaToWorld(dx, dy) {
  // Undo the roll first: the camera plane is rolled, the world is not.
  const cr = Math.cos(brainCam.roll || 0), sr = Math.sin(brainCam.roll || 0);
  [dx, dy] = [dx * cr + dy * sr, -dx * sr + dy * cr];
  const cy = Math.cos(brainCam.yaw), sy = Math.sin(brainCam.yaw);
  const cp = Math.cos(brainCam.pitch), sp = Math.sin(brainCam.pitch);
  const y1 = dy * cp, z1 = -dy * sp;           // Rx(-pitch) on (dx, dy, 0)
  return { x: dx * cy - z1 * sy, y: y1, z: dx * sy + z1 * cy };   // Ry(-yaw)
}

function advanceCamera() {
  // The idle turn. Paused while the pointer is on a neuron or doing
  // anything, so a tooltip never drifts out from under the cursor.
  if (!brainCam.spin || REDUCED_MOTION) return;
  if (brain.hover || brain.nodeDrag || brain.orbit || brain.pan || brain.band) return;
  // The angle you just set is the angle you wanted. Spin only resumes
  // after the pointer has been away for a while — otherwise every view
  // you arranged drifted off the moment you let go.
  if (performance.now() - brain.lastTouch < IDLE_BEFORE_SPIN_MS) return;
  brainCam.yaw += SPIN_RATE;
}

function canvasPoint(canvas, event) {
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  return { x: (event.clientX - box.left) * ratio,
           y: (event.clientY - box.top) * ratio };
}

/* -- draw -------------------------------------------------------------- */

/* ONE renderer, two canvases. The overview's hub is not a second
   metaphor for the brain — it is the SAME graph, the same nodes, edges,
   lobes and colours, drawn small. Two drawings of one system that do not
   match teach the reader that neither is the system. */
/* A synapse's brightness is its cosine: at the floor it is faint, at 0.95
   it is as bright as the style allows. Structural wires are flat. */
function edgeStrength(edge) {
  if (edge.kind !== "synapse" || edge.weight == null) return 1;
  const span = Math.max(0.05, 0.95 - brain.floor);
  return 0.45 + 0.55 * Math.min(1, Math.max(0, (edge.weight - brain.floor) / span));
}

function drawGraph(canvas, view, opts) {
  opts = opts || {};
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;

  const styles = getComputedStyle(document.documentElement);
  const dim = styles.getPropertyValue("--dim").trim() || "#888";
  const bad = styles.getPropertyValue("--bad").trim() || "#f55";
  const text = styles.getPropertyValue("--text").trim() || "#ddd";
  const panel = styles.getPropertyValue("--panel").trim() || "#111";
  const nodes = visibleNodes();
  const shown = new Set(nodes.map((n) => n.id));
  brain.frame = { canvas, view };            // for edgeControl's surface arcs
  updateFocus();
  // Project once per frame. Everything below reads from this map, and the
  // somas are drawn back-to-front so the front of the brain occludes the
  // back instead of the last node in the array winning.
  const proj = new Map(nodes.map((n) => [n.id, toScreen(canvas, n, view)]));
  const ordered = [...nodes].sort((a, b) => proj.get(b.id).z - proj.get(a.id).z);

  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Lobe halos — the brain has regions, and they should read as regions.
  const drawnLabels = [];   // rects {x, y, w, h} of every label on this frame
  brain.labelStats = { drawn: 0, skipped: 0 };
  brain.captionHits = [];
  brain.lobeScreen = {};
  {
    for (const [type, lobe] of Object.entries(LOBES)) {
      const members = nodes.filter((n) => n.type === type);
      if (!members.length) continue;
      const points = members.map((n) => proj.get(n.id));
      const cx = points.reduce((s, p) => s + p.x, 0) / points.length;
      const cy = points.reduce((s, p) => s + p.y, 0) / points.length;
      if (type !== "core") brain.lobeScreen[type] = { x: cx, y: cy };
      if (members.length < 2) continue;
      const spread = Math.max(...points.map((p) => Math.hypot(p.x - cx, p.y - cy)))
                   + 46 * ratio;
      // A lobe that just received activity glows brighter and fades: the
      // recall lobe lighting up IS "it is searching its memory".
      const heat = lobeHeat(type);
      const cz = points.reduce((s, p) => s + p.z, 0) / points.length;
      const back = backFade(cz);
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, spread);
      glow.addColorStop(0, nodeColour(members[0], (0.16 + 0.3 * heat) * back));
      glow.addColorStop(1, nodeColour(members[0], 0));
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(cx, cy, spread, 0, Math.PI * 2); ctx.fill();
      ctx.font = `${11 * ratio}px ui-monospace, monospace`;
      ctx.fillStyle = dim; ctx.globalAlpha = 0.8 * Math.max(0.35, back);
      const held = members.reduce((s, n) => s + (n.members ? n.members.length : 1), 0);
      const caption = lobe.label.toUpperCase() + "  " + held
        + (type === "contact" && brain.contactsTotal ? ` of ${brain.contactsTotal}` : "")
        + (type === "episode" && members.some((n) => n.dayNode) ? ` · ${members.length} days` : "");
      const captionWidth = ctx.measureText(caption).width;
      // Clamped into the canvas: a lobe drifting off the top edge took
      // its own label with it and the regions went unnamed.
      const lx = Math.min(Math.max(cx - captionWidth / 2, 8 * ratio),
                          canvas.width - captionWidth - 8 * ratio);
      let ly = Math.min(Math.max(cy - spread - 8 * ratio, 16 * ratio),
                        canvas.height - 8 * ratio);
      // Two lobes whose centres are close printed their captions on top of
      // each other — "FACTRECALL 179". Stack them instead of overprinting.
      for (const placed of drawnLabels) {
        if (Math.abs(placed.y - ly) < 13 * ratio
            && lx < placed.x + placed.w && placed.x < lx + captionWidth) {
          ly = placed.y - 14 * ratio;
        }
      }
      drawnLabels.push({ x: lx, y: ly - 11 * ratio, w: captionWidth, h: 13 * ratio });
      // A caption is a target: tap it to fly to its lobe (see captionAt).
      brain.captionHits.push({ type, x: lx - 4 * ratio, y: ly - 14 * ratio,
                               w: captionWidth + 8 * ratio, h: 20 * ratio });
      ctx.fillText(caption, lx, ly);
      ctx.globalAlpha = 1;
    }
  }

  // A wire carrying a pulse is drawn bright for as long as it carries it.
  // The structural wires (spoke, subject) sit at 10-16% alpha, so a pulse
  // riding one looked like it was crossing empty space — the string it
  // was on was there, just invisible.
  const carrying = new Set();
  for (const signal of brain.signals) {
    carrying.add(signal.from < signal.to ? signal.from + "|" + signal.to
                                         : signal.to + "|" + signal.from);
  }

  // Axons. A selected node's edges are drawn bright so "what is this
  // wired to" is answered by clicking rather than by squinting.
  const drawnFolded = new Set();
  for (const edge0 of brain.edges) {
    if (!brain.showEdge[edge0.kind]) continue;
    let edge = edge0;
    if (brain.folded) {
      const ra = standIn(edge0.a), rb = standIn(edge0.b);
      if (ra !== edge0.a || rb !== edge0.b) {
        if (ra === rb) continue;                       // inside one day
        const key = ra < rb ? ra + "|" + rb + "|" + edge0.kind : rb + "|" + ra + "|" + edge0.kind;
        if (drawnFolded.has(key)) continue;
        drawnFolded.add(key);
        edge = { ...edge0, a: ra, b: rb };
      }
    }
    if (!shown.has(edge.a) || !shown.has(edge.b)) continue;
    const a = brain.byId.get(edge.a), b = brain.byId.get(edge.b);
    const style = EDGE_STYLE[edge.kind] || EDGE_STYLE.synapse;
    const touched = brain.selection.has(edge.a) || brain.selection.has(edge.b)
                 || (brain.hover && (brain.hover.id === edge.a || brain.hover.id === edge.b))
                 || carrying.has(edge.a < edge.b ? edge.a + "|" + edge.b
                                                 : edge.b + "|" + edge.a);
    // A wire is only as visible as the dimmer of its two ends.
    const lit = Math.min(matchAlpha(a), matchAlpha(b)) * focusEdgeAlpha(edge);
    const pa = proj.get(edge.a), pb = proj.get(edge.b);
    const farness = (1 - 0.55 * (depthOf(pa.z) + depthOf(pb.z)) / 2)
                  * Math.min(backFade(pa.z), backFade(pb.z));
    ctx.strokeStyle = edge.contested ? bad
      : (styles.getPropertyValue(style.key).trim() || dim);
    const strength = edgeStrength(edge);
    // A wire between two lobes rests at 40%: its arc crossed the region
    // in between and smeared the two together. Touched, it is full.
    const crossing = a.type !== b.type && a.type !== "core" && b.type !== "core" && !touched ? 0.4 : 1;
    ctx.globalAlpha = (touched ? Math.min(1, style.alpha * 2.4) : style.alpha)
                    * lit * farness * strength * crossing;
    ctx.lineWidth = (touched ? style.width * 1.8 : style.width) * ratio * (0.6 + 0.4 * strength);
    if (style.dash) ctx.setLineDash([4 * ratio, 4 * ratio]);
    ctx.beginPath();
    ctx.moveTo(pa.x, pa.y);
    // A slight bow keeps parallel edges between the same regions apart;
    // a wire from the core runs through its lobe's centre (edgeControl).
    const ec = edgeControl(pa, pb, edge.a, edge.b);
    ctx.quadraticCurveTo(ec.x, ec.y, pb.x, pb.y);
    ctx.stroke();
    if (style.dash) ctx.setLineDash([]);
  }
  ctx.globalAlpha = 1;

  // Call wires: the ask going out, the answer coming back. A dashed arc
  // between person and skill that fades over its life, with one bright
  // pulse riding from the caller to the called.
  for (const wire of brain.callWires) {
    if (!shown.has(wire.from) || !shown.has(wire.to)) continue;
    const pa = proj.get(wire.from), pb = proj.get(wire.to);
    const life = (performance.now() - wire.born) / wire.ttl;       // 0 → 1
    const fade = 1 - life;
    const control = edgeControl(pa, pb, wire.from, wire.to);
    const accentColour = styles.getPropertyValue("--accent").trim() || dim;
    ctx.strokeStyle = accentColour;
    ctx.globalAlpha = 0.55 * fade;
    ctx.lineWidth = 1.2 * ratio;
    ctx.setLineDash([5 * ratio, 4 * ratio]);
    ctx.beginPath();
    ctx.moveTo(pa.x, pa.y);
    ctx.quadraticCurveTo(control.x, control.y, pb.x, pb.y);
    ctx.stroke();
    ctx.setLineDash([]);
    const t = Math.min(1, life * 2.2);                              // arrives early, wire lingers
    const target = brain.byId.get(wire.to);
    drawCurrent(ctx, (u) => bezierAt(pa, control, pb, u), t,
                (alpha) => nodeColour(target, alpha * fade), ratio,
                (opts.nodeScale || 1) * 1.15, 1);
  }

  // Action potentials in flight. Drawn after the wires and before the
  // somas: a pulse rides over its edge but passes behind the neurons.
  for (const signal of brain.signals) {
    if (signal.t < 0) continue;                    // staggered, not started yet
    const a = brain.byId.get(signal.from), b = brain.byId.get(signal.to);
    if (!a || !b || !shown.has(signal.from) || !shown.has(signal.to)) continue;
    // Ride the wire EXACTLY as drawn. The bow of the quadratic flips side
    // depending on which end is "a", so the curve is built in the edge's
    // own orientation and the pulse walks it forwards or backwards.
    // Building it from the travel direction put every reverse-travelling
    // pulse on a curve mirrored to the other side of its wire.
    const ca = proj.get(signal.ca), cb = proj.get(signal.cb);
    const control = edgeControl(ca, cb, signal.ca, signal.cb);
    const reverse = signal.from !== signal.ca;
    const at = (t) => bezierAt(ca, control, cb, reverse ? 1 - t : t);
    const tt = Math.min(1, signal.t);
    const strength = signal.strength * (0.55 + 0.45 * Math.sin(Math.PI * tt));
    drawCurrent(ctx, at, tt, (alpha) => nodeColour(b, alpha), ratio,
                opts.nodeScale || 1, strength);
  }

  // Somas, back to front.
  for (const node of ordered) {
    const p = proj.get(node.id);
    const selected = brain.selection.has(node.id);
    const seed = node.id.charCodeAt(2) % 13;
    const beat = node.type === "core" ? coreBeat() : null;
    const breath = beat ? 1 + 0.22 * beat.b
                 : REDUCED_MOTION ? 1 : 1 + Math.sin(performance.now() / 1400 + seed) * 0.08;
    // Perspective on the radius and a fade on the whole node: the two
    // cues that make a rotating cloud read as depth rather than as a
    // scatter that happens to be moving.
    const radius = nodeRadius(node) * (opts.nodeScale || 1) * ratio * breath
                 * Math.max(0.6, Math.min(view.scale, 2.4)) * Math.pow(p.p, 0.85);
    ctx.globalAlpha = (1 - 0.62 * depthOf(p.z)) * backFade(p.z);

    const halo = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, radius * 3.6);
    halo.addColorStop(0, nodeColour(node, selected ? 0.75 : 0.42));
    halo.addColorStop(1, nodeColour(node, 0));
    ctx.fillStyle = halo;
    ctx.beginPath(); ctx.arc(p.x, p.y, radius * 3.6, 0, Math.PI * 2); ctx.fill();

    ctx.fillStyle = nodeColour(node, node.active === false ? 0.55 : 0.98);
    ctx.beginPath();
    if (node.type === "task" || node.type === "document" || node.type === "note") {
      // Square things are ARTEFACTS — a queued job, a stored file. Shape
      // carries type even when colour is carrying group.
      ctx.rect(p.x - radius, p.y - radius, radius * 2, radius * 2);
    } else if (node.type === "skill") {
      ctx.moveTo(p.x, p.y - radius);
      ctx.lineTo(p.x + radius, p.y);
      ctx.lineTo(p.x, p.y + radius);
      ctx.lineTo(p.x - radius, p.y);
      ctx.closePath();
    } else if (node.type === "memory" && node.term === "short") {
      // Hollow: a short-term fact is held, not kept — it fades in 14
      // days. Same colour, same size, no fill: the shape says "will go".
      ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = panel;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius * 0.55, 0, Math.PI * 2);
    } else {
      ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
    }
    ctx.fill();
    if (node.type === "core") {
      // The core: a white-hot centre inside two rings — the one neuron
      // that is the system rather than something it holds. It beats:
      // the soma swells with each beat (breath above) and a ring leaves
      // it once per cycle, wider and faster while it is working.
      ctx.fillStyle = `rgba(255, 255, 255, ${(0.45 + 0.4 * beat.b) * focusAlpha(node)})`;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius * 0.38, 0, Math.PI * 2); ctx.fill();
      if (beat.b > 0 || beat.ph > 0) {
        ctx.strokeStyle = nodeColour(node, 0.5 * (1 - beat.ph));
        ctx.lineWidth = (beat.quick ? 2.0 : 1.4) * ratio * (1 - beat.ph);
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius + beat.ph * (beat.quick ? 46 : 34) * ratio, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.strokeStyle = nodeColour(node, 0.7); ctx.lineWidth = 1.1 * ratio;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius + 5 * ratio, 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = nodeColour(node, 0.35); ctx.lineWidth = 0.8 * ratio;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius + 11 * ratio, 0, Math.PI * 2); ctx.stroke();
    }

    // Firing: an expanding ring that fades, plus a brighter core. Drawn
    // after the soma so the pulse reads as coming OUT of the neuron.
    const firedAt = brain.fired.get(node.id);
    if (firedAt !== undefined) {
      const age = (performance.now() - firedAt) / FIRE_MS;
      if (age >= 1) {
        brain.fired.delete(node.id);
      } else {
        const ease = 1 - Math.pow(1 - age, 2);
        ctx.strokeStyle = nodeColour(node, 0.85 * (1 - age));
        ctx.lineWidth = 2.2 * ratio * (1 - age);
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius + ease * 26 * ratio, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = nodeColour(node, 0.9 * (1 - age));
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius * (1 + 0.5 * (1 - age)), 0, Math.PI * 2);
        ctx.fill();
      }
    }

    if (node.type === "face") {
      ctx.strokeStyle = nodeColour(node, 0.85);
      ctx.lineWidth = 1.2 * ratio;
      ctx.beginPath();
      ctx.arc(p.x, p.y, radius + 3.5 * ratio, 0, Math.PI * 2);
      ctx.stroke();
    }
    if (node.overdue) {
      ctx.strokeStyle = bad; ctx.lineWidth = 1.6 * ratio;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius + 4 * ratio, 0, Math.PI * 2); ctx.stroke();
    }
    // An orphan memory (no synapse above the floor) and a dead skill
    // (registered, never called) both get a hollow ring: present in the
    // brain, connected to nothing.
    if (node.orphan || node.dead) {
      ctx.strokeStyle = nodeColour(node, 0.6);
      ctx.lineWidth = ratio;
      ctx.setLineDash([2 * ratio, 3 * ratio]);
      ctx.beginPath();
      ctx.arc(p.x, p.y, radius + 5 * ratio, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (selected || node === brain.hover) {
      ctx.strokeStyle = nodeColour(node, 1); ctx.lineWidth = 1.3 * ratio;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius + 6 * ratio, 0, Math.PI * 2); ctx.stroke();
    }
    // Labels appear when zoomed in, or for the big structural nodes.
    // Memories carry long text, so they stay unlabelled by default — but
    // a search hit or a selection has earned its name on the canvas.
    // At rest only people are named (the core and the lobe captions
    // carry the rest); everything else earns its name by zoom, hover,
    // selection, search or focus. Naming the busiest skills at rest was
    // the last of the standing clutter.
    const named = view.scale > 2.0 || node.type === "person" || node.dayNode
      || (selected && (brain.selection.size <= 24 || view.scale > 2.0))
      || (brain.query && brain.matches.has(node.id))
      // A focused neighbourhood is named only while it is small enough
      // to read (or zoomed in): the owner's 251 neighbours as text was
      // a wall, not a label.
      || (brain.focusMix > 0.5 && brain.focusSet.has(node.id)
          && (brain.focusSet.size <= 24 || view.scale > 1.7));
    if (node.type === "core" && opts.labels !== false) {
      ctx.font = `${10.5 * ratio}px ui-monospace, monospace`;
      ctx.fillStyle = nodeColour(node, 0.95);
      ctx.textAlign = "center";
      ctx.fillText("K Y R A A N", p.x, p.y + radius + 24 * ratio);
      ctx.textAlign = "start";
    } else if (opts.labels !== false && named) {
      // No overprinting: a label that would land on one already drawn
      // this frame is skipped, unless it has been asked for (selected,
      // hovered, searched). The dense skill lobe printed a dozen names
      // on top of each other and none of them could be read.
      ctx.font = `${9.5 * ratio}px ui-monospace, monospace`;
      const text_ = node.label.slice(0, 26);
      const rect = { x: p.x + radius + 4 * ratio, y: p.y - 6 * ratio,
                     w: ctx.measureText(text_).width, h: 11 * ratio };
      // A selected day yields to collisions: the callout names it already.
      const wanted = (selected && !node.dayNode) || node === brain.hover
        || (brain.query && brain.matches.has(node.id));
      if (!wanted && backFade(p.z) < 0.5) { brain.labelStats.skipped++; continue; }
      const collides = !wanted && drawnLabels.some((r) =>
        rect.x < r.x + r.w && r.x < rect.x + rect.w && rect.y < r.y + r.h && r.y < rect.y + rect.h);
      if (collides) {
        brain.labelStats.skipped++;
      } else {
        drawnLabels.push(rect);
        brain.labelStats.drawn++;
        ctx.fillStyle = dim;
        ctx.fillText(text_, rect.x, p.y + 3 * ratio);
      }
    }
    ctx.globalAlpha = 1;
  }

  // Rubber band.
  if (brain.band && opts.band !== false) {
    const { x0, y0, x1, y1 } = brain.band;
    ctx.strokeStyle = text; ctx.globalAlpha = 0.8;
    ctx.setLineDash([5 * ratio, 4 * ratio]);
    ctx.lineWidth = ratio;
    ctx.strokeRect(Math.min(x0, x1), Math.min(y0, y1),
                   Math.abs(x1 - x0), Math.abs(y1 - y0));
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }

  // The callout: a hovered neuron's name, or — when the side consoles are
  // folded, as they are on a phone — the selected neuron's name, type and
  // wire count, so a tap tells you what you tapped.
  brain.calloutFor = null;
  if (opts.hover !== false) {
    let target = null, lines = [];
    if (brain.hover) {
      target = brain.hover; lines = [brain.hover.label.slice(0, 76)];
    } else if (brain.selection.size === 1 && (brain.sideHidden || PHONE.matches)) {
      const id = [...brain.selection][0];
      const node = brain.byId.get(id);
      if (node && shown.has(id)) {
        const wires = wireCount(node);
        target = node;
        lines = [node.label.slice(0, 44),
                 `${node.type} · ${wires} wire${wires === 1 ? "" : "s"} · double-tap zooms in`];
      }
    }
    if (target) {
      brain.calloutFor = target.id;
      const p = toScreen(canvas, target, view);
      ctx.font = `${(lines.length > 1 ? 11 : 12) * ratio}px ui-monospace, monospace`;
      const width = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 14 * ratio;
      const height = (lines.length > 1 ? 34 : 20) * ratio;
      let bx = p.x + 12 * ratio;
      if (bx + width > canvas.width) bx = Math.max(4 * ratio, p.x - 12 * ratio - width);
      let by = p.y - 7 * ratio - height;
      if (by < 4 * ratio) by = p.y + 12 * ratio;
      ctx.fillStyle = panel; ctx.globalAlpha = 0.96;
      ctx.fillRect(bx, by, width, height);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = nodeColour(target, 0.85);
      ctx.lineWidth = ratio;
      ctx.strokeRect(bx, by, width, height);
      ctx.fillStyle = text;
      ctx.fillText(lines[0], bx + 7 * ratio, by + 14 * ratio);
      if (lines[1]) {
        ctx.fillStyle = dim; ctx.font = `${9.5 * ratio}px ui-monospace, monospace`;
        ctx.fillText(lines[1], bx + 7 * ratio, by + 27 * ratio);
      }
    }
  }

}

/* Sector 06: interactive, labelled, simulated. */
let lastFrame = 0;

/* Signals advance on WALL time, once per frame, not per canvas — two
   views advancing the same pulses would run them at double speed. */
function tickSignals() {
  const now = performance.now();
  const dt = lastFrame ? Math.min(0.05, (now - lastFrame) / 1000) : 0;
  lastFrame = now;
  advanceSignals(dt);
}

function fadeLiveLine() {
  const line = $("mem-live");
  if (!line || !brain.live) return;
  const age = (performance.now() - brain.live.at) / 6000;
  line.style.opacity = String(Math.max(0, 1 - age));
  if (age >= 1) { brain.live = null; line.textContent = ""; }
}

function drawBrain() {
  const canvas = $("mem-canvas");
  if (!canvas || currentView !== "memory") return;
  fadeLiveLine();
  sizeCanvas(canvas);
  // The layout runs while it is hot and freezes when it has cooled to
  // the floor; a drag or any change reheats it. On a phone at 400 nodes
  // the O(n²) step was the frame's biggest cost for no visible motion.
  updateCollapse(brain.view.scale);
  if (brain.alpha > 0.02 || brain.nodeDrag) simulate();
  tickSignals();
  advanceCamera();
  drawGraph(canvas, brain.view, {});
  brain.raf = requestAnimationFrame(drawBrain);
}

/* The overview hub: same graph, no labels, no band, its own framing. */
function drawHub() {
  const canvas = $("hub-canvas");
  if (!canvas || currentView !== "overview") return;
  sizeCanvas(canvas);
  updateCollapse(hub.view.scale);
  if (brain.alpha > 0.02 || brain.nodeDrag) simulate();
  tickSignals();
  advanceCamera();
  // Keep it framed while the simulation is still moving, then stop: a
  // hub that re-fits forever never sits still, and one that fits once
  // frames the seed positions instead of the result.
  if (brain.nodes.length && (brain.alpha > 0.12 || !hub.fitted)) {
    focusOn(canvas, visibleNodes(), hub.view);
    hub.fitted = performance.now();
  }
  drawGraph(canvas, hub.view, { labels: false, band: false, nodeScale: 0.8 });
  hub.raf = requestAnimationFrame(drawHub);
}

function sizeCanvas(canvas) {
  if (canvas.id === "mem-canvas") {
    const portrait = canvas.clientHeight > canvas.clientWidth * 1.15;
    if (portrait !== brain.portrait) { brain.portrait = portrait; brain.ringIndex = null; reheat(1); }
  }
  const ratio = window.devicePixelRatio || 1;
  const box = canvas.getBoundingClientRect();
  if (canvas.width !== Math.round(box.width * ratio)
      || canvas.height !== Math.round(box.height * ratio)) {
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
  }
}

/* -- picking ----------------------------------------------------------- */

function nodeAt(canvas, event, view) {
  const point = canvasPoint(canvas, event);
  const ratio = window.devicePixelRatio || 1;
  let best = null, bestDistance = 16 * ratio;
  for (const node of visibleNodes()) {
    const p = toScreen(canvas, node, view);
    const distance = Math.hypot(p.x - point.x, p.y - point.y);
    if (distance < bestDistance) { best = node; bestDistance = distance; }
  }
  return best;
}

function nodesInBand(canvas) {
  if (!brain.band) return [];
  const { x0, y0, x1, y1 } = brain.band;
  const left = Math.min(x0, x1), right = Math.max(x0, x1);
  const top = Math.min(y0, y1), bottom = Math.max(y0, y1);
  return visibleNodes().filter((node) => {
    const p = toScreen(canvas, node, brain.view);
    return p.x >= left && p.x <= right && p.y >= top && p.y <= bottom;
  });
}

/* Zoom so that the screen point `about` stays where it is. The renderer
   places a node at W/2 + (c*size + view.x)*scale, so for a fixed screen
   offset K the view must shift by K*(1/s1 - 1/s0). Used by the wheel,
   Cmd-drag, and the +/- keys (about the centre). */
function zoomAbout(canvas, about, targetScale) {
  const s0 = brain.view.scale;
  const s1 = Math.max(0.35, Math.min(6, targetScale));
  const kx = about.x - canvas.width / 2, ky = about.y - canvas.height / 2;
  brain.view.x += kx * (1 / s1 - 1 / s0);
  brain.view.y += ky * (1 / s1 - 1 / s0);
  brain.view.scale = s1;
}

/* Fit everything currently shown. The force layout settles wherever the
   wiring puts it, which is not centred on the origin — without this the
   skill lobe simply walked off the bottom-right edge. Called after the
   simulation has had a moment to settle, not immediately, or it fits the
   seed positions instead of the result. */
/* The lobe whose caption is under a canvas point, or null. Tapping a
   caption flies to that lobe — the one-tap way into a region on a phone. */
function captionAt(canvas, event) {
  const p = canvasPoint(canvas, event);
  const hit = (brain.captionHits || []).find((r) =>
    p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h);
  return hit ? hit.type : null;
}

function flyToLobe(canvas, type) {
  const members = visibleNodes().filter((n) => n.type === type);
  if (!members.length) return false;
  focusOn(canvas, members);
  brain.lastTouch = performance.now();
  return true;
}

function fitAll() {
  const canvas = $("mem-canvas");
  if (canvas) focusOn(canvas, visibleNodes());
}

let fitTimer = null;
function fitWhenSettled(delay = 1400) {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(fitAll, delay);
}

/* Zoom to fit a set of nodes — the "zoom into a cluster" gesture. */
function focusOn(canvas, nodes, view) {
  view = view || brain.view;
  if (!nodes.length) return;
  const size = Math.min(canvas.width, canvas.height) * 0.42;
  const ratio = window.devicePixelRatio || 1;
  // Camera space, not world space: what has to fit is what is on screen
  // at THIS angle, perspective included.
  const cams = nodes.map((n) => toCamera(n));
  const xs = cams.map((c) => c.x), ys = cams.map((c) => c.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  view.x = -((minX + maxX) / 2) * size;
  view.y = -((minY + maxY) / 2) * size;
  // Solve the projection for scale rather than guessing a constant: the
  // renderer places a node at W/2 + (x*size + view.x)*scale, so the fit
  // is (half the canvas, less padding) over (half the span, in pixels).
  // A guessed constant is what left the whole graph as a dot in the
  // middle of an empty field.
  const pad = 30 * ratio;
  const halfX = Math.max(0.05, (maxX - minX) / 2) * size;
  const halfY = Math.max(0.05, (maxY - minY) / 2) * size;
  view.scale = Math.max(0.3, Math.min(6,
    Math.min((canvas.width / 2 - pad) / halfX,
             (canvas.height / 2 - pad) / halfY)));
}

/* -- side panels ------------------------------------------------------- */

function neighboursOf(id) {
  const out = [];
  for (const edge of brain.edges) {
    if (edge.a === id) out.push({ edge, other: brain.byId.get(edge.b) });
    else if (edge.b === id) out.push({ edge, other: brain.byId.get(edge.a) });
  }
  return out.filter((n) => n.other)
            .sort((a, b) => (b.edge.weight || 0) - (a.edge.weight || 0));
}

function kvRow(list, key, value, flagged) {
  list.appendChild(el("dt", null, key));
  list.appendChild(el("dd", flagged ? "flagged" : null, value || "—"));
}

function renderSelection() {
  const body = $("pick-body");
  clear(body);
  const ids = [...brain.selection];

  if (!ids.length) {
    empty(body, "click a neuron · shift-drag to pick a group");
    verdictInto("pick-verdict", "—");
    return;
  }

  if (ids.length > 1) {
    // A GROUP reads as a census, not as a list of 40 labels.
    const picked = ids.map((id) => brain.byId.get(id)).filter(Boolean);
    verdictInto("pick-verdict", `${picked.length} picked`, "ok");
    const byType = {};
    for (const node of picked) byType[node.type] = (byType[node.type] || 0) + 1;
    const list = el("dl", "kv");
    for (const [type, n] of Object.entries(byType).sort((a, b) => b[1] - a[1])) {
      kvRow(list, type, String(n));
    }
    body.appendChild(list);
    const inner = brain.edges.filter(
      (e) => brain.selection.has(e.a) && brain.selection.has(e.b)).length;
    body.appendChild(el("div", "sub", `${inner} edges inside the selection`));
    const focus = el("button", null, "zoom to selection");
    focus.addEventListener("click", () => focusOn($("mem-canvas"), picked));
    body.appendChild(focus);
    const skills = picked.filter((n) => n.type === "skill").map((n) => n.label);
    if (skills.length) {
      const inStream = el("button", null, `watch ${skills.length} firing`);
      inStream.addEventListener("click", () => focusToolsIn("stream", skills));
      body.appendChild(inStream);
      const inTurns = el("button", null, "turns that used them");
      inTurns.addEventListener("click", () => focusToolsIn("turns", skills));
      body.appendChild(inTurns);
    }
    for (const node of picked.slice(0, 12)) {
      const row = el("div", "census-row");
      row.appendChild(el("span", "census-name", node.label));
      row.appendChild(el("span", "tag", node.type));
      body.appendChild(row);
    }
    if (picked.length > 12) {
      body.appendChild(el("div", "sub", `+${picked.length - 12} more`));
    }
    return;
  }

  const node = brain.byId.get(ids[0]);
  if (!node) { empty(body, "gone"); return; }
  verdictInto("pick-verdict", node.type, node.overdue ? "bad" : "ok");
  body.appendChild(el("p", "fact-quote", node.label));

  const list = el("dl", "kv");
  kvRow(list, "type", node.type);
  if (node.type === "memory") {
    kvRow(list, "subject", node.subject);
    kvRow(list, "kind", node.kind);
    kvRow(list, "importance", node.importance);
    // A short-term fact is forgotten 14 days after it was learned
    // (memory/engine._SHORT_TERM_DAYS); say when.
    if (node.term === "short") {
      const born = Date.parse(node.created || "");
      const gone = isFinite(born) ? new Date(born + SHORT_TERM_DAYS * 86400000) : null;
      const left = gone ? Math.ceil((gone - Date.now()) / 86400000) : null;
      kvRow(list, "term", "short — forgotten " + (gone ? gone.toISOString().slice(0, 10)
        + (left !== null ? ` (${left <= 0 ? "due" : left + "d left"})` : "") : "after 14 days"),
        left !== null && left <= 3);
    } else {
      kvRow(list, "term", node.term || "long");
    }
    kvRow(list, "state", node.active ? "active" : "superseded", !node.active);
    kvRow(list, "created", (node.created || "").slice(0, 10));
  } else if (node.type === "skill") {
    kvRow(list, "server", node.group);
    kvRow(list, "permission", node.permission,
          node.permission === "confirm");
    kvRow(list, "effect", node.side_effects, node.side_effects === "write");
    kvRow(list, "calls", String(node.uses));
    kvRow(list, "registered", node.registered ? "yes" : "no — loop tool",
          !node.registered);
  } else if (node.dayNode) {
    kvRow(list, "day", node.day);
    kvRow(list, "exchanges", String((node.members || []).length));
    kvRow(list, "folded", "zoom in, or open below, to see each one");
  } else if (node.type === "episode") {
    kvRow(list, "day", node.day);
    kvRow(list, "with", (node.participants || []).join(", "));
  } else if (node.type === "document") {
    kvRow(list, "kind", node.doc_kind);
    kvRow(list, "chunks", String(node.chunks));
    kvRow(list, "file", node.filename || "—");
  } else if (node.type === "note") {
    kvRow(list, "path", node.path || "—");
    kvRow(list, "tags", (node.tags || []).join(" ") || "—");
    kvRow(list, "relations", (node.relations || []).join(" · ") || "—");
    kvRow(list, "event", node.event_date || "—");
    kvRow(list, "chunks", String(node.chunks));
    kvRow(list, "versions", String(node.versions || 1));
    kvRow(list, "state", node.active === false ? "gone from the vault" : "indexed",
          node.active === false);
    if (node.obsidian_url) {
      list.appendChild(el("dt", null, "open"));
      const dd = el("dd");
      const a = el("a", "obsidian-link", "in Obsidian \u2197");
      a.href = node.obsidian_url;          // a URL from the server, never markup
      a.title = node.obsidian_url;
      dd.appendChild(a);
      list.appendChild(dd);
    } else {
      kvRow(list, "open", "no vault configured", true);
    }
  } else if (node.type === "tag") {
    kvRow(list, "notes", String(node.notes));
  } else if (node.type === "contact") {
    kvRow(list, "match", node.match === "is" ? "exact name" : "alias token — confirm",
          node.match !== "is");
    kvRow(list, "person", node.person);
    kvRow(list, "phones", (node.phones || []).join(", ") || "—");
    kvRow(list, "emails", (node.emails || []).join(", ") || "—");
  } else if (node.type === "face") {
    kvRow(list, "templates", String(node.templates));
    const linked = neighboursOf(node.id).some((n) => n.edge.kind === "recognises");
    kvRow(list, "person", linked ? "linked" : "no person record", !linked);
  } else if (node.type === "core") {
    const by = {};
    for (const { edge } of neighboursOf(node.id)) by[edge.kind] = (by[edge.kind] || 0) + 1;
    kvRow(list, "acts through", `${by.acts || 0} skills`);
    kvRow(list, "talks with", `${by.talks || 0}`);
    kvRow(list, "will fire", `${by.fires || 0} scheduled`);
    kvRow(list, "received", `${by.received || 0} files and notes`);
  } else if (node.type === "place") {
    kvRow(list, "radius", node.radius_km ? node.radius_km + " km" : "—");
    kvRow(list, "remembered", (node.created || "").slice(0, 10) || "—");
    kvRow(list, "owner", node.inside ? "inside now" : "not there", false);
    kvRow(list, "last visit", (node.last_visit || "").slice(0, 16).replace("T", " ") || "—");
  } else if (node.type === "care") {
    kvRow(list, "dose", node.status, node.status === "overdue");
    if (node.done_on) kvRow(list, "given", node.done_on + (node.source ? " · " + node.source : ""));
    if (node.due) kvRow(list, "due", node.due, node.status === "overdue");
  } else if (node.type === "task" && node.task_type === "code") {
    kvRow(list, "task", "coding job");
    kvRow(list, "status", node.status, node.status === "failed");
    kvRow(list, "branch", node.branch || "—");
    kvRow(list, "started", (node.created || "").slice(0, 16).replace("T", " ") || "—");
  } else if (node.type === "task") {
    kvRow(list, "task", node.task_type);
    kvRow(list, "repeat", node.repeat || "once");
    kvRow(list, "fires", node.fires_in === null ? "unparsed"
      : relative(node.fires_in), node.overdue);
  } else {
    kvRow(list, "group", node.group);
  }
  body.appendChild(list);

  const links = neighboursOf(node.id);
  if (links.length) {
    body.appendChild(el("div", "census-head", `wired to ${links.length}`));
    for (const { edge, other } of links.slice(0, 10)) {
      const row = el("div", "census-row clickable");
      row.appendChild(el("span", "census-name", other.label));
      row.appendChild(el("span", "tag", edge.contested ? "contested" : edge.kind));
      row.addEventListener("click", () => {
        brain.selection = new Set([other.id]);
        renderSelection();
      });
      body.appendChild(row);
    }
  }
  const focus = el("button", null, node.dayNode ? "open this day" : "zoom to its wiring");
  focus.addEventListener("click", () => focusOn($("mem-canvas"),
    node.dayNode ? node.members.map((id) => brain.byId.get(id)).filter(Boolean)
                 : [node, ...links.map((l) => l.other)]));
  body.appendChild(focus);
  // One action per kind of wire this neuron has: a person's "12 facts
  // (subject)", a tag's "4 notes (tagged)", a skill's "3 co-firing".
  // Each selects exactly those neurons and frames them — the question
  // "what is this wired to, by what?" answered in one tap, on a phone
  // where the list above is folded away.
  const byKind = new Map();
  for (const { edge, other } of links) {
    if (!byKind.has(edge.kind)) byKind.set(edge.kind, []);
    byKind.get(edge.kind).push(other);
  }
  if (byKind.size) {
    const actions = el("div", "wire-actions");
    const noun = (kind, n) => ({
      subject: "facts", relation: "relations", spoke: "episodes", recalls: "facts recalled",
      about: "documents", recognises: "faces", is: "contacts", maybe: "maybe-contacts",
      owns: "work", managed_by: "skills", coactivation: "co-firing skills",
      synapse: "nearest by meaning", tagged: "tagged", illustrates: "illustrated",
      wikilink: "wikilinked notes", acts: "skills", fires: "scheduled", received: "received",
      talks: "talks with",
    })[kind] || kind;
    for (const [kind, others] of [...byKind.entries()].sort((a, b) => b[1].length - a[1].length)) {
      const btn = el("button", "wire-action", `${others.length} ${noun(kind, others.length)}`);
      btn.title = `select the ${others.length} neuron${others.length === 1 ? "" : "s"} wired by ${kind}`;
      btn.addEventListener("click", () => {
        brain.selection = new Set(others.map((n) => n.id));
        focusOn($("mem-canvas"), [node, ...others]);
        renderSelection();
        syncUrl(false);
      });
      actions.appendChild(btn);
    }
    body.appendChild(actions);
  }
  if (node.type === "skill") {
    const inStream = el("button", null, "watch it firing");
    inStream.addEventListener("click", () => focusToolsIn("stream", [node.label]));
    body.appendChild(inStream);
    const inTurns = el("button", null, "turns that used it");
    inTurns.addEventListener("click", () => focusToolsIn("turns", [node.label]));
    body.appendChild(inTurns);
  }
}

function renderFindings(graph) {
  const body = $("findings-body");
  clear(body);
  const rows = [];
  for (const name of graph.contested) {
    rows.push({ label: name, tag: "contested", ids: [] });
  }
  // A variant is ONE fact spelling one answer two ways — extraction noise
  // worth tidying, not a dispute. Calling it contested was a false alarm.
  for (const name of graph.variants || []) {
    rows.push({ label: name, tag: "variant", ids: [] });
  }
  for (const id of graph.orphans) {
    const node = brain.byId.get(id);
    if (node) rows.push({ label: node.label, tag: "orphan", ids: [id] });
  }
  for (const label of graph.dead_skills) {
    rows.push({ label, tag: "never called", ids: ["s:" + label] });
  }
  for (const label of graph.maybe_contacts || []) {
    const node = brain.nodes.find((n) => n.type === "contact" && label.startsWith(n.label + " "));
    rows.push({ label, tag: "confirm", ids: node ? [node.id] : [] });
  }
  if (!rows.length) { empty(body, "nothing loose"); verdictInto("findings-verdict", "clean", "ok"); return; }

  for (const row of rows.slice(0, 24)) {
    const line = el("div", "finding");
    line.appendChild(el("span", "finding-name", row.label));
    line.appendChild(el("span", "tag", row.tag));
    if (row.ids.length) {
      line.addEventListener("click", () => {
        brain.selection = new Set(row.ids.filter((id) => brain.byId.has(id)));
        renderSelection();
        const picked = [...brain.selection].map((id) => brain.byId.get(id));
        if (picked.length) focusOn($("mem-canvas"), picked);
        syncUrl(false);
      });
    }
    body.appendChild(line);
  }
  if (rows.length > 24) body.appendChild(el("div", "sub", `+${rows.length - 24} more`));
  verdictInto("findings-verdict",
    `${graph.orphans.length} orphan · ${graph.dead_skills.length} dead`
    + (graph.contested.length ? ` · ${graph.contested.length} contested` : ""),
    graph.contested.length ? "bad" : rows.length ? "warn" : "ok");
}

function renderMemories() {
  const body = $("memories-body");
  if (!body) return;
  clear(body);
  const REMEMBERED = { memory: "fact", episode: "recall",
                       document: "doc", face: "face", contact: "contact",
                       note: "note" };
  let facts = brain.nodes.filter((n) => REMEMBERED[n.type]
                                     && brain.showType[n.type]);
  if (brain.query) facts = facts.filter((n) => brain.matches.has(n.id));
  // Newest first: the thing you are looking for is usually the thing it
  // just learned.
  facts.sort((a, b) => (b.created || "").localeCompare(a.created || ""));

  if (!facts.length) {
    empty(body, brain.query ? "nothing remembered matches that" : "no memories");
    verdictInto("memories-verdict", "0");
    return;
  }
  const rows = el("div", "rows");
  for (const fact of facts) {
    const row = el("div", "row clickable"
                   + (fact.active === false ? " warn" : ""));
    row.appendChild(el("span", "body", fact.content || fact.label));
    row.appendChild(el("span", "tag", REMEMBERED[fact.type]));
    if (fact.subject && fact.subject !== "owner") {
      row.appendChild(el("span", "tag", fact.subject));
    }
    if (fact.active === false) row.appendChild(el("span", "tag", "superseded"));
    if (fact.orphan) row.appendChild(el("span", "tag", "orphan"));
    row.title = (fact.created || "").slice(0, 10) + " · " + fact.kind;
    // Clicking a line selects the neuron and flies to it, so the list and
    // the graph are two views of one thing rather than two lists.
    row.addEventListener("click", () => {
      brain.selection = new Set([fact.id]);
      renderSelection();
      focusOn($("mem-canvas"), [fact, ...neighboursOf(fact.id).map((n) => n.other)]);
      syncUrl(false);
    });
    rows.appendChild(row);
  }
  body.appendChild(rows);
  const total = brain.nodes.filter((n) => REMEMBERED[n.type]).length;
  verdictInto("memories-verdict",
    brain.query ? `${facts.length} of ${total}` : String(facts.length));
}

function renderLegend() {
  const legend = $("mem-legend");
  clear(legend);
  const counts = new Map();
  for (const node of visibleNodes()) {
    const key = groupKey(node);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  for (const [key, n] of [...counts].sort((a, b) => b[1] - a[1]).slice(0, 9)) {
    const row = el("div", "key");
    const swatch = el("span", "swatch");
    const hsl = brain.palette.get(key) || { h: 40, s: 80, l: 50 };
    swatch.style.background = `hsl(${hsl.h}, ${hsl.s}%, ${hsl.l}%)`;
    row.appendChild(swatch);
    row.appendChild(el("span", "label", `${key} · ${n}`));
    legend.appendChild(row);
  }
  // In lobe colour the legend says exactly what the lobes picker says, so
  // the picker carries the swatch and count and the box on the canvas
  // goes. Other colourings (cluster, subject) keep the box — the picker
  // cannot show them.
  const byLobe = brain.colour === "lobe";
  legend.hidden = byLobe;
  for (const box of document.querySelectorAll("#pick-lobes input[type=checkbox]")) {
    const type = box.id.replace("show-", "");
    const label = box.closest("label");
    for (const old of label.querySelectorAll(".swatch, .pick-count")) old.remove();
    if (!byLobe) continue;
    const hsl = brain.palette.get(type) || { h: 40, s: 80, l: 50 };
    const swatch = el("span", "swatch");
    swatch.style.background = `hsl(${hsl.h}, ${hsl.s}%, ${hsl.l}%)`;
    label.insertBefore(swatch, box.nextSibling);
    label.appendChild(el("span", "pick-count",
      String(brain.nodes.filter((n) => n.type === type && !n.dayNode).length)));
  }
}

function renderGate(review) {
  const body = $("gate-body");
  clear(body);
  const pct = Math.round(review.total_reviewed / review.needed * 100);
  body.appendChild(el("div", "figure", `${review.total_reviewed} / ${review.needed}`));
  body.appendChild(el("div", "sub", "facts reviewed toward the stage-2 gate"));
  const bar = el("div", "bar");
  const fill = el("span", pct >= 100 ? "" : "warn");
  fill.style.width = Math.min(100, pct) + "%";
  bar.appendChild(fill);
  body.appendChild(bar);
  const list = el("dl", "kv");
  const approval = review.trailing_approval === null ? "no reviews yet"
    : `${Math.round(review.trailing_approval * 100)}% (last ${review.trailing_window})`;
  kvRow(list, "approval", approval);
  kvRow(list, "needed", `${Math.round(review.rate_needed * 100)}%`);
  kvRow(list, "remaining", String(review.remaining));
  kvRow(list, "pending", String(review.pending_count));
  body.appendChild(list);
  verdictInto("gate-verdict", review.gate_met ? "met" : `${pct}%`,
              review.gate_met ? "ok" : "warn");
}

function renderCensus(graph) {
  const body = $("census-body");
  clear(body);
  for (const [head, counts] of [["neurons", graph.counts],
                                ["connections", graph.edge_counts]]) {
    const group = el("div", "census-group");
    group.appendChild(el("div", "census-head", head));
    for (const [name, n] of Object.entries(counts).sort((a, b) => b[1] - a[1])) {
      const row = el("div", "census-row");
      row.appendChild(el("span", "census-name", name));
      row.appendChild(el("span", "census-n", String(n)));
      group.appendChild(row);
    }
    body.appendChild(group);
  }
  if (graph.contested.length) {
    const group = el("div", "census-group");
    group.appendChild(el("div", "census-head", "contested"));
    for (const name of graph.contested) {
      const row = el("div", "census-row");
      row.appendChild(el("span", "census-name", name));
      row.appendChild(el("span", "tag", "two tails"));
      group.appendChild(row);
    }
    body.appendChild(group);
  }
  verdictInto("census-verdict",
    `${graph.nodes.length} · ${graph.edges.length}`);
}

/* -- load and wire ----------------------------------------------------- */

function restartBrain() {
  if (brain.raf) cancelAnimationFrame(brain.raf);
  brain.raf = null;
  drawBrain();
}

async function loadMemory() {
  const note = $("mem-note");
  note.textContent = "loading…";
  // The tail is what makes the brain live; open it even if sector 01 was
  // never visited (loadStream is a no-op when already connected).
  loadStream();
  try {
    const [graph, review] = await Promise.all([
      ensureGraph(), api("/api/memory/review"),
    ]);
    brain.selection.clear();
    renderGate(review);
    renderCensus(graph);
    renderFindings(graph);
    renderMemories();
    renderLiveLog();
    renderSelection();
    renderLegend();
    fitWhenSettled();
    note.textContent =
      (graph.demo ? "DEMO DATA · " : "")
      + `${graph.nodes.length} neurons · ${graph.edges.length} connections`
      + ` · mesh ${brain.floor.toFixed(2)}`
      + (graph.contested.length ? ` · ${graph.contested.length} contested` : "")
      + (graph.degraded ? ` · degraded: ${graph.degraded}` : "");
    if (brain.query) runSearch(brain.query);   // note and matches survive a reload
    restartBrain();
  } catch (err) {
    note.textContent = "could not load the brain: " + err.message;
  }
}

/* Dropdown checklists. Open on the button, close on outside click or
   Esc, and keep the count on the button honest — "lobes 5/7" is the only
   hint that something is hidden once the checkboxes are out of sight. */
function refreshPickSummaries() {
  for (const pick of document.querySelectorAll(".pick")) {
    const boxes = pick.querySelectorAll("input[type=checkbox]");
    const on = [...boxes].filter((b) => b.checked).length;
    const count = pick.querySelector(".pick-btn b");
    if (count) count.textContent = `${on}/${boxes.length}`;
    pick.querySelector(".pick-btn").classList.toggle("partial", on < boxes.length);
  }
}

function wirePickers() {
  const closeAll = () => {
    for (const pick of document.querySelectorAll(".pick.open")) {
      pick.classList.remove("open");
      pick.querySelector(".pick-btn").setAttribute("aria-expanded", "false");
    }
  };
  for (const pick of document.querySelectorAll(".pick")) {
    const button = pick.querySelector(".pick-btn");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const opening = !pick.classList.contains("open");
      closeAll();
      if (opening) {
        pick.classList.add("open");
        button.setAttribute("aria-expanded", "true");
      }
    });
    pick.querySelector(".pick-menu").addEventListener("click", (e) => e.stopPropagation());
    pick.addEventListener("change", refreshPickSummaries);
  }
  document.addEventListener("click", closeAll);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeAll(); });
  refreshPickSummaries();
}

/* The side column: six frames, most empty most of the time. Each console
   folds on its header, the fold is remembered, and the whole column can
   go away — the graph is the point and it should be allowed to have the
   width. */
const FOLD_KEY = "kyraan.brain.folded";

function foldedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || "[]")); }
  catch (_) { return new Set(); }
}

function wireSidePanel() {
  const view = $("view-memory");
  const side = view && view.querySelector(".memory-side");
  if (!side) return;
  const folded = foldedSet();
  for (const console_ of side.querySelectorAll(".console")) {
    const head = console_.querySelector("h2");
    if (!head || !console_.id) continue;
    if (folded.has(console_.id)) console_.classList.add("folded");
    head.title = "click to fold";
    head.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      console_.classList.toggle("folded");
      const now = foldedSet();
      if (console_.classList.contains("folded")) now.add(console_.id); else now.delete(console_.id);
      try { localStorage.setItem(FOLD_KEY, JSON.stringify([...now])); } catch (_) {}
    });
  }
  // Neither toggle refits. The view is centre-relative, so the graph
  // keeps its place and size and the freed space opens around it; the
  // refit that used to run here re-framed everything and read as "the
  // simulation reset". FIT is one key away if the space is wanted.
  const toggle = $("side-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      view.classList.toggle("side-hidden");
      brain.sideHidden = view.classList.contains("side-hidden");
      toggle.textContent = brain.sideHidden ? "\u25c2 panels" : "panels \u25b8";
      syncUrl(false);
    });
  }
  const top = $("top-toggle");
  if (top) {
    top.addEventListener("click", () => {
      view.classList.toggle("top-hidden");
      brain.topHidden = view.classList.contains("top-hidden");
      top.textContent = brain.topHidden ? "controls \u25be" : "controls \u25b4";
      syncUrl(false);
    });
  }
}

function wireMemory() {
  const canvas = $("mem-canvas");
  if (!canvas) return;
  wireSidePanel();

  canvas.addEventListener("mousedown", (event) => {
    const point = canvasPoint(canvas, event);
    if (brain.keys.zoom || event.metaKey || event.ctrlKey) {
      // Cmd/Ctrl-drag: up zooms in, down zooms out, about where you
      // pressed. Scale is recomputed from the start each move, so the
      // gesture never drifts.
      brain.zoomDrag = { anchor: point, y0: point.y, scale0: brain.view.scale };
      brain.lastTouch = performance.now();
      canvas.classList.add("dragging");
      return;
    }
    if (brain.keys.space) {
      brain.pan = point;
      brain.lastTouch = performance.now();
      canvas.classList.add("dragging");
      return;
    }
    const hit = nodeAt(canvas, event);
    if (hit) {
      // Dragging a picked node drags the whole picked group with it.
      const group = brain.selection.has(hit.id)
        ? [...brain.selection].map((id) => brain.byId.get(id)).filter(Boolean)
        : [hit];
      for (const node of group) node.pinned = true;
      brain.nodeDrag = { group, last: canvasPoint(canvas, event) };
      canvas.classList.add("dragging");
    } else if (event.shiftKey) {
      const point = canvasPoint(canvas, event);
      brain.band = { x0: point.x, y0: point.y, x1: point.x, y1: point.y };
    } else if (event.altKey || event.button === 2 || brain.dragMode === "pan") {
      // Pan: right button, alt, or the drag-mode switch set to pan.
      brain.pan = canvasPoint(canvas, event);
      canvas.classList.add("dragging");
    } else {
      brain.orbit = canvasPoint(canvas, event);
      canvas.classList.add("dragging");
    }
    brain.lastTouch = performance.now();
  });
  // Right-drag pans, so the browser menu must not eat the gesture.
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());

  canvas.addEventListener("mousemove", (event) => {
    const point = canvasPoint(canvas, event);
    if (brain.zoomDrag) {
      const dy = brain.zoomDrag.y0 - point.y;             // up = in
      zoomAbout(canvas, brain.zoomDrag.anchor,
                brain.zoomDrag.scale0 * Math.exp(dy * 0.006));
      brain.lastTouch = performance.now();
      return;
    }
    if (brain.nodeDrag) {
      const size = Math.min(canvas.width, canvas.height) * 0.42;
      for (const node of brain.nodeDrag.group) {
        // Undo the perspective at the node's own depth, then the rotation,
        // so the neuron stays under the pointer from any angle.
        const p = toCamera(node).p;
        const dx = (point.x - brain.nodeDrag.last.x) / brain.view.scale / size / p;
        const dy = (point.y - brain.nodeDrag.last.y) / brain.view.scale / size / p;
        const w = screenDeltaToWorld(dx, dy);
        node.x += w.x; node.y += w.y; node.z += w.z;
      }
      brain.nodeDrag.last = point;
      reheat(0.6);          // let the neighbours settle around the new place
      return;
    }
    if (brain.band) { brain.band.x1 = point.x; brain.band.y1 = point.y; return; }
    if (brain.orbit) {
      brainCam.yaw += (point.x - brain.orbit.x) * YAW_GAIN;
      brainCam.pitch = Math.max(-1.3, Math.min(1.3,
        brainCam.pitch + (point.y - brain.orbit.y) * PITCH_GAIN));
      brain.orbit = point;
      brain.lastTouch = performance.now();
      return;
    }
    if (brain.pan) {
      brain.view.x += (point.x - brain.pan.x) / brain.view.scale;
      brain.view.y += (point.y - brain.pan.y) / brain.view.scale;
      brain.pan = point;
      brain.lastTouch = performance.now();
      return;
    }
    brain.hover = nodeAt(canvas, event);
  });

  window.addEventListener("mouseup", () => {
    if (brain.band) {
      const picked = nodesInBand(canvas);
      brain.selection = new Set(picked.map((n) => n.id));
      brain.band = null;
      renderSelection();
      syncUrl(false);
    }
    if (brain.nodeDrag) {
      // Released nodes stay where they were put — a brain you can arrange
      // is one you can reason about. "Release" un-pins everything.
      brain.nodeDrag = null;
    }
    brain.pan = null;
    brain.orbit = null;
    brain.zoomDrag = null;
    canvas.classList.remove("dragging");
  });

  canvas.addEventListener("click", (event) => {
    const hit = nodeAt(canvas, event);
    if (!hit) {
      const lobe = captionAt(canvas, event);
      if (lobe && flyToLobe(canvas, lobe)) return;
      if (!event.shiftKey) brain.selection.clear();
    }
    else if (event.shiftKey) {
      if (brain.selection.has(hit.id)) brain.selection.delete(hit.id);
      else brain.selection.add(hit.id);
    } else {
      brain.selection = new Set([hit.id]);
    }
    renderSelection();
    syncUrl(false);
  });

  canvas.addEventListener("dblclick", (event) => {
    const hit = nodeAt(canvas, event);
    if (!hit) return;
    if (hit.dayNode) {
      brain.selection = new Set([hit.id]);
      focusOn(canvas, hit.members.map((id) => brain.byId.get(id)).filter(Boolean));
      renderSelection();
      return;
    }
    // Zoom into the cluster this neuron belongs to.
    const cluster = visibleNodes().filter((n) => groupKey(n) === groupKey(hit));
    brain.selection = new Set(cluster.map((n) => n.id));
    focusOn(canvas, cluster);
    renderSelection();
  });

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    brain.lastTouch = performance.now();
    zoomAbout(canvas, canvasPoint(canvas, event),
              brain.view.scale * Math.exp(-event.deltaY * 0.0012));
  }, { passive: false });

  // Touch. A phone sends no mousemove for a drag, so without these the
  // brain rendered on a phone and could not be turned. One finger turns
  // (or pans, in pan mode); two fingers pan and pinch-zoom about their
  // midpoint; a tap selects; nothing here scrolls the page.
  const touchState = { mode: null, last: null, dist: 0, mid: null, start: null };
  const tp = (t) => canvasPoint(canvas, { clientX: t.clientX, clientY: t.clientY });
  canvas.addEventListener("touchstart", (event) => {
    touchState.angle = null;
    event.preventDefault();
    brain.lastTouch = performance.now();
    const t = event.touches;
    if (t.length === 1) {
      const p = tp(t[0]);
      touchState.mode = "one"; touchState.last = p; touchState.start = p;
    } else if (t.length >= 2) {
      const a = tp(t[0]), b = tp(t[1]);
      touchState.mode = "two";
      touchState.dist = Math.hypot(a.x - b.x, a.y - b.y);
      touchState.mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      // The twist needs a starting angle, or the first move is lost.
      touchState.angle = Math.atan2(b.y - a.y, b.x - a.x);
    }
  }, { passive: false });
  canvas.addEventListener("touchmove", (event) => {
    event.preventDefault();
    brain.lastTouch = performance.now();
    const t = event.touches;
    if (touchState.mode === "one" && t.length === 1) {
      const p = tp(t[0]);
      const dx = p.x - touchState.last.x, dy = p.y - touchState.last.y;
      if (brain.dragMode === "pan") {
        brain.view.x += dx / brain.view.scale; brain.view.y += dy / brain.view.scale;
      } else {
        brainCam.yaw += dx * YAW_GAIN;
        brainCam.pitch = Math.max(-1.3, Math.min(1.3, brainCam.pitch + dy * PITCH_GAIN));
      }
      touchState.last = p;
    } else if (t.length >= 2) {
      const a = tp(t[0]), b = tp(t[1]);
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const angle = Math.atan2(b.y - a.y, b.x - a.x);
      if (touchState.mode === "two" && touchState.dist > 0) {
        zoomAbout(canvas, mid, brain.view.scale * (dist / touchState.dist));
        brain.view.x += (mid.x - touchState.mid.x) / brain.view.scale;
        brain.view.y += (mid.y - touchState.mid.y) / brain.view.scale;
        // Twist: the two fingers' angle turns the brain about the
        // viewing axis (owner, 2026-09-03). Wrapped, so a finger crossing
        // the ±π seam does not spin it a full turn.
        // No previous angle on the first move after touchstart: no twist.
        let twist = Number.isFinite(touchState.angle) ? angle - touchState.angle : 0;
        if (twist > Math.PI) twist -= 2 * Math.PI;
        if (twist < -Math.PI) twist += 2 * Math.PI;
        brainCam.roll = (brainCam.roll || 0) + twist;
      }
      touchState.mode = "two"; touchState.dist = dist; touchState.mid = mid;
      touchState.angle = angle;
    }
  }, { passive: false });
  canvas.addEventListener("touchend", (event) => {
    event.preventDefault();
    if (touchState.mode === "one" && touchState.start && touchState.last
        && Math.hypot(touchState.last.x - touchState.start.x,
                      touchState.last.y - touchState.start.y) < 8 * (window.devicePixelRatio || 1)) {
      // A tap: select what is under the finger, like a click. Two taps
      // within a third of a second zoom into what the neuron is wired to
      // (its focus set), or fit the whole brain when on empty space.
      const t = event.changedTouches[0];
      const hit = t ? nodeAt(canvas, { clientX: t.clientX, clientY: t.clientY }) : null;
      const now = performance.now();
      const prev = touchState.lastTap;
      const isDouble = prev && now - prev.t < 340
        && Math.hypot(touchState.start.x - prev.x, touchState.start.y - prev.y) < 24 * (window.devicePixelRatio || 1);
      touchState.lastTap = isDouble ? null : { t: now, x: touchState.start.x, y: touchState.start.y };
      const lobe = !hit && t ? captionAt(canvas, { clientX: t.clientX, clientY: t.clientY }) : null;
      if (lobe && !isDouble) {
        flyToLobe(canvas, lobe);
      } else if (isDouble) {
        if (hit && hit.dayNode) {
          brain.selection = new Set([hit.id]);
          focusOn(canvas, hit.members.map((id) => brain.byId.get(id)).filter(Boolean));
        } else if (hit) {
          brain.selection = new Set([hit.id]);
          const hood = [...focusSetFor(hit)].map((id) => brain.byId.get(id))
            .filter((n) => n && brain.showType[n.type]);
          focusOn(canvas, hood);
        } else {
          fitAll();
        }
      } else {
        brain.selection = hit ? new Set([hit.id]) : new Set();
      }
      renderSelection();
      syncUrl(false);
    }
    if (event.touches.length === 0) touchState.mode = null;
    else if (event.touches.length === 1) {
      touchState.mode = "one"; touchState.last = tp(event.touches[0]); touchState.start = touchState.last;
    }
  }, { passive: false });

  // Held modifiers. Space is the hand (pan), Cmd/Ctrl is the lens (zoom).
  // They take precedence over grabbing a neuron, so you can pan across a
  // dense lobe without picking one up. Cleared on blur, or a key held
  // when the window lost focus would stick forever.
  const typing = (event) => event.target
    && /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName);
  const cursorFor = () => {
    if (currentView !== "memory") return;
    canvas.style.cursor = brain.keys.space ? "grab"
      : brain.keys.zoom ? "zoom-in" : "default";
  };
  // Jump to search: Cmd+Space, Ctrl+Space, or "/". On a Mac the OS owns
  // Cmd+Space (Spotlight) and the page usually never sees it, which is
  // why the other two exist. If the controls row is hidden it is shown
  // first — focusing an invisible box does nothing.
  const focusSearch = () => {
    const search = $("mem-search");
    if (!search) return;
    if ($("view-memory").classList.contains("top-hidden")) $("top-toggle").click();
    search.focus();
    search.select();
  };
  window.addEventListener("keydown", (event) => {
    if (currentView !== "memory") return;
    const wantsSearch = (event.key === " " && (event.metaKey || event.ctrlKey))
                     || (event.key === "/" && !typing(event));
    if (wantsSearch) { event.preventDefault(); focusSearch(); return; }
    if (typing(event)) return;
    if (event.key === " ") {
      if (!brain.keys.space) { brain.keys.space = true; cursorFor(); }
      event.preventDefault();          // Space must not scroll the page
    }
    if (event.key === "Meta" || event.key === "Control") {
      brain.keys.zoom = true; cursorFor();
    }
  });
  window.addEventListener("keyup", (event) => {
    if (event.key === " ") { brain.keys.space = false; cursorFor(); }
    if (event.key === "Meta" || event.key === "Control") { brain.keys.zoom = false; cursorFor(); }
  });
  window.addEventListener("blur", () => {
    brain.keys.space = false; brain.keys.zoom = false;
    brain.zoomDrag = null; brain.pan = null; brain.orbit = null;
    cursorFor();
  });

  window.addEventListener("keydown", (event) => {
    if (currentView !== "memory") return;
    if (event.target && /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
    if (event.key === "Escape") { brain.selection.clear(); renderSelection(); }
    if (event.key === "+" || event.key === "=") {
      zoomAbout(canvas, { x: canvas.width / 2, y: canvas.height / 2 }, brain.view.scale * 1.25);
      brain.lastTouch = performance.now();
    }
    if (event.key === "-" || event.key === "_") {
      zoomAbout(canvas, { x: canvas.width / 2, y: canvas.height / 2 }, brain.view.scale / 1.25);
      brain.lastTouch = performance.now();
    }
    if (event.key === "0") { fitAll(); brain.lastTouch = performance.now(); }
    // Arrow keys nudge the view: the one pan that needs no gesture at all.
    const step = 48 / brain.view.scale;
    if (event.key === "ArrowLeft")  { brain.view.x += step; brain.lastTouch = performance.now(); }
    if (event.key === "ArrowRight") { brain.view.x -= step; brain.lastTouch = performance.now(); }
    if (event.key === "ArrowUp")    { brain.view.y += step; brain.lastTouch = performance.now(); }
    if (event.key === "ArrowDown")  { brain.view.y -= step; brain.lastTouch = performance.now(); }
  });

  const search = $("mem-search");
  if (search) {
    search.addEventListener("input", (e) => { runSearch(e.target.value); syncUrl(false); });
    search.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { search.value = ""; runSearch(""); syncUrl(false); search.blur(); }
      if (e.key !== "Enter" || !brain.matches.size) return;
      // Enter commits the search: select the hits and frame them.
      brain.selection = new Set(brain.matches);
      renderSelection();
      focusOn(canvas, [...brain.matches].map((id) => brain.byId.get(id)).filter(Boolean));
      syncUrl(false);
    });
  }
  $("mem-colour").addEventListener("change", (e) => {
    brain.colour = e.target.value;
    buildPalette(); renderLegend();
  });
  const since = $("mem-since");
  if (since) since.addEventListener("change", (e) => { setSince(Number(e.target.value)); syncUrl(false); });
  for (const [id, type] of [["show-memory", "memory"], ["show-person", "person"],
                            ["show-place", "place"], ["show-care", "care"],
                            ["show-episode", "episode"], ["show-document", "document"],
                            ["show-face", "face"], ["show-contact", "contact"],
                            ["show-note", "note"], ["show-tag", "tag"],
                            ["show-task", "task"], ["show-skill", "skill"]]) {
    const box = $(id);
    if (box) box.addEventListener("change", (e) => {
      brain.showType[type] = e.target.checked;
      reheat(0.8); renderLegend(); fitWhenSettled();
    });
  }
  for (const [id, kind] of [["edge-synapse", "synapse"], ["edge-relation", "relation"],
                            ["edge-coactivation", "coactivation"],
                            ["edge-structure", "structure"]]) {
    const box = $(id);
    if (box) box.addEventListener("change", (e) => {
      if (kind === "structure") {
        for (const k of ["subject", "owns", "managed_by", "spoke", "about",
                         "recognises", "recalls", "is", "maybe", "tagged"]) {
          brain.showEdge[k] = e.target.checked;
        }
      } else {
        brain.showEdge[kind] = e.target.checked;
      }
      reheat(0.5);
    });
  }
  const floor = $("mem-floor");
  if (floor) {
    floor.value = brain.floor;
    $("mem-floor-value").textContent = brain.floor.toFixed(2);
    // input = live label, change = refetch. 0.45 was a number tuned by eye
    // on 43 facts; it belongs on a control, not in a constant.
    floor.addEventListener("input", (e) => {
      $("mem-floor-value").textContent = parseFloat(e.target.value).toFixed(2);
    });
    floor.addEventListener("change", (e) => {
      brain.floor = parseFloat(e.target.value);
      loaded.delete("memory");
      ensureGraph(true);
      loadMemory();
      syncUrl(false);
    });
  }
  $("mem-fit").addEventListener("click", fitAll);
  const drag = $("mem-drag");
  if (drag) {
    drag.value = brain.dragMode;
    drag.addEventListener("change", (e) => { brain.dragMode = e.target.value; syncUrl(false); });
  }
  const spin = $("mem-spin");
  if (spin) {
    spin.checked = brainCam.spin;
    spin.addEventListener("change", (e) => { brainCam.spin = e.target.checked; syncUrl(false); });
  }
  $("mem-reset").addEventListener("click", () => {
    brain.view = { x: 0, y: 0, scale: 1 };
    brainCam.yaw = HOME_CAM.yaw; brainCam.pitch = HOME_CAM.pitch; brainCam.roll = 0;
    for (const node of brain.nodes) node.pinned = false;
    seedPositions();
    reheat(1);
    fitWhenSettled();
  });
  for (const button of document.querySelectorAll("#phosphor button")) {
    button.addEventListener("click", () => {
      if (brain.nodes.length) { buildPalette(); renderLegend(); }
    });
  }
}

/* ------------------------------------------------------------------ host */
/* Sector 07 — the MacBook itself. Two halves of one question:
   the OS says what is holding MEMORY (the local model, by a mile), and
   our own audit log says where the TIME goes. Neither can answer for the
   other: ps cannot tell a chosen call from a degraded fallback, and the
   log cannot see six resident gigabytes. */

const hostState = { history: [], roles: [], raf: null, snapshot: null };

const GB = 1024 ** 3;
function gb(bytes) { return (bytes / GB).toFixed(bytes >= 10 * GB ? 0 : 1) + " GB"; }

function gauge(label, value, pct, level, note) {
  const box = el("div", "gauge");
  const head = el("div", "gauge-head");
  head.appendChild(el("span", null, label));
  head.appendChild(el("span", "gauge-value " + (level || ""), value));
  box.appendChild(head);
  if (pct !== null && pct !== undefined) {
    const bar = el("div", "bar");
    const fill = el("span", level === "ok" ? "" : level);
    fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
    bar.appendChild(fill);
    box.appendChild(bar);
  }
  if (note) box.appendChild(el("div", "gauge-note", note));
  return box;
}

function renderHostGauges(snap) {
  const body = $("host-gauges");
  clear(body);
  const grid = el("div", "gauges");

  const load = snap.load || {};
  // Load per CORE is the number that travels: 1.0 means fully committed,
  // above it there is a queue. Raw load means nothing without the core count.
  const loadLevel = load.per_core >= 1 ? "bad" : load.per_core >= 0.7 ? "warn" : "ok";
  grid.appendChild(gauge("load / core", String(load.per_core ?? "—"),
    (load.per_core || 0) * 100, loadLevel,
    `${load["1m"]} · ${load["5m"]} · ${load["15m"]} over ${snap.cpus} cores`));

  const mem = snap.memory || {};
  const memLevel = mem.used_pct >= 90 ? "bad" : mem.used_pct >= 75 ? "warn" : "ok";
  grid.appendChild(gauge("memory", `${mem.used_pct ?? "—"}%`, mem.used_pct, memLevel,
    `${gb(mem.used || 0)} of ${gb(mem.total || 0)} · ${gb(mem.compressed || 0)} compressed`));

  const disk = snap.disk || {};
  const diskLevel = disk.used_pct >= 90 ? "bad" : disk.used_pct >= 80 ? "warn" : "ok";
  grid.appendChild(gauge("disk", `${disk.used_pct ?? "—"}%`, disk.used_pct, diskLevel,
    `${gb(disk.free || 0)} free`));

  const batt = snap.battery || {};
  if (batt.percent !== undefined && batt.percent !== null) {
    grid.appendChild(gauge("power", `${batt.percent}%`, batt.percent,
      batt.power === "ac" ? "ok" : batt.percent < 25 ? "bad" : "",
      batt.power === "ac" ? "on mains" : "on battery — scheduled jobs still fire"));
  }
  body.appendChild(grid);

  const worst = Math.max(load.per_core >= 1 ? 2 : load.per_core >= 0.7 ? 1 : 0,
                         mem.used_pct >= 90 ? 2 : mem.used_pct >= 75 ? 1 : 0,
                         disk.used_pct >= 90 ? 2 : disk.used_pct >= 80 ? 1 : 0);
  verdictInto("host-verdict", ["nominal", "watch", "pressure"][worst],
              ["ok", "warn", "bad"][worst]);
}

function renderHostRoles(snap) {
  const body = $("host-roles");
  clear(body);
  const roles = snap.roles || [];
  if (!roles.length) { empty(body, "no Kyraan processes found"); return; }
  const peak = Math.max(...roles.map((r) => r.rss), 1);
  const rows = el("div", "rows");
  for (const role of roles) {
    const row = el("div", "row");
    row.appendChild(el("span", "kind", role.role));
    const gaugeCell = el("span", "body");
    const bar = el("div", "bar");
    const fill = el("span");
    fill.style.width = Math.round(role.rss / peak * 100) + "%";
    bar.appendChild(fill);
    gaugeCell.appendChild(bar);
    gaugeCell.title = role.note;
    row.appendChild(gaugeCell);
    row.appendChild(el("span", "num", gb(role.rss)));
    row.appendChild(el("span", "num", role.cpu.toFixed(1) + "%"));
    rows.appendChild(row);
  }
  body.appendChild(rows);
  const total = roles.reduce((sum, r) => sum + r.rss, 0);
  const share = snap.memory && snap.memory.total
    ? Math.round(total / snap.memory.total * 100) : null;
  verdictInto("host-roles-note",
    gb(total) + (share === null ? "" : ` · ${share}% of RAM`));
}

function renderHostProcesses(snap) {
  const body = $("host-procs");
  clear(body);
  const rows = el("div", "rows");
  for (const proc of (snap.processes || []).slice(0, 14)) {
    const row = el("div", "row" + (proc.role ? " ok" : ""));
    row.appendChild(el("span", "ts", String(proc.pid)));
    const name = el("span", "kind", proc.name);
    name.title = proc.command;
    row.appendChild(name);
    row.appendChild(el("span", "body", proc.role
      ? `${proc.role} — ${proc.role_note}` : ""));
    row.appendChild(el("span", "num", gb(proc.rss)));
    row.appendChild(el("span", "num", proc.cpu.toFixed(1) + "%"));
    rows.appendChild(row);
  }
  body.appendChild(rows);
}

function renderWorkload(data) {
  const body = $("workload-body");
  clear(body);
  if (!data.models.length) { empty(body, "no model calls in the window"); return; }
  const rows = el("div", "rows");
  for (const model of data.models) {
    const row = el("div", "row");
    row.appendChild(el("span", "kind", model.model));
    const gaugeCell = el("span", "body");
    const bar = el("div", "bar");
    const fill = el("span", model.ms_share >= 50 ? "warn" : "");
    fill.style.width = model.ms_share + "%";
    bar.appendChild(fill);
    gaugeCell.appendChild(bar);
    gaugeCell.title = `${model.calls} calls · avg ${model.avg_ms}ms`;
    row.appendChild(gaugeCell);
    row.appendChild(el("span", "num", model.ms_share + "%"));
    row.appendChild(el("span", "num", (model.ms / 1000).toFixed(0) + "s"));
    row.appendChild(el("span", "num", model.avg_ms + "ms"));
    row.appendChild(el("span", "num", "$" + model.cost_usd.toFixed(4)));
    rows.appendChild(row);
  }
  body.appendChild(rows);
  verdictInto("workload-note", `${data.hours}h · by wall time`);
}

/* Stacked areas of resident memory per role over time, with load per core
   drawn over it on its own scale. Stacked because the question is "what is
   the machine holding", and a stack answers it at a glance where four
   separate lines make you add them up yourself. */
function drawHostGraph() {
  const canvas = $("host-canvas");
  if (!canvas || currentView !== "host") return;
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const box = canvas.getBoundingClientRect();
  if (canvas.width !== Math.round(box.width * ratio)
      || canvas.height !== Math.round(box.height * ratio)) {
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
  }
  const styles = getComputedStyle(document.documentElement);
  const dim = styles.getPropertyValue("--dim").trim() || "#888";
  const accent = styles.getPropertyValue("--accent").trim() || "#ffb000";
  const warn = styles.getPropertyValue("--warn").trim() || "#e0a458";

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const points = hostState.history;
  if (points.length < 2) {
    ctx.fillStyle = dim;
    ctx.font = `${12 * ratio}px ui-monospace, monospace`;
    ctx.fillText("collecting — one sample every 5s", 12 * ratio, 24 * ratio);
    hostState.raf = requestAnimationFrame(drawHostGraph);
    return;
  }

  const pad = { l: 8 * ratio, r: 8 * ratio, t: 10 * ratio, b: 16 * ratio };
  const w = canvas.width - pad.l - pad.r;
  const h = canvas.height - pad.t - pad.b;
  const roles = [...new Set(points.flatMap((p) => Object.keys(p.roles || {})))];
  const peak = Math.max(1, ...points.map(
    (p) => Object.values(p.roles || {}).reduce((a, b) => a + b, 0)));

  const x = (i) => pad.l + (i / (points.length - 1)) * w;
  const y = (v) => pad.t + h - (v / peak) * h;

  const hues = tubeVariants(roles.length);
  roles.forEach((role, index) => {
    // Stack from the bottom: each band sits on the sum of the ones below.
    const below = roles.slice(0, index);
    ctx.beginPath();
    points.forEach((point, i) => {
      const base = below.reduce((sum, r) => sum + (point.roles[r] || 0), 0);
      const top = base + (point.roles[role] || 0);
      if (i === 0) ctx.moveTo(x(i), y(top)); else ctx.lineTo(x(i), y(top));
    });
    for (let i = points.length - 1; i >= 0; i--) {
      const base = below.reduce((sum, r) => sum + (points[i].roles[r] || 0), 0);
      ctx.lineTo(x(i), y(base));
    }
    ctx.closePath();
    const hsl = hues[index];
    ctx.fillStyle = `hsla(${hsl.h}, ${hsl.s}%, ${hsl.l}%, 0.42)`;
    ctx.fill();
    ctx.strokeStyle = `hsl(${hsl.h}, ${hsl.s}%, ${hsl.l}%)`;
    ctx.lineWidth = ratio;
    ctx.stroke();
  });

  // Load per core, on its own 0..2 scale, dashed so it never reads as
  // another memory band.
  ctx.beginPath();
  points.forEach((point, i) => {
    const value = Math.min(2, point.load || 0) / 2;
    const py = pad.t + h - value * h;
    if (i === 0) ctx.moveTo(x(i), py); else ctx.lineTo(x(i), py);
  });
  ctx.setLineDash([4 * ratio, 4 * ratio]);
  ctx.strokeStyle = warn;
  ctx.lineWidth = 1.4 * ratio;
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.font = `${10 * ratio}px ui-monospace, monospace`;
  ctx.fillStyle = dim;
  ctx.fillText(gb(peak), pad.l, pad.t + 9 * ratio);
  ctx.fillText(`${points.length * 5}s window · dashed = load/core (0-2)`,
               pad.l, canvas.height - 4 * ratio);

  let legendX = pad.l;
  roles.forEach((role, index) => {
    const hsl = hues[index];
    ctx.fillStyle = `hsl(${hsl.h}, ${hsl.s}%, ${hsl.l}%)`;
    ctx.fillRect(legendX, pad.t + 16 * ratio, 7 * ratio, 7 * ratio);
    ctx.fillStyle = dim;
    ctx.fillText(role, legendX + 11 * ratio, pad.t + 23 * ratio);
    legendX += ctx.measureText(role).width + 26 * ratio;
  });
  void accent;
  hostState.raf = requestAnimationFrame(drawHostGraph);
}

async function refreshHost() {
  try {
    const [snap, history, load] = await Promise.all([
      api("/api/host"), api("/api/host/history"), api("/api/workload?hours=24"),
    ]);
    hostState.snapshot = snap;
    hostState.history = history.points || [];
    renderHostGauges(snap);
    renderHostRoles(snap);
    renderHostProcesses(snap);
    renderWorkload(load);
    verdictInto("host-graph-note", hostState.history.length < 2
      ? "collecting…" : `${hostState.history.length} samples`);
  } catch (err) {
    empty($("host-gauges"), "host unavailable: " + err.message);
  }
}

function loadHost() {
  refreshHost();
  if (hostState.raf) cancelAnimationFrame(hostState.raf);
  drawHostGraph();
  return Promise.resolve();
}

/* --------------------------------------------------------------- actions */
/* Sector 08 — what Kyraan has actually DONE. The other sectors answer what
   it knows, what is scheduled, what it can do and what it cost; this is
   the one that answers what it changed in the calendar, the reminders and
   the memory, which is the question an owner-reviewed system exists to
   answer.

   `undoable` is a STATE here, not a button. Undoing is a write and goes
   through the kernel in Phase C — rule 1. */

const actionsState = { toolFilter: new Set() };

function actionSummary(action) {
  const args = Object.entries(action.args || {})
    .filter(([k]) => k !== "chat_id")
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" ");
  return args || "—";
}

function renderActions(data) {
  const rows = $("actions-rows");
  clear(rows);

  const tools = $("actions-tools");
  clear(tools);
  for (const entry of data.by_tool) {
    const chip = el("span", "chip-tool"
      + (actionsState.toolFilter.size && !actionsState.toolFilter.has(entry.tool)
         ? " off" : ""));
    chip.appendChild(el("span", null, `${entry.tool} ${entry.count}`));
    chip.addEventListener("click", () => {
      if (actionsState.toolFilter.has(entry.tool)) actionsState.toolFilter.delete(entry.tool);
      else actionsState.toolFilter.add(entry.tool);
      renderActions(data);
      syncUrl(false);
    });
    tools.appendChild(chip);
  }

  const shown = data.actions.filter(
    (a) => !actionsState.toolFilter.size || actionsState.toolFilter.has(a.tool));
  if (!shown.length) { empty(rows, "no actions in this window"); }
  else {
    const list = el("div", "rows");
    for (const action of shown) {
      const state = action.undone ? "undone"
                  : action.undoable ? "undoable" : "irreversible";
      const row = el("div", "row " + (action.undone ? "undone" : ""));
      row.appendChild(el("span", "ts", hhmmss(action.at)));
      row.appendChild(el("span", "kind", action.tool));
      row.appendChild(el("span", "body", actionSummary(action)));
      const mark = el("span", "undo-state " + state, state);
      // The inverse is the interesting part of an undoable action: it says
      // exactly what reversing it would run.
      mark.title = action.undo_tool
        ? `inverse: ${action.undo_tool}` : "no declared inverse";
      row.appendChild(mark);
      list.appendChild(row);
    }
    rows.appendChild(list);
  }

  $("actions-summary").textContent =
    `${data.undoable} undoable · ${data.irreversible} irreversible · `
    + `${data.undone} undone`
    + (data.degraded ? ` · ${data.degraded}` : "");
  verdictInto("actions-note",
    `${shown.length} of ${data.total} · ${data.days}d`);
}

async function loadActions() {
  const rows = $("actions-rows");
  try {
    const data = await api("/api/actions?limit=400&days="
      + encodeURIComponent($("actions-days").value));
    actionsState.last = data;
    renderActions(data);
  } catch (err) {
    empty(rows, "could not load actions: " + err.message);
  }
}

/* ---------------------------------------------------------------- health */

async function loadHealth(force) {
  const body = $("health-body");
  body.textContent = "probing…";
  try {
    const data = await api("/api/health" + (force ? "?force=1" : ""));
    body.textContent = data.text;
    $("health-note").textContent =
      `${data.verdict}${data.cached ? " (cached)" : ""} — checked ${hhmmss(data.checked_at)}`;
  } catch (err) {
    body.textContent = "health probe failed: " + err.message;
  }
}

/* ------------------------------------------------------------------ wire */

/* --------------------------------------------------------------- routes */
/* Sectors are real URLs, and each sector's meaningful state rides in the
   query string. Without this a reload — or a bookmark, or a link to a
   turn you want someone to look at — dropped the reader back on the
   overview with every filter reset.

   Route names are the RAIL's names, not the internal view ids: /schedule
   reads better than /triggers and is what the reader sees on screen. */
const ROUTES = {
  overview: "overview", stream: "stream", turns: "turns",
  schedule: "triggers", spend: "cost", systems: "health", brain: "memory",
  host: "host", actions: "actions",
};
const VIEW_TO_ROUTE = Object.fromEntries(
  Object.entries(ROUTES).map(([route, view]) => [view, route]));

let restoring = false;   // suppress URL writes while applying one
let openTurnId = null;   // the forensics panel's subject, so /turns?turn=… survives a reload

/* A tool filter set from the brain: pick skills there, then ask to see
   them firing (sector 01) or the turns that used them (sector 02). The
   selection was previously a dead end — you could pick a group and do
   nothing with it. */
const toolFilter = { stream: [], turns: [] };

function renderToolChips(view) {
  const host = $(view === "stream" ? "stream-tools" : "turns-tools");
  if (!host) return;
  clear(host);
  for (const tool of toolFilter[view]) {
    const chip = el("span", "chip-tool");
    chip.appendChild(el("span", null, tool));
    const drop = el("button", null, "\u00d7");
    drop.title = "remove";
    drop.addEventListener("click", () => {
      toolFilter[view] = toolFilter[view].filter((t) => t !== tool);
      renderToolChips(view);
      syncUrl(false);
      (view === "stream" ? renderStream() : loadTurns());
    });
    chip.appendChild(drop);
    host.appendChild(chip);
  }
}

/* Hand a set of tools to another sector and go there. */
function focusToolsIn(view, tools) {
  toolFilter[view] = [...tools];
  loaded.delete(view);          // force the loader to re-run with the filter
  showView(view);
  renderToolChips(view);
}

/* What each sector wants remembered. Kept small on purpose: enough that a
   reload is invisible, not so much that the URL becomes a session dump. */
function collectState(view) {
  const params = new URLSearchParams();
  if (view === "stream") {
    const q = $("stream-q").value.trim();
    if (q) params.set("q", q);
    if ($("stream-anomalies").checked) params.set("anomalies", "1");
    if (!$("stream-live").checked) params.set("live", "0");
    if (toolFilter.stream.length) params.set("tools", toolFilter.stream.join(","));
  } else if (view === "turns") {
    params.set("sort", $("turns-sort").value);
    params.set("hours", $("turns-hours").value);
    if (toolFilter.turns.length) params.set("tools", toolFilter.turns.join(","));
    if (openTurnId) params.set("turn", openTurnId);
  } else if (view === "actions") {
    params.set("days", $("actions-days").value);
    if (actionsState.toolFilter.size) {
      params.set("tools", [...actionsState.toolFilter].join(","));
    }
  } else if (view === "cost") {
    params.set("days", $("cost-days").value);
  } else if (view === "memory") {
    params.set("colour", brain.colour);
    if (brain.since) params.set("since", String(Math.round(brain.since / 86400000)));
    if (brain.query) params.set("q", brain.query);
    if (!brainCam.spin) params.set("spin", "0");
    if (brain.dragMode !== "orbit") params.set("drag", brain.dragMode);
    if (brain.sideHidden) params.set("side", "0");
    if (brain.topHidden) params.set("top", "0");
    // On a phone the folded state is the default, so it is the OPEN state
    // that must be named or a reload folds the panel again.
    if (PHONE.matches && !brain.sideHidden) params.set("side", "1");
    if (PHONE.matches && !brain.topHidden) params.set("top", "1");
    if (brain.floor !== 0.45) params.set("floor", brain.floor.toFixed(2));
    const lobes = Object.entries(brain.showType)
      .filter(([, on]) => on).map(([type]) => type);
    // "< 4" was the lobe count when this was written. With seven lobes,
    // hiding one left six — never fewer than four — so hiding recall,
    // docs or faces changed the view and was never written to the URL.
    if (lobes.length < Object.keys(brain.showType).length) {
      params.set("lobes", lobes.join(","));
    }
    const off = Object.entries(brain.showEdge)
      .filter(([, on]) => !on).map(([kind]) => kind);
    if (off.length) params.set("hide", off.join(","));
    if (brain.selection.size && brain.selection.size <= 25) {
      params.set("sel", [...brain.selection].join(","));
    }
  }
  return params;
}

function syncUrl(push) {
  if (restoring) return;
  const route = VIEW_TO_ROUTE[currentView] || "overview";
  const params = collectState(currentView);
  const query = params.toString();
  const url = "/" + route + (query ? "?" + query : "");
  // replaceState for tweaks, pushState for sector changes: otherwise every
  // keystroke in the stream filter becomes its own back-button step.
  if (push) history.pushState({}, "", url);
  else history.replaceState({}, "", url);
}

function applyState(view, params) {
  if (view === "stream") {
    toolFilter.stream = (params.get("tools") || "").split(",").filter(Boolean);
    $("stream-q").value = params.get("q") || "";
    $("stream-anomalies").checked = params.get("anomalies") === "1";
    $("stream-live").checked = params.get("live") !== "0";
  } else if (view === "turns") {
    toolFilter.turns = (params.get("tools") || "").split(",").filter(Boolean);
    if (params.get("sort")) $("turns-sort").value = params.get("sort");
    if (params.get("hours")) $("turns-hours").value = params.get("hours");
  } else if (view === "actions") {
    if (params.get("days")) $("actions-days").value = params.get("days");
    actionsState.toolFilter = new Set(
      (params.get("tools") || "").split(",").filter(Boolean));
  } else if (view === "cost") {
    if (params.get("days")) $("cost-days").value = params.get("days");
  } else if (view === "memory") {
    if (params.get("floor")) {
      brain.floor = parseFloat(params.get("floor"));
      const slider = $("mem-floor");
      if (slider) { slider.value = brain.floor; $("mem-floor-value").textContent = brain.floor.toFixed(2); }
    }
    // The query text can be restored now, but MATCHING it cannot: the
    // nodes do not exist until the fetch resolves. Running it here set a
    // query with zero hits and dimmed the entire graph.
    const query = params.get("q");
    if (query) {
      const box = $("mem-search");
      if (box) box.value = query;
    }
    if (params.get("top") === "0") {
      brain.topHidden = true;
      $("view-memory").classList.add("top-hidden");
      const t = $("top-toggle");
      if (t) t.textContent = "controls \u25be";
    }
    if (params.get("side") === "0") {
      brain.sideHidden = true;
      $("view-memory").classList.add("side-hidden");
      const toggle = $("side-toggle");
      if (toggle) toggle.textContent = "\u25c2 panels";
    }
    // A phone has no room for a controls row and a column of consoles
    // beside the canvas: both start folded there, and the two header
    // toggles bring them back. A URL that names the state wins.
    if (PHONE.matches) {
      if (!params.has("top") && !brain.topHidden) {
        brain.topHidden = true;
        $("view-memory").classList.add("top-hidden");
        const t = $("top-toggle");
        if (t) t.textContent = "controls \u25be";
      }
      if (!params.has("side") && !brain.sideHidden) {
        brain.sideHidden = true;
        $("view-memory").classList.add("side-hidden");
        const toggle = $("side-toggle");
        if (toggle) toggle.textContent = "\u25c2 panels";
      }
    }
    if (params.get("drag") === "pan") {
      brain.dragMode = "pan";
      const sel = $("mem-drag");
      if (sel) sel.value = "pan";
    }
    if (params.get("spin") === "0") {
      brainCam.spin = false;
      const box = $("mem-spin");
      if (box) box.checked = false;
    }
    if (params.get("colour")) brain.colour = params.get("colour");
    if (params.get("since")) setSince(Number(params.get("since")) || 0);
    const colourSelect = $("mem-colour");
    if (colourSelect) colourSelect.value = brain.colour;
    const lobes = params.get("lobes");
    if (lobes !== null) {
      const on = new Set(lobes.split(",").filter(Boolean));
      for (const type of Object.keys(brain.showType)) {
        brain.showType[type] = on.has(type);
        const box = $("show-" + type);
        if (box) box.checked = brain.showType[type];
      }
    }
    const hide = params.get("hide");
    if (hide !== null) {
      const off = new Set(hide.split(",").filter(Boolean));
      for (const kind of Object.keys(brain.showEdge)) {
        brain.showEdge[kind] = !off.has(kind);
      }
      for (const [id, kind] of [["edge-synapse", "synapse"],
                                ["edge-relation", "relation"],
                                ["edge-coactivation", "coactivation"]]) {
        const box = $(id);
        if (box) box.checked = brain.showEdge[kind];
      }
      const structure = $("edge-structure");
      if (structure) structure.checked = brain.showEdge.subject;
    }
  }
}

/* Applied AFTER the sector's loader has fetched — a selection or an open
   turn refers to data that does not exist until then. */
function applyLoadedState(view, params) {
  if (view === "turns" && params.get("turn")) {
    showTurn(params.get("turn"));
  } else if (view === "memory") {
    if (params.get("q")) runSearch(params.get("q"));
    if (params.get("sel")) {
      const ids = params.get("sel").split(",").filter((id) => brain.byId.has(id));
      brain.selection = new Set(ids);
      renderSelection();
    }
  }
}

function routeFromLocation() {
  const segment = location.pathname.replace(/^\/+|\/+$/g, "").split("/")[0];
  return ROUTES[segment] ? segment : "overview";
}

function navigate() {
  const route = routeFromLocation();
  const params = new URLSearchParams(location.search);
  const view = ROUTES[route];
  restoring = true;
  applyState(view, params);
  showView(view, { fromUrl: true, params });
  restoring = false;
  refreshPickSummaries();
}

const LOADERS = {
  overview: loadOverview,
  stream: loadStream, turns: loadTurns, triggers: loadTriggers,
  cost: loadCost, health: () => loadHealth(false), memory: loadMemory,
  host: loadHost, actions: loadActions,
};
const loaded = new Set();
let currentView = "overview";

function showView(name, options = {}) {
  currentView = name;
  for (const sector of document.querySelectorAll(".sector")) {
    sector.classList.toggle("on", sector.dataset.view === name);
  }
  for (const view of document.querySelectorAll(".view")) {
    view.classList.toggle("on", view.id === "view-" + name);
  }
  if (!loaded.has(name)) {
    loaded.add(name);
    const done = LOADERS[name]();
    if (options.params) {
      Promise.resolve(done).then(() => applyLoadedState(name, options.params));
    }
  } else if (options.params) {
    applyLoadedState(name, options.params);
  }
  if (!options.fromUrl) syncUrl(true);
  // A requestAnimationFrame loop behind a hidden section is pure battery
  // burn — the deck is meant to be left open for hours.
  if (name === "memory") restartBrain();
  else if (brain.raf) { cancelAnimationFrame(brain.raf); brain.raf = null; }
  if (name === "overview") { drawHub(); }
  if (turnCard.turnId && $("turn-card")) renderTurnCard();
  else if (hub.raf) { cancelAnimationFrame(hub.raf); hub.raf = null; }
  if (name === "host") { drawHostGraph(); }
  else if (hostState.raf) { cancelAnimationFrame(hostState.raf); hostState.raf = null; }
}

for (const sector of document.querySelectorAll(".sector")) {
  sector.addEventListener("click", () => showView(sector.dataset.view));
}
window.addEventListener("popstate", navigate);

// Every control that changes what you are looking at writes the URL.
for (const id of ["stream-q", "stream-anomalies", "stream-live", "turns-sort",
                  "turns-hours", "cost-days", "mem-colour", "mem-since", "show-memory",
                  "show-person", "show-task", "show-skill",
                  // Added with the recall/docs/faces lobes — but not here, so
                  // hiding any of the three changed the view and never the
                  // URL. Found by toggling one inside the new picker.
                  "show-episode", "show-document", "show-face", "show-contact", "show-place", "show-care",
                  "show-note", "show-tag", "edge-synapse",
                  "edge-relation", "edge-coactivation", "edge-structure",
                  "actions-days"]) {
  const node = $(id);
  // Deferred a tick: this listener is registered before each control's
  // own state listener, so a synchronous write here read the PREVIOUS
  // state — hiding a lobe wrote nothing, restoring it wrote "hidden". One
  // step late, every time, for every control in this list.
  if (node) node.addEventListener("change", () => setTimeout(() => syncUrl(false), 0));
}
$("stream-q").addEventListener("input", () => setTimeout(() => syncUrl(false), 0));
$("stream-q").addEventListener("input", renderStream);
$("stream-anomalies").addEventListener("change", renderStream);
$("stream-live").addEventListener("change", connectStream);
$("stream-clear").addEventListener("click", () => { stream.rows = []; renderStream(); });
$("turns-sort").addEventListener("change", loadTurns);
$("turns-hours").addEventListener("change", loadTurns);
$("turns-refresh").addEventListener("click", loadTurns);
$("triggers-refresh").addEventListener("click", loadTriggers);
$("cost-days").addEventListener("change", loadCost);
$("actions-days").addEventListener("change", loadActions);
$("actions-refresh").addEventListener("click", loadActions);
$("cost-refresh").addEventListener("click", loadCost);
$("health-refresh").addEventListener("click", () => loadHealth(true));

initPhosphor();

/* Phone: the status bar tucks itself away (owner, 2026-09-03: "top panel
   on mobile should be auto hidden"). It shows for a few seconds on load
   and whenever the handle is tapped, then folds up and gives the brain
   its 60px. The readouts keep updating underneath; nothing is lost. */
(function tuckTop() {
  const handle = $("top-handle");
  if (!handle || !PHONE.matches) return;
  const IDLE_MS = 5000;
  let timer = null;
  const tuck = () => { document.body.classList.add("top-tucked"); timer = null; };
  const show = () => {
    document.body.classList.remove("top-tucked");
    clearTimeout(timer);
    timer = setTimeout(tuck, IDLE_MS);
  };
  handle.addEventListener("click", () => {
    if (document.body.classList.contains("top-tucked")) show(); else tuck();
  });
  document.querySelector("header").addEventListener("click", show);
  show();
})();
wirePickers();
wireMemory();
wireHub();
refreshStatus();
setInterval(refreshStatus, 10000);
// The deck's slower consoles refresh on their own clock — schedule and
// spend move in minutes, not seconds, and re-probing systems is costly.
setInterval(() => { if (currentView === "overview") refreshDeck(); }, 60000);
// The host view is a monitor: it refreshes on its own while it is open.
setInterval(() => { if (currentView === "host") refreshHost(); }, 5000);
navigate();
