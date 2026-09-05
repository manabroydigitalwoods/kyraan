"""Headlines (owner 2026-09-05: "Kyraan does not even share the top
headlines… we will require top headlines from my current state, country
and world… every morning and evening… human readable, with the important
points of every headline").

Not a web search. Editors' front pages, by RSS: The Hindu (Kolkata,
national), NDTV top stories, BBC World, and Google News' regional feed
— dated items with a real summary each, so a headline is a headline,
not a snippet that happened to rank. The worker model writes one line
of why-it-matters per headline from the title and summary ONLY; when
it is unavailable the summary's first sentence stands in. Cached ten
minutes per scope. Feed text is untrusted data, never instructions."""
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from kyraan.control_plane.logging_setup import log_event

CACHE_S = 600
_cache: dict = {}

SCOPES = ("state", "country", "world")
LABELS = {"state": "🏙 {state}", "country": "🇮🇳 {country}", "world": "🌍 World"}


def _settings() -> dict:
    try:
        from kyraan.control_plane import config
        cfg = config.load().get("news") or {}
    except Exception:
        cfg = {}
    return {"state": str(cfg.get("state") or "West Bengal"),
            "country": str(cfg.get("country") or "India"),
            "per_scope": int(cfg.get("per_scope") or 5),
            "digest_times": list(cfg.get("digest_times") or ["07:45", "19:45"]),
            "enabled": cfg.get("enabled", True) is not False}


