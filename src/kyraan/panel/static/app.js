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
const EXPECTED_API = 7;

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function empty(node, message) {
  clear(node);
  node.appendChild(el("div", "empty", message));
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
    case "tool_call":
      return `${event.tool || event.skill || "?"} ${JSON.stringify(event.args || {})}`;
    case "tool_result":
      return `${event.tool || event.skill || "?"} ${event.ok ? "ok" : "FAILED: " + (event.error || "")}` +
             (event.duration_ms != null ? ` ${event.duration_ms}ms` : "");
    case "model_call":
      return `${event.provider || "?"}/${event.model || "?"} in=${event.input_tokens || 0} ` +
             `out=${event.output_tokens || 0} cached=${event.cached_tokens || 0} ` +
             `$${(event.cost_usd || 0).toFixed(5)}`;
    case "turn_health":
      return event.anomaly_count ? `${event.anomaly_count} anomalies` : "clean";
    default: {
      const rest = {};
      for (const [k, v] of Object.entries(event)) if (!skip.has(k)) rest[k] = v;
      return JSON.stringify(rest);
    }
  }
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

/* -- schedule console -- */

function renderScheduleConsole(data) {
  const body = $("schedule-body");
  clear(body);
  const rows = el("div", "rows");
  const shown = data.triggers.slice(0, 5);
  if (!shown.length) {
    empty(body, "nothing scheduled");
  } else {
    for (const item of shown) {
      const fire = item.fire || {};
      const row = el("div", "row" + (fire.overdue ? " bad" : ""));
      row.appendChild(el("span", "ts", relative(fire.in_seconds)));
      row.appendChild(el("span", "body", item.text));
      row.appendChild(el("span", "tag", item.type));
      rows.appendChild(row);
    }
    body.appendChild(rows);
  }
  verdictInto("schedule-verdict",
    data.overdue ? `${data.overdue} overdue` : `${data.triggers.length} queued`,
    data.overdue ? "bad" : "ok");
}

/* -- top consumers console -- */

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
    ["/api/triggers", renderScheduleConsole, "schedule-body", "schedule-verdict"],
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
  await Promise.all([refreshDeck(), loadStream()]);
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
const LOBES = {
  memory: { anchor: [-0.95, 0.10], label: "memory" },
  person: { anchor: [0.0, -0.10], label: "people" },
  task:   { anchor: [0.55, -0.75], label: "work" },
  skill:  { anchor: [0.80, 0.45], label: "skills" },
};

const EDGE_STYLE = {
  synapse:     { alpha: 0.34, width: 1.0, rest: 90,  key: "--accent" },
  subject:     { alpha: 0.16, width: 0.8, rest: 120, key: "--dim" },
  relation:    { alpha: 0.7,  width: 1.6, rest: 100, key: "--ok" },
  owns:        { alpha: 0.4,  width: 1.1, rest: 150, key: "--dim" },
  managed_by:  { alpha: 0.22, width: 0.9, rest: 210, key: "--dim" },
  coactivation:{ alpha: 0.4,  width: 1.2, rest: 80,  key: "--accent" },
};

const brain = {
  nodes: [], edges: [], byId: new Map(),
  colour: "lobe",
  showType: { memory: true, person: true, task: true, skill: true },
  showEdge: Object.fromEntries(Object.keys(EDGE_STYLE).map((k) => [k, true])),
  view: { x: 0, y: 0, scale: 1 },
  pan: null, nodeDrag: null, band: null,
  hover: null, selection: new Set(),
  alpha: 1, raf: null, palette: new Map(), review: null, census: null,
  fired: new Map(),        // node id -> performance.now() of its last firing
  floor: 0.45,             // synapse threshold; a control, not a constant
  findings: { orphans: [], dead: [], contested: [] },
};

// How long a neuron stays lit after it fires. Long enough to catch out of
// the corner of your eye, short enough that a busy turn does not leave the
// whole skill lobe permanently on.
const FIRE_MS = 2600;

/* Live activation. The SSE tail already carries every tool call, so the
   brain can show what is firing RIGHT NOW rather than only what exists —
   the difference between an anatomy diagram and an EEG. Fed from the same
   one connection the stream sector uses; costs nothing extra. */
