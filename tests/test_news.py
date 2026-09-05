"""Headlines (owner 2026-09-05): feed parsing, dedupe, render, rail."""
import asyncio
from datetime import datetime, timezone

from kyraan.tools import news

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Suvendu Adhikari unveils new tourism logo - Telegraph India</title>
 <link>https://news.google.com/rss/articles/x</link><pubDate>Sat, 05 Sep 2026 04:30:00 GMT</pubDate>
 <source url="https://telegraphindia.com">Telegraph India</source>
 <description>&lt;a href="https://x"&gt;link&lt;/a&gt;</description></item>
<item><title>Bonus challenge ahead for tea planters</title>
 <link>https://www.thehindu.com/x</link><pubDate>Sat, 05 Sep 2026 03:00:00 GMT</pubDate>
 <description>Government, planters and unions meet next week to fix the rate. More text here.</description></item>
<item><title>Suvendu Adhikari unveils new tourism logo</title>
 <link>https://www.thehindu.com/y</link><pubDate>Sat, 05 Sep 2026 02:00:00 GMT</pubDate>
 <description>A duplicate of the first, older.</description></item>
</channel></rss>"""


def test_parse_cleans_titles_and_summaries():
    items = news.parse_feed(RSS, "https://www.thehindu.com/news/cities/kolkata/feeder/default.rss")
    assert items[0]["title"] == "Suvendu Adhikari unveils new tourism logo" and items[0]["source"] == "Telegraph India"
    assert items[0]["summary"] == ""                                   # a link-only description is nothing
    assert items[1]["source"] == "The Hindu"                          # from the feed's host
    assert items[1]["summary"] == "Government, planters and unions meet next week to fix the rate."
    assert items[0]["published"] == datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc)
    assert news.parse_feed("<not xml", "") == []


def test_headlines_dedupe_and_order(monkeypatch):
    monkeypatch.setattr(news, "_fetch", lambda url: RSS)
    monkeypatch.setattr(news, "feeds", lambda scope: ["u1", "u2"])
    news._cache.clear()
    got = news.headlines("state", 10)
    assert [g["title"] for g in got] == ["Suvendu Adhikari unveils new tourism logo", "Bonus challenge ahead for tea planters"]


def test_render_falls_back_to_summaries_without_the_model(monkeypatch):
    from kyraan.model_router import router
    def boom(**kw): raise RuntimeError("no model")
    monkeypatch.setattr(router, "call", boom)
    items = news.parse_feed(RSS, "https://www.thehindu.com/x")[:2]
    now = datetime(2026, 9, 5, 5, 0, tzinfo=timezone.utc)
    out = news.render({"state": items, "world": []}, now=now)
    assert out.startswith(news.HTML_MARK + "📰 <b>Headlines</b> ·")
    assert "<b>🏙 WEST BENGAL</b>\n\n1. <b>Suvendu Adhikari unveils new tourism logo</b>\n    <i>Telegraph India · 30m ago</i>" in out
    assert "2. <b>Bonus challenge ahead for tea planters</b>\n    <i>The Hindu · 2h ago</i>\n    → Government, planters and unions meet next week to fix the rate." in out
    assert "<b>🌍 WORLD</b>\n(nothing fresh came through)" in out


def test_render_escapes_html_and_trims_long_titles(monkeypatch):
    from kyraan.model_router import router
    monkeypatch.setattr(router, "call", lambda **kw: (_ for _ in ()).throw(RuntimeError("no")))
    long = "A" * 120
    items = [{"title": f"Tata & Sons <win> {long}", "source": "X", "published": None, "link": "", "summary": "It is 5 > 3 & fine, they said."}]
    out = news.render({"country": items}, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert "Tata &amp; Sons &lt;win&gt;" in out and out.count("…") == 1
    assert "→ It is 5 &gt; 3 &amp; fine, they said." in out


def test_channel_sends_marked_text_as_html():
    from kyraan.channels import telegram_bot as tb
    assert tb._send_kwargs(news.HTML_MARK + "<b>x</b>") == ("<b>x</b>", {"parse_mode": "HTML"})
    assert tb._send_kwargs("plain **x**") == ("plain **x**", {})
    assert tb._plain(news.HTML_MARK + "**keep**") == news.HTML_MARK + "**keep**"


def test_scope_words():
    assert news.scope_from_words("bengal") == ("state",)
    assert news.scope_from_words("world") == ("world",)
    assert news.scope_from_words("india") == ("country",)
    assert news.scope_from_words("") == news.SCOPES


def test_news_rail(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    asked = []
    monkeypatch.setattr(news, "digest_text", lambda scopes=news.SCOPES, per=None: asked.append(tuple(scopes)) or "📰 Headlines · x")
    for q, want in (("news", news.SCOPES), ("top headlines", news.SCOPES), ("bengal news", ("state",)),
                    ("world news today", ("world",)), ("news from india", ("country",)),
                    ("give me the latest headlines", news.SCOPES), ("what's the news?", news.SCOPES)):
        out = asyncio.run(orchestrator.handle_message(1, q))
        assert out.startswith("📰"), q
        assert asked[-1] == want, (q, asked[-1])
    assert asyncio.run(orchestrator.handle_message(1, "any ai related news?")) != "📰 Headlines · x"   # a topic: search, not the digest


def test_log_and_history_keep_words_not_tags():
    from kyraan.control_plane.logging_setup import plain_record
    marked = news.HTML_MARK + "📰 <b>Headlines</b>\n1. <b>Tata &amp; Sons</b>\n    <i>The Hindu · 5m ago</i>"
    assert plain_record(marked) == "📰 Headlines\n1. Tata & Sons\n    The Hindu · 5m ago"
    assert plain_record("plain <b>kept</b>") == "plain <b>kept</b>"          # only marked text is touched


def test_news_rail_takes_adjectives_after_the_scope(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    asked = []
    monkeypatch.setattr(news, "digest_text", lambda scopes=news.SCOPES, per=None: asked.append(tuple(scopes)) or "📰 x")
    for q, want in (("bengal top headlines", ("state",)), ("india latest news", ("country",)), ("top bengal headlines", ("state",))):
        assert asyncio.run(orchestrator.handle_message(1, q)).startswith("📰"), q
        assert asked[-1] == want, q