def feeds(scope: str) -> list:
    s = _settings()
    q = urllib.request.quote(s["state"])
    return {
        "state": [
            "https://www.thehindu.com/news/cities/kolkata/feeder/default.rss",
            f"https://news.google.com/rss/search?q={q}+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
        ],
        "country": [
            "https://feeds.feedburner.com/ndtvnews-top-stories",
            "https://www.thehindu.com/news/national/feeder/default.rss",
        ],
        "world": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
        ],
    }[scope]


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(s: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", str(s or "")))).strip()


def _first_sentence(s: str, limit: int = 180) -> str:
    raw = str(s or "").lstrip()
    if raw.startswith(("<a", "<ol", "<ul")):
        return ""          # Google News: a list of related links, not a summary
    s = _text(s)
    if not s or s.startswith("http") or len(s) < 25:
        return ""
    m = re.search(r"^(.+?[.!?])(\s|$)", s)
    out = m.group(1) if m and len(m.group(1)) <= limit else s[:limit].rsplit(" ", 1)[0]
    return out.strip()


def _clean_title(title: str, source: str) -> str:
    t = _text(title)
    if source and t.lower().endswith((" - " + source).lower()):
        t = t[: -len(source) - 3].rstrip()
    return t


def _host(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        h = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("feeds.")
        return {"bbci.co.uk": "BBC", "ndtv.com": "NDTV", "thehindu.com": "The Hindu",
                "feedburner.com": "NDTV", "news.google.com": ""}.get(h, h)
    except Exception:
        return ""


def parse_feed(xml_text: str, feed_url: str = "") -> list:
    """Items from one RSS document: title, source, published (UTC), link,
    summary. Malformed input yields nothing — never an exception."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    default_source = _host(feed_url)
    out = []
    for it in root.findall(".//item"):
        src = it.find("source")
        source = _text(src.text if src is not None else "") or default_source
        title = _clean_title(it.findtext("title") or "", source)
        if not title:
            continue
        pub = None
        for tag in ("pubDate", "{http://purl.org/dc/elements/1.1/}date"):
            raw = it.findtext(tag)
            if raw:
                try:
                    pub = parsedate_to_datetime(raw)
                except Exception:
                    try:
                        pub = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except Exception:
                        pub = None
                break
        if pub is not None and pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        out.append({
            "title": title[:160],
            "source": source[:40],
            "published": pub,
            "link": (it.findtext("link") or "").strip(),
            "summary": _first_sentence(it.findtext("description") or ""),
        })
    return out


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Kyraan headlines)"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:60]


def headlines(scope: str, limit: int | None = None) -> list:
    """The freshest distinct items for a scope, newest first."""
    s = _settings()
    limit = limit or s["per_scope"]
    cached = _cache.get(scope)
    if cached and time.monotonic() - cached[0] < CACHE_S:
        return cached[1][:limit]
    items = []
    for url in feeds(scope):
        try:
            items += parse_feed(_fetch(url), url)
        except Exception as exc:
            log_event("news_feed_failed", scope=scope, url=url[:60], error=str(exc)[:80])
    seen, distinct = set(), []
    for it in sorted(items, key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        key = _norm(it["title"])
        if key in seen:
            continue
        seen.add(key)
        distinct.append(it)
    _cache[scope] = (time.monotonic(), distinct)
    return distinct[:limit]


_SYSTEM = ("You write the one-line 'why it matters' under each news headline for a busy reader. "
           "Do NOT restate the headline — add what it means, who is affected, or what comes next, "
           "taken from the summary. Use ONLY the title and summary given — never add facts, names "
           "or numbers that are not there. At most 22 words per line, plain words, no hype. "
           "Answer ONLY with JSON: "
           '{"points": ["<line for item 1>", "<line for item 2>", ...]} in the same order.')


def key_points(items: list) -> list:
    """One line per item from the worker model, else the summary."""
    fallback = [it.get("summary") or "" for it in items]
    if not items:
        return []
    try:
        from kyraan.model_router import router
        prompt = "\n".join(f"{i + 1}. {it['title']}\n   summary: {it.get('summary') or '(none)'}"
                           for i, it in enumerate(items))
        resp = router.call(prompt=prompt, system=_SYSTEM, tier=router.worker_tier(),
                           max_tokens=60 * len(items) + 40, force_json=True)
        data = json.loads(router.strip_code_fence(resp.text or "{}"))
        points = [str(p).strip() for p in (data.get("points") or [])]
        if len(points) != len(items):
            raise ValueError("count mismatch")
        return [p[:200] if p else fallback[i] for i, p in enumerate(points)]
    except Exception as exc:
        log_event("news_points_failed", error=str(exc)[:80])
        return fallback


def _ago(pub, now) -> str:
    if pub is None:
        return ""
    mins = int((now - pub).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)}m ago"
    if mins < 36 * 60:
        return f"{mins // 60}h ago"
    return pub.astimezone().strftime("%-d %b")


def render(sections: dict, now=None) -> str:
    """Human-readable digest — a numbered headline with its source and
    age, then one indented line of what matters."""
    from kyraan.control_plane.dnd import local_now
    now = now or datetime.now(timezone.utc)
    s = _settings()
    head = f"📰 Headlines · {local_now().strftime('%a %-d %b, %-I:%M %p')}"
    blocks = [head]
    for scope, items in sections.items():
        label = LABELS[scope].format(state=s["state"], country=s["country"])
        if not items:
            blocks.append(f"{label}\n(nothing fresh came through)")
            continue
        points = key_points(items)
        lines = [label]
        for i, (it, point) in enumerate(zip(items, points), 1):
            meta = " · ".join(x for x in (it.get("source"), _ago(it.get("published"), now)) if x)
            lines.append(f"{i}. {it['title']}" + (f" — {meta}" if meta else ""))
            if point:
                lines.append(f"   ↳ {point}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def digest_text(scopes=SCOPES, per: int | None = None) -> str:
    return render({sc: headlines(sc, per) for sc in scopes})


def scope_from_words(text: str) -> tuple:
    """Which scopes an ask names: 'bengal news' → state; 'world news' →
    world; plain 'news' → all three."""
    low = str(text or "").lower()
    s = _settings()
    want = []
    if any(w in low for w in (s["state"].lower(), "bengal", "state", "local", "kolkata", "siliguri")):
        want.append("state")
    if any(w in low for w in (s["country"].lower(), "national", "country", "desh")):
        want.append("country")
    if any(w in low for w in ("world", "international", "global", "foreign")):
        want.append("world")
    return tuple(want) or SCOPES
