"""web.open (governance round 2026-08-31): SSRF guards, text
extraction, the provenance rail, and the taint class."""
import pytest

from kyraan.control_plane import taint
from kyraan.tools import web_search
from kyraan.tools.registry import ToolError


# --- SSRF guard -----------------------------------------------------------

@pytest.mark.parametrize("url,msg", [
    ("ftp://example.com/x", "only http"),
    ("http://user:pw@example.com/", "credentials"),
    ("http:///nohost", "no host"),
    ("http://127.0.0.1:8123/api", "private network"),
    ("http://192.168.1.10/", "private network"),
    ("http://[::1]/", "private network"),
    ("http://169.254.169.254/latest/meta-data", "private network"),
])
def test_ssrf_guard_refuses(url, msg):
    with pytest.raises(ToolError, match=msg):
        web_search._assert_public(url)


def test_public_host_passes():
    assert web_search._assert_public("https://example.com/a").hostname == "example.com"


def test_redirect_hops_are_each_revalidated(monkeypatch):
    # a PUBLIC page redirecting to the Home Assistant box must be refused
    import urllib.error
    hops = []

    class _Opener:
        def open(self, req, timeout=None):
            hops.append(req.full_url)
            err = urllib.error.HTTPError(req.full_url, 302, "found",
                                         {"Location": "http://192.168.1.5/"}, None)
            raise err
    monkeypatch.setattr(web_search.urllib.request, "build_opener",
                        lambda *a: _Opener())
    with pytest.raises(ToolError, match="private network"):
        web_search._open("https://example.com/page")
    assert hops == ["https://example.com/page"]  # hop 2 refused pre-fetch


# --- extraction -----------------------------------------------------------

def test_html_becomes_readable_text():
    parser = web_search._TextExtract()
    parser.feed("<html><head><title>Big News</title>"
                "<script>evil()</script></head><body><nav>menu</nav>"
                "<h1>Headline</h1><p>First para.</p>"
                "<p>Second para.</p></body></html>")
    text = " ".join(parser.chunks)
    assert "Headline" in text and "First para." in text
    assert "evil" not in text and "menu" not in text
    assert parser.title == "Big News"


# --- provenance rail ------------------------------------------------------

async def test_open_refuses_urls_without_provenance():
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    loop_tools.reset_turn_urls()
    with pytest.raises(kernel.ToolFailed, match="search results"):
        await loop_tools._web_open(7, {"url": "https://evil.example/x"}, "hi")


async def test_search_results_and_user_urls_are_openable(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    loop_tools.reset_turn_urls()
    loop_tools._note_urls(["https://thehindu.com/story1"])
    opened = []

    async def fake_run(call, **kw):
        opened.append(call.args["url"])
        return {"url": call.args["url"], "title": "t", "text": "body"}
    monkeypatch.setattr(kernel, "run_tool", fake_run)
    await loop_tools._web_open(7, {"url": "https://thehindu.com/story1"}, "")
    await loop_tools._web_open(
        7, {"url": "https://manab.link/doc"},
        "read https://manab.link/doc please")   # user-supplied provenance
    assert len(opened) == 2


def test_fetched_pages_carry_the_web_taint():
    assert taint.source_class("web.open") == taint.WEB_UNTRUSTED
