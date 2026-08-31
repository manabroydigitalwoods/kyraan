"""Web search adapter — tool #4 (2026-08-26). Self-hosted SearXNG (open
source, in Docker on this Mac, same pattern as Home Assistant): no API
key, no per-query cost, and the only thing that leaves the machine is the
query itself, fanned out by SearXNG to public engines. Read-only by
construction: titles, URLs, and snippets only — Kyraan never fetches or
renders a page, so the injection surface is capped at snippet text (and
the agent loop's taint rail keeps even that text from ever steering a
non-read tool in the same turn).

Setup (one-time, done 2026-08-26): the container is compose-managed from
docker/ in this repo (`docker compose up -d`); its config lives at
docker/searxng/settings.yml (JSON format enabled, limiter off — local-only
instance). SEARXNG_URL in .env points at it (http://127.0.0.1:8888).
Unset = the capability brief lists search under "not connected yet".

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from kyraan.tools.registry import ToolError, TransientToolError

_MAX_RESULTS = 8
_MAX_QUERY_CHARS = 400


def configured() -> bool:
    return bool(os.environ.get("SEARXNG_URL", "").strip())


def _base() -> str:
    url = os.environ.get("SEARXNG_URL", "").strip().rstrip("/")
    if not url:
        raise ToolError(
            "web search isn't configured — set SEARXNG_URL in .env "
            "(the local SearXNG container, e.g. http://127.0.0.1:8888)"
        )
    return url


def _search(query: str, count: int) -> dict:
    query = str(query).strip()[:_MAX_QUERY_CHARS]
    if not query:
        raise ToolError("web.search needs a non-empty query")
    count = max(1, min(int(count), _MAX_RESULTS))
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    request = urllib.request.Request(
        f"{_base()}/search?{params}",
        headers={
            "Accept": "application/json",
            # SearXNG's botdetection logs an ERROR on every request that
            # arrives without a client-IP header (normally a reverse
            # proxy's job); we call it directly, so we say who we are.
            "X-Real-IP": "127.0.0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ToolError(
                "SearXNG refused the JSON API — ~/searxng/settings.yml needs "
                "'json' under search.formats (then restart the container)"
            ) from exc
        if exc.code == 429:
            raise TransientToolError("SearXNG rate limit hit — its limiter should be off for local use") from exc
        if exc.code >= 500:
            raise TransientToolError(f"SearXNG returned {exc.code}") from exc
        raise ToolError(f"SearXNG error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(
            f"could not reach SearXNG at {_base()} — is the searxng container "
            f"running? (docker start searxng): {exc}"
        ) from exc

    results = []
    for item in data.get("results", [])[:count]:
        results.append({
            "title": str(item.get("title", "")).strip(),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "") or "").strip(),
            **({"published": str(item.get("publishedDate"))}
               if item.get("publishedDate") else {}),
        })
    return {"query": query, "results": results}


# --- web.open (governance round 2026-08-31, plan §3c gate lifted) ----------
# Owner's conditions, all deterministic:
# - provenance: the LOOP enforces that only URLs from this turn's search
#   results or the user's own message are openable (loop_tools) — a URL
#   found inside a fetched page is not
# - taint: fetched text is WEB_UNTRUSTED (control_plane/taint.py) — the
#   write-lockout covers it the moment it enters the turn
# - SSRF: http/https only, no credentials in the URL, every redirect
#   hop re-validated against private/loopback/link-local/reserved
#   ranges — Home Assistant and Postgres do not exist for this tool
# - direct fetch accepted (ordinary browsing exposure), size/time caps

_MAX_BYTES = 500_000
_MAX_TEXT = 6_000
_MAX_HOPS = 3


def _assert_public(url: str) -> "urllib.parse.SplitResult":
    import ipaddress
    import socket
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError("only http(s) pages can be opened")
    if parts.username or parts.password:
        raise ToolError("URLs with credentials are refused")
    host = parts.hostname or ""
    if not host:
        raise ToolError("no host in that URL")
    try:
        infos = socket.getaddrinfo(host, parts.port or
                                   (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise TransientToolError(f"cannot resolve {host}: {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ToolError(f"{host} resolves into a private network — "
                            "refused (SSRF guard)")
    return parts


class _TextExtract(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg", "head",
             "nav", "footer", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks, self._skipping, self.title = [], 0, ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title.strip():
            self.title = data.strip()[:200]
        if not self._skipping and data.strip():
            self.chunks.append(data)


def _open(url: str) -> dict:
    import re as _re
    import urllib.request
    seen = 0
    for _hop in range(_MAX_HOPS + 1):
        _assert_public(url)   # EVERY hop — a public host may redirect inward
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": "Mozilla/5.0 (Kyraan personal assistant; reads text)",
            "Accept": "text/html,application/xhtml+xml,text/plain"})

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=12)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                target = exc.headers.get("Location", "")
                if not target:
                    raise ToolError("redirect with no target")
                url = urllib.parse.urljoin(url, target)
                continue
            raise ToolError(f"the page answered {exc.code}")
        except OSError as exc:
            raise TransientToolError(f"fetch failed: {exc}")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not any(t in ctype for t in ("text/html", "text/plain", "xml")):
            raise ToolError(f"not a readable page ({ctype.split(';')[0] or 'unknown type'})")
        raw = resp.read(_MAX_BYTES)
        charset = "utf-8"
        m = _re.search(r"charset=([\w-]+)", ctype)
        if m:
            charset = m.group(1)
        text = raw.decode(charset, errors="replace")
        if "html" in ctype or text.lstrip()[:1] == "<":
            parser = _TextExtract()
            try:
                parser.feed(text)
            except Exception:
                pass
            body = _re.sub(r"\n{3,}", "\n\n",
                           " ".join(parser.chunks).replace(" \n ", "\n")).strip()
            title = parser.title
        else:
            body, title = text.strip(), ""
        return {"url": url, "title": title, "text": body[:_MAX_TEXT],
                "truncated": len(body) > _MAX_TEXT}
    raise ToolError("too many redirects")


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "web.search":
        return await asyncio.to_thread(_search, args.get("query", ""), args.get("count", 5))
    if tool_name == "web.open":
        return await asyncio.to_thread(_open, str(args.get("url", "")).strip())
    raise ToolError(f"web_search adapter does not provide {tool_name!r}")