function brainActivate(event) {
  let id = null;
  if (event.kind === "tool_call" || event.kind === "agent_tool_call") {
    if (event.tool) id = "s:" + event.tool;
  } else if (event.reminder_id) {
    id = "t:reminder:" + event.reminder_id;
  }
  if (id && brain.byId.has(id)) brain.fired.set(id, performance.now());
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

function groupKey(node) {
  if (brain.colour === "lobe") return node.type;
  if (brain.colour === "group") return node.group || node.type;
  return node[brain.colour] || node.type;
}

function buildPalette() {
  const keys = [...new Set(brain.nodes.map(groupKey))].sort();
  const base = hslFromAccent();
  brain.palette = new Map(keys.map((key, i) => {
    const spread = keys.length > 1 ? (i / (keys.length - 1) - 0.5) : 0;
    return [key, { h: (base.h + spread * 82 + 360) % 360,
                   s: Math.round(base.s * 100),
                   l: Math.round(48 + spread * 15) }];
  }));
}

function nodeColour(node, alpha) {
  const hsl = brain.palette.get(groupKey(node)) || { h: 40, s: 80, l: 50 };
  // Superseded memories stay in the brain — they are part of what it
  // holds — but they no longer speak, so they are dimmed.
  const lightness = node.active === false ? Math.round(hsl.l * 0.45) : hsl.l;
  return `hsla(${hsl.h}, ${hsl.s}%, ${lightness}%, ${alpha})`;
}

function nodeRadius(node) {
  if (node.type === "person") return 7.5;
  if (node.type === "task") return 6.5;
  if (node.type === "skill") {
    // Log scale: home.get_state ran 1236 times and calendar.list_events 69.
    // Linear would make one node the size of the lobe.
    return 3.4 + Math.log10((node.uses || 0) + 1) * 1.9;
  }
  return node.importance === "high" ? 5.4 : 4.0;
}

/* -- layout ------------------------------------------------------------ */

function visibleNodes() {
  return brain.nodes.filter((n) => brain.showType[n.type]);
}

function seedPositions() {
  const spread = 0.34;
  brain.nodes.forEach((node, i) => {
    const anchor = (LOBES[node.type] || LOBES.memory).anchor;
    // Deterministic jitter: the same brain comes back the same way, so
    // the layout is somewhere you can learn rather than a new scatter.
    const angle = (i * 137.508) * Math.PI / 180;      // golden angle
    const radius = spread * Math.sqrt((i % 40) / 40);
    node.x = anchor[0] + Math.cos(angle) * radius;
    node.y = anchor[1] + Math.sin(angle) * radius;
    node.vx = 0; node.vy = 0; node.pinned = false;
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
  const REPEL = 0.00026, SPRING = 0.012, ANCHOR = 0.055, DAMP = 0.86;

  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1e-6) { dx = (i - j) * 1e-3; dy = 1e-3; d2 = 1e-6; }
      const force = Math.min(REPEL / d2, 0.06);
      const d = Math.sqrt(d2);
      a.vx += (dx / d) * force; a.vy += (dy / d) * force;
      b.vx -= (dx / d) * force; b.vy -= (dy / d) * force;
    }
  }

  for (const edge of brain.edges) {
    if (!brain.showEdge[edge.kind]) continue;
    const a = brain.byId.get(edge.a), b = brain.byId.get(edge.b);
    if (!a || !b || !brain.showType[a.type] || !brain.showType[b.type]) continue;
    const style = EDGE_STYLE[edge.kind] || EDGE_STYLE.synapse;
    const rest = style.rest / 900;
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.hypot(dx, dy) || 1e-6;
    const pull = (d - rest) * SPRING * Math.min(1.4, 0.35 + (edge.weight || 0.5));
    a.vx += (dx / d) * pull; a.vy += (dy / d) * pull;
    b.vx -= (dx / d) * pull; b.vy -= (dy / d) * pull;
  }

  for (const node of nodes) {
    const anchor = (LOBES[node.type] || LOBES.memory).anchor;
    node.vx += (anchor[0] - node.x) * ANCHOR;
    node.vy += (anchor[1] - node.y) * ANCHOR;
    if (node.pinned) { node.vx = 0; node.vy = 0; continue; }
    node.vx *= DAMP; node.vy *= DAMP;
    node.x += node.vx * brain.alpha;
    node.y += node.vy * brain.alpha;
  }
  brain.alpha = Math.max(0.02, brain.alpha * 0.99);
}

