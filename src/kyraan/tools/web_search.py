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


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "web.search":
        return await asyncio.to_thread(_search, args.get("query", ""), args.get("count", 5))
    raise ToolError(f"web_search adapter does not provide {tool_name!r}")
