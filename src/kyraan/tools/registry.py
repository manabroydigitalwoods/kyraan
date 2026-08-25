"""Tool registry — every tool Kyraan can call is declared in
config/permissions.yaml's `tools:` section and validated here at load.
No tool exists outside the registry, exactly as the kernel refuses to run
an unregistered skill. Design: docs/design/tool_registry.md.
"""
import importlib
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
        )
        _validate(name, spec, all_names, servers)
        specs[name] = spec
    return specs


def get(tool_name: str) -> ToolSpec:
    specs = load()
    if tool_name not in specs:
        raise ToolError(f"unknown tool {tool_name!r} — not in config/permissions.yaml's tools section")
    return specs[tool_name]


@lru_cache
def _adapter_module(server: str):
    servers = config.load().get("tool_servers", {}) or {}
    entry = servers[server]
    transport = entry.get("transport", "builtin")
    if transport == "builtin":
        return importlib.import_module(entry["module"])
    raise ToolError(
        f"tool server {server!r}: transport {transport!r} not implemented yet — "
        "builtin is the only Phase 2 transport (MCP-stdio is the designed next step)"
    )


async def dispatch(spec: ToolSpec, args: dict):
    """Hand the call to the tool's adapter. Adapters expose one interface:
    `async def call(tool_name, args)` — builtin or MCP, callers can't tell."""
    module = _adapter_module(spec.server)
    return await module.call(spec.name, args)