function reheat(to = 1) { brain.alpha = to; }

/* -- projection to screen ---------------------------------------------- */

function toScreen(canvas, node) {
  const size = Math.min(canvas.width, canvas.height) * 0.42;
  return {
    x: canvas.width / 2 + (node.x * size + brain.view.x) * brain.view.scale,
    y: canvas.height / 2 + (node.y * size + brain.view.y) * brain.view.scale,
  };
}

function fromScreen(canvas, sx, sy) {
  const size = Math.min(canvas.width, canvas.height) * 0.42;
  return {
    x: ((sx - canvas.width / 2) / brain.view.scale - brain.view.x) / size,
    y: ((sy - canvas.height / 2) / brain.view.scale - brain.view.y) / size,
  };
}

function canvasPoint(canvas, event) {
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  return { x: (event.clientX - box.left) * ratio,
           y: (event.clientY - box.top) * ratio };
}

/* -- draw -------------------------------------------------------------- */

function drawBrain() {
  const canvas = $("mem-canvas");
  if (!canvas || currentView !== "memory") return;
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const box = canvas.getBoundingClientRect();
  if (canvas.width !== Math.round(box.width * ratio)) {
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
  }

  simulate();

  const styles = getComputedStyle(document.documentElement);
  const dim = styles.getPropertyValue("--dim").trim() || "#888";
  const bad = styles.getPropertyValue("--bad").trim() || "#f55";
  const text = styles.getPropertyValue("--text").trim() || "#ddd";
  const panel = styles.getPropertyValue("--panel").trim() || "#111";
  const nodes = visibleNodes();
  const shown = new Set(nodes.map((n) => n.id));

  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Lobe halos — the brain has regions, and they should read as regions.
  {
    for (const [type, lobe] of Object.entries(LOBES)) {
      const members = nodes.filter((n) => n.type === type);
      if (members.length < 2) continue;
      const points = members.map((n) => toScreen(canvas, n));
      const cx = points.reduce((s, p) => s + p.x, 0) / points.length;
      const cy = points.reduce((s, p) => s + p.y, 0) / points.length;
      const spread = Math.max(...points.map((p) => Math.hypot(p.x - cx, p.y - cy)))
                   + 30 * ratio;
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, spread);
      glow.addColorStop(0, nodeColour(members[0], 0.1));
      glow.addColorStop(1, nodeColour(members[0], 0));
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(cx, cy, spread, 0, Math.PI * 2); ctx.fill();
      ctx.font = `${11 * ratio}px ui-monospace, monospace`;
      ctx.fillStyle = dim; ctx.globalAlpha = 0.8;
      const caption = lobe.label.toUpperCase() + "  " + members.length;
      const captionWidth = ctx.measureText(caption).width;
      // Clamped into the canvas: a lobe drifting off the top edge took
      // its own label with it and the regions went unnamed.
      const lx = Math.min(Math.max(cx - captionWidth / 2, 8 * ratio),
                          canvas.width - captionWidth - 8 * ratio);
      const ly = Math.min(Math.max(cy - spread - 8 * ratio, 16 * ratio),
                          canvas.height - 8 * ratio);
      ctx.fillText(caption, lx, ly);
      ctx.globalAlpha = 1;
    }
  }

  // Axons. A selected node's edges are drawn bright so "what is this
  // wired to" is answered by clicking rather than by squinting.
  for (const edge of brain.edges) {
    if (!brain.showEdge[edge.kind]) continue;
    if (!shown.has(edge.a) || !shown.has(edge.b)) continue;
    const a = brain.byId.get(edge.a), b = brain.byId.get(edge.b);
    const style = EDGE_STYLE[edge.kind] || EDGE_STYLE.synapse;
    const touched = brain.selection.has(edge.a) || brain.selection.has(edge.b)
                 || (brain.hover && (brain.hover.id === edge.a || brain.hover.id === edge.b));
    const pa = toScreen(canvas, a), pb = toScreen(canvas, b);
    ctx.strokeStyle = edge.contested ? bad
      : (styles.getPropertyValue(style.key).trim() || dim);
    ctx.globalAlpha = touched ? Math.min(1, style.alpha * 2.4) : style.alpha;
    ctx.lineWidth = (touched ? style.width * 1.8 : style.width) * ratio;
    ctx.beginPath();
    ctx.moveTo(pa.x, pa.y);
    // A slight bow keeps parallel edges between the same regions apart.
    ctx.quadraticCurveTo((pa.x + pb.x) / 2 - (pb.y - pa.y) * 0.12,
                         (pa.y + pb.y) / 2 + (pb.x - pa.x) * 0.12, pb.x, pb.y);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // Somas.
  for (const node of nodes) {
    const p = toScreen(canvas, node);
    const selected = brain.selection.has(node.id);
    const seed = node.id.charCodeAt(2) % 13;
    const breath = REDUCED_MOTION ? 1 : 1 + Math.sin(performance.now() / 1400 + seed) * 0.08;
    const radius = nodeRadius(node) * ratio * breath
                 * Math.max(0.6, Math.min(brain.view.scale, 2.4));

    const halo = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, radius * 3.6);
    halo.addColorStop(0, nodeColour(node, selected ? 0.75 : 0.42));
    halo.addColorStop(1, nodeColour(node, 0));
    ctx.fillStyle = halo;
    ctx.beginPath(); ctx.arc(p.x, p.y, radius * 3.6, 0, Math.PI * 2); ctx.fill();

    ctx.fillStyle = nodeColour(node, node.active === false ? 0.55 : 0.98);
    ctx.beginPath();
    if (node.type === "task") {
      // Work is square: shape carries type even when colour carries group.
      ctx.rect(p.x - radius, p.y - radius, radius * 2, radius * 2);
    } else if (node.type === "skill") {
      ctx.moveTo(p.x, p.y - radius);
      ctx.lineTo(p.x + radius, p.y);
      ctx.lineTo(p.x, p.y + radius);
      ctx.lineTo(p.x - radius, p.y);
      ctx.closePath();
    } else {
      ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
    }
    ctx.fill();

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
    if (brain.view.scale > 1.7 || node.type === "person"
        || (node.type === "skill" && (node.uses || 0) > 200)) {
      ctx.font = `${9.5 * ratio}px ui-monospace, monospace`;
      ctx.fillStyle = dim;
      ctx.fillText(node.label.slice(0, 26), p.x + radius + 4 * ratio, p.y + 3 * ratio);
    }
  }

  // Rubber band.
  if (brain.band) {
    const { x0, y0, x1, y1 } = brain.band;
    ctx.strokeStyle = text; ctx.globalAlpha = 0.8;
    ctx.setLineDash([5 * ratio, 4 * ratio]);
    ctx.lineWidth = ratio;
    ctx.strokeRect(Math.min(x0, x1), Math.min(y0, y1),
                   Math.abs(x1 - x0), Math.abs(y1 - y0));
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }

  if (brain.hover) {
    const p = toScreen(canvas, brain.hover);
    const label = brain.hover.label.slice(0, 76);
    ctx.font = `${12 * ratio}px ui-monospace, monospace`;
    const width = ctx.measureText(label).width + 14 * ratio;
    let bx = p.x + 12 * ratio;
    if (bx + width > canvas.width) bx = p.x - 12 * ratio - width;
    const by = p.y - 27 * ratio;
    ctx.fillStyle = panel; ctx.globalAlpha = 0.96;
    ctx.fillRect(bx, by, width, 20 * ratio);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = nodeColour(brain.hover, 0.85);
    ctx.lineWidth = ratio;
    ctx.strokeRect(bx, by, width, 20 * ratio);
    ctx.fillStyle = text;
    ctx.fillText(label, bx + 7 * ratio, by + 14 * ratio);
  }

  brain.raf = requestAnimationFrame(drawBrain);
}

