"""Tool registry — every tool Kyraan can call is declared in
config/permissions.yaml's `tools:` section and validated here at load.
No tool exists outside the registry, exactly as the kernel refuses to run
an unregistered skill. Design: docs/design/tool_registry.md.

Two transports behind one adapter interface: `builtin` (an importable
module exposing `async def call(tool_name, args)`) and `mcp-stdio` (a
child process speaking MCP JSON-RPC over stdio — the plan's standard for
external tools). Callers can't tell them apart; moving a tool between
transports is a config change.
"""
import asyncio
import importlib
import itertools
import json as _json
from dataclasses import dataclass
from functools import lru_cache

from kyraan.control_plane import config

_PERMISSIONS = {"auto", "confirm", "disabled"}
_SIDE_EFFECTS = {"read", "write", "notify"}
_PARAM_TYPES = {"string", "datetime", "number", "bool"}


class ToolError(Exception):
    """Non-transient tool failure — retrying won't help (bad config,
    missing credential, 4xx, unknown tool)."""


class TransientToolError(Exception):
    """Transient tool failure (network, timeout, 5xx) — worth retrying up
    to the entry's `retries` budget."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    server: str
    permission: str        # auto | confirm | disabled
    side_effects: str      # read | write | notify
    params: dict           # {param: {type, required}}
    returns: str
    retries: int
    timeout_s: float
    on_failure: str        # surface | fallback:<tool.name> | silent
    # MCP-mounted tools (2026-08-31): the server's OWN tool name. Ours
    # stay namespaced ("gh.create_issue"); the wire carries this.
    mcp_name: str = ""


def _validate(name: str, spec: ToolSpec, all_names: set, servers: dict) -> None:
    if spec.permission not in _PERMISSIONS:
        raise ValueError(f"tool {name!r}: unknown permission {spec.permission!r}")
    if spec.side_effects not in _SIDE_EFFECTS:
        raise ValueError(f"tool {name!r}: unknown side_effects {spec.side_effects!r}")
    # The hard rule: a write/notify tool with auto permission must be
    # impossible, not discouraged. Relaxing this for a specific tool is an
    # explicit Phase 4 decision, made here, in code review — never by config.
    if spec.side_effects in ("write", "notify") and spec.permission == "auto":
        raise ValueError(
            f"tool {name!r}: side_effects={spec.side_effects!r} requires permission "
            "'confirm' (or 'disabled') — auto write tools are forbidden"
        )
    if spec.server not in servers:
        raise ValueError(f"tool {name!r}: unknown server {spec.server!r} — declare it under tool_servers")
    for pname, pspec in spec.params.items():
        if not isinstance(pspec, dict) or pspec.get("type") not in _PARAM_TYPES:
            raise ValueError(f"tool {name!r}: param {pname!r} needs a type in {sorted(_PARAM_TYPES)}")
    if spec.on_failure.startswith("fallback:"):
        target = spec.on_failure.split(":", 1)[1]
        if target not in all_names or target == name:
            raise ValueError(f"tool {name!r}: fallback target {target!r} is not a registered tool")
    elif spec.on_failure == "silent":
        if spec.side_effects != "notify":
            raise ValueError(f"tool {name!r}: on_failure 'silent' is legal only for notify tools")
    elif spec.on_failure != "surface":
        raise ValueError(f"tool {name!r}: unknown on_failure {spec.on_failure!r}")


def load() -> dict:
    """Parse + validate the tools section. Raises ValueError on any
    misconfiguration — the app must fail at startup, not at call time."""
    cfg = config.load()
    raw_tools = cfg.get("tools", {}) or {}
    servers = cfg.get("tool_servers", {}) or {}
    for sname, sentry in servers.items():
        transport = (sentry or {}).get("transport", "builtin")
        if transport == "builtin" and not (sentry or {}).get("module"):
            raise ValueError(f"tool server {sname!r}: builtin transport needs a module")
        if transport == "mcp-stdio" and not (sentry or {}).get("command"):
            raise ValueError(f"tool server {sname!r}: mcp-stdio transport needs a command list")
        if transport not in ("builtin", "mcp-stdio"):
            raise ValueError(f"tool server {sname!r}: unknown transport {transport!r}")
    all_names = set(raw_tools)
    specs = {}
    for name, entry in raw_tools.items():
        failure = entry.get("failure", {}) or {}
        spec = ToolSpec(
            name=name,
            description=entry.get("description", ""),
            server=entry.get("server", ""),
            permission=entry.get("permission", "confirm"),  # unlisted default: confirm, like skills
            side_effects=entry.get("side_effects", "write"),  # unknown effect class treated as most dangerous
            params=entry.get("params", {}) or {},
            returns=entry.get("returns", ""),
            retries=int(failure.get("retries", 0)),
            timeout_s=float(failure.get("timeout_s", 10)),
            on_failure=failure.get("on_failure", "surface"),
            mcp_name=str(entry.get("mcp_name", "") or ""),
        )
        _validate(name, spec, all_names, servers)
        specs[name] = spec
    for spec in specs.values():
        if spec.on_failure.startswith("fallback:"):
            target = specs[spec.on_failure.split(":", 1)[1]]
            if target.permission == "confirm":
                raise ValueError(
                    f"tool {spec.name!r}: fallback target {target.name!r} is confirm-gated — "
                    "a mid-failure fallback must never spring a confirmation on the user"
                )
    return specs


def _verification_class(name: str, spec: "ToolSpec") -> str | None:
    """From the loop's declared map (the executors own verification);
    reads need none."""
    if spec.side_effects == "read":
        return None
    try:
        from kyraan.agents.loop_tools import VERIFICATION_CLASS
        return VERIFICATION_CLASS.get(name)
    except Exception:
        return None


def contracts() -> dict:
    """Capability-contract metadata (plan §3c, adopted 2026-08-28): per
    tool, {effect, risk, requires_confirmation, taint} as auditable DATA
    derived from the declared spec — never a second hand-written table
    that can drift. The suite pins the invariant the derivation rests
    on: every write-effect tool is confirm-gated."""
    from kyraan.control_plane import taint as _taint
    out = {}
    for name, spec in load().items():
        out[name] = {
            "effect": spec.side_effects,
            "risk": ("external_write" if spec.side_effects == "write"
                     else "notify" if spec.side_effects == "notify"
                     else "read_only"),
            "requires_confirmation": spec.permission == "confirm",
            "taint": _taint.source_class(name),
            "verification": _verification_class(name, spec),
        }
    return out


# Normalized error names (plan §3c): a thin mapping over the two error
# classes so logs and failure handling can say WHAT KIND without every
# caller re-parsing provider prose. Not a new system — a label.
def error_name(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return "TIMEOUT"
    if any(t in text for t in ("401", "403", "unauthorized", "forbidden",
                               "credential", "auth", "api key", "token expired")):
        return "AUTH_REQUIRED"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "RATE_LIMITED"
    if "404" in text or "not found" in text:
        return "NOT_FOUND"
    if isinstance(exc, TransientToolError):
        return "NETWORK"
    return "TOOL_FAILED"


def get(tool_name: str) -> ToolSpec:
    specs = load()
    if tool_name not in specs:
        raise ToolError(f"unknown tool {tool_name!r} — not in config/permissions.yaml's tools section")
    return specs[tool_name]


class MCPStdioAdapter:
    """Minimal MCP client: spawns the configured command once, handshakes
    (initialize / initialized), then serializes tools/call requests over
    the child's stdio. One in-flight request at a time — MCP stdio is a
    single ordered stream. A dead child is respawned on the next call."""

    def __init__(self, command: list, env: dict | None = None):
        self._command = command
        self._env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        import os as _os
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**_os.environ, **self._env},
        )
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "kyraan", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    def _notify(self, method: str, params: dict) -> None:
        line = _json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        self._proc.stdin.write(line.encode())

    async def _request(self, method: str, params: dict) -> dict:
        req_id = next(self._ids)
        line = _json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                raise TransientToolError(f"MCP server {self._command[0]} closed its stdio mid-request")
            try:
                message = _json.loads(raw)
            except _json.JSONDecodeError:
                continue  # servers may log stray lines to stdout; skip them
            if message.get("id") != req_id:
                continue  # notification or stale reply — not ours
            if "error" in message:
                raise ToolError(f"MCP server error: {message['error'].get('message', message['error'])}")
            return message.get("result", {})

    async def call(self, tool_name: str, args: dict) -> object:
        async with self._lock:
            try:
                await self._ensure_started()
                result = await self._request("tools/call", {"name": tool_name, "arguments": args})
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._proc = None
                raise TransientToolError(f"MCP server pipe broke: {exc}") from exc
        if result.get("isError"):
            raise ToolError(f"MCP tool {tool_name!r} reported an error: {result.get('content')}")
        content = result.get("content") or []
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return _json.loads(joined)  # structured results ride as JSON text
        except _json.JSONDecodeError:
            return joined


@lru_cache
def _adapter(server: str):
    servers = config.load().get("tool_servers", {}) or {}
    entry = servers[server]
    transport = entry.get("transport", "builtin")
    if transport == "builtin":
        return importlib.import_module(entry["module"])
    if transport == "mcp-stdio":
        return MCPStdioAdapter(list(entry["command"]),
                               env=dict(entry.get("env") or {}))
    raise ToolError(f"tool server {server!r}: unknown transport {transport!r} (builtin | mcp-stdio)")


# Back-compat name used by tests/monkeypatches.
_adapter_module = _adapter


async def dispatch(spec: ToolSpec, args: dict):
    """Hand the call to the tool's adapter. Adapters expose one interface:
    `async def call(tool_name, args)` — builtin or MCP, callers can't tell.
    MCP servers get their OWN tool name on the wire (mcp_name, defaulting
    to the part after our namespace dot)."""
    adapter = _adapter(spec.server)
    if isinstance(adapter, MCPStdioAdapter):
        wire = spec.mcp_name or spec.name.split(".", 1)[-1]
        return await adapter.call(wire, args)
    return await adapter.call(spec.name, args)