/* -- picking ----------------------------------------------------------- */

function nodeAt(canvas, event) {
  const point = canvasPoint(canvas, event);
  const ratio = window.devicePixelRatio || 1;
  let best = null, bestDistance = 16 * ratio;
  for (const node of visibleNodes()) {
    const p = toScreen(canvas, node);
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
    const p = toScreen(canvas, node);
    return p.x >= left && p.x <= right && p.y >= top && p.y <= bottom;
  });
}

/* Fit everything currently shown. The force layout settles wherever the
   wiring puts it, which is not centred on the origin — without this the
   skill lobe simply walked off the bottom-right edge. Called after the
   simulation has had a moment to settle, not immediately, or it fits the
   seed positions instead of the result. */
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
function focusOn(canvas, nodes) {
  if (!nodes.length) return;
  const size = Math.min(canvas.width, canvas.height) * 0.42;
  const ratio = window.devicePixelRatio || 1;
  const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  brain.view.x = -((minX + maxX) / 2) * size;
  brain.view.y = -((minY + maxY) / 2) * size;
  // Solve the projection for scale rather than guessing a constant: the
  // renderer places a node at W/2 + (x*size + view.x)*scale, so the fit
  // is (half the canvas, less padding) over (half the span, in pixels).
  // A guessed constant is what left the whole graph as a dot in the
  // middle of an empty field.
  const pad = 56 * ratio;
  const halfX = Math.max(0.05, (maxX - minX) / 2) * size;
  const halfY = Math.max(0.05, (maxY - minY) / 2) * size;
  brain.view.scale = Math.max(0.3, Math.min(6,
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
  const focus = el("button", null, "zoom to its wiring");
  focus.addEventListener("click", () => focusOn($("mem-canvas"),
    [node, ...links.map((l) => l.other)]));
  body.appendChild(focus);
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
      api("/api/brain?floor=" + brain.floor.toFixed(2)),
      api("/api/memory/review"),
    ]);
    brain.nodes = graph.nodes;
    brain.edges = graph.edges;
    brain.byId = new Map(graph.nodes.map((n) => [n.id, n]));
    seedPositions();
    buildPalette();
    reheat(1);
    brain.selection.clear();
    renderGate(review);
    renderCensus(graph);
    renderFindings(graph);
    renderSelection();
    renderLegend();
    fitWhenSettled();
    note.textContent =
      `${graph.nodes.length} neurons · ${graph.edges.length} connections`
      + ` · mesh ${brain.floor.toFixed(2)}`
      + (graph.contested.length ? ` · ${graph.contested.length} contested` : "")
      + (graph.degraded ? ` · degraded: ${graph.degraded}` : "");
    restartBrain();
  } catch (err) {
    note.textContent = "could not load the brain: " + err.message;
  }
}

function wireMemory() {
  const canvas = $("mem-canvas");
  if (!canvas) return;

  canvas.addEventListener("mousedown", (event) => {
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
    } else {
      brain.pan = canvasPoint(canvas, event);
      canvas.classList.add("dragging");
    }
  });

  canvas.addEventListener("mousemove", (event) => {
    const point = canvasPoint(canvas, event);
    if (brain.nodeDrag) {
      const size = Math.min(canvas.width, canvas.height) * 0.42;
      const dx = (point.x - brain.nodeDrag.last.x) / brain.view.scale / size;
      const dy = (point.y - brain.nodeDrag.last.y) / brain.view.scale / size;
      for (const node of brain.nodeDrag.group) { node.x += dx; node.y += dy; }
      brain.nodeDrag.last = point;
      reheat(0.6);          // let the neighbours settle around the new place
      return;
    }
    if (brain.band) { brain.band.x1 = point.x; brain.band.y1 = point.y; return; }
    if (brain.pan) {
      brain.view.x += (point.x - brain.pan.x) / brain.view.scale;
      brain.view.y += (point.y - brain.pan.y) / brain.view.scale;
      brain.pan = point;
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
    canvas.classList.remove("dragging");
  });

  canvas.addEventListener("click", (event) => {
    const hit = nodeAt(canvas, event);
    if (!hit) { if (!event.shiftKey) brain.selection.clear(); }
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
    // Zoom into the cluster this neuron belongs to.
    const cluster = visibleNodes().filter((n) => groupKey(n) === groupKey(hit));
    brain.selection = new Set(cluster.map((n) => n.id));
    focusOn(canvas, cluster);
    renderSelection();
  });

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    brain.view.scale = Math.max(0.35, Math.min(6,
      brain.view.scale * Math.exp(-event.deltaY * 0.0012)));
  }, { passive: false });

  window.addEventListener("keydown", (event) => {
    if (currentView !== "memory") return;
    if (event.key === "Escape") { brain.selection.clear(); renderSelection(); }
  });

  $("mem-colour").addEventListener("change", (e) => {
    brain.colour = e.target.value;
    buildPalette(); renderLegend();
  });
  for (const [id, type] of [["show-memory", "memory"], ["show-person", "person"],
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
        for (const k of ["subject", "owns", "managed_by"]) brain.showEdge[k] = e.target.checked;
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
      loadMemory();
      syncUrl(false);
    });
  }
  $("mem-fit").addEventListener("click", fitAll);
  $("mem-reset").addEventListener("click", () => {
    brain.view = { x: 0, y: 0, scale: 1 };
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
  } else if (view === "cost") {
    params.set("days", $("cost-days").value);
  } else if (view === "memory") {
    params.set("colour", brain.colour);
    if (brain.floor !== 0.45) params.set("floor", brain.floor.toFixed(2));
    const lobes = Object.entries(brain.showType)
      .filter(([, on]) => on).map(([type]) => type);
    if (lobes.length < 4) params.set("lobes", lobes.join(","));
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
  } else if (view === "cost") {
    if (params.get("days")) $("cost-days").value = params.get("days");
  } else if (view === "memory") {
    if (params.get("floor")) {
      brain.floor = parseFloat(params.get("floor"));
      const slider = $("mem-floor");
      if (slider) { slider.value = brain.floor; $("mem-floor-value").textContent = brain.floor.toFixed(2); }
    }
    if (params.get("colour")) brain.colour = params.get("colour");
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
  } else if (view === "memory" && params.get("sel")) {
    const ids = params.get("sel").split(",").filter((id) => brain.byId.has(id));
    brain.selection = new Set(ids);
    renderSelection();
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
}

const LOADERS = {
  overview: loadOverview,
  stream: loadStream, turns: loadTurns, triggers: loadTriggers,
  cost: loadCost, health: () => loadHealth(false), memory: loadMemory,
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
}

for (const sector of document.querySelectorAll(".sector")) {
  sector.addEventListener("click", () => showView(sector.dataset.view));
}
window.addEventListener("popstate", navigate);

// Every control that changes what you are looking at writes the URL.
for (const id of ["stream-q", "stream-anomalies", "stream-live", "turns-sort",
                  "turns-hours", "cost-days", "mem-colour", "show-memory",
                  "show-person", "show-task", "show-skill", "edge-synapse",
                  "edge-relation", "edge-coactivation", "edge-structure"]) {
  const node = $(id);
  if (node) node.addEventListener("change", () => syncUrl(false));
}
$("stream-q").addEventListener("input", () => syncUrl(false));
$("stream-q").addEventListener("input", renderStream);
$("stream-anomalies").addEventListener("change", renderStream);
$("stream-live").addEventListener("change", connectStream);
$("stream-clear").addEventListener("click", () => { stream.rows = []; renderStream(); });
$("turns-sort").addEventListener("change", loadTurns);
$("turns-hours").addEventListener("change", loadTurns);
$("turns-refresh").addEventListener("click", loadTurns);
$("triggers-refresh").addEventListener("click", loadTriggers);
$("cost-days").addEventListener("change", loadCost);
$("cost-refresh").addEventListener("click", loadCost);
$("health-refresh").addEventListener("click", () => loadHealth(true));

initPhosphor();
wireMemory();
refreshStatus();
setInterval(refreshStatus, 10000);
// The deck's slower consoles refresh on their own clock — schedule and
// spend move in minutes, not seconds, and re-probing systems is costly.
setInterval(() => { if (currentView === "overview") refreshDeck(); }, 60000);
navigate();
