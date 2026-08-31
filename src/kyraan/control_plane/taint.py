"""Trust/taint classes — plan §3c, adopted 2026-08-28, built 2026-08-31.

Names the taxonomy OVER the mechanisms that already enforce it, so every
"where may this data go?" question has one checked place instead of a
per-adapter re-derivation. This module changes no behavior: the
write-lockout lives in the agent loop, the cloud exclusions live in the
adapters and prompt assembly, the biometric wall lives in the handlers —
each cites its class here, and the test suite pins the map.
"""

# Untrusted CONTENT classes — text a third party authored. Its danger is
# prompt injection: it may instruct, so it must never precede a write.
WEB_UNTRUSTED = "WEB_UNTRUSTED"
EMAIL_UNTRUSTED = "EMAIL_UNTRUSTED"

# Sensitive DATA classes — trusted content whose danger is exposure.
BIOMETRIC = "BIOMETRIC"          # face templates, voice audio
PENDING_FACTS = "PENDING_FACTS"  # unreviewed extraction proposals
CONTACT_DATA = "CONTACT_DATA"    # reserved: contacts sync (§3c adopt-next)

# What each class means operationally — the doctrine line an auditor (or
# a future adapter author) reads first.
DOCTRINE = {
    WEB_UNTRUSTED: ("Search snippets are third-party text: once any enter "
                    "a turn, every non-read tool is blocked for the rest "
                    "of that turn (agent_loop taint rail)."),
    EMAIL_UNTRUSTED: ("Email bodies are third-party text AND private: "
                      "processed local-only, never in cloud prompts, "
                      "summaries composed off-prompt (gmail adapter §3a)."),
    BIOMETRIC: ("Face templates and voice audio never leave the machine; "
                "only a matched NAME may enter a prompt. Owner-governed "
                "regardless of grants (telegram handlers)."),
    PENDING_FACTS: ("Unreviewed facts reach the local tier only — never "
                    "cloud prompts, never other viewers (memory engine)."),
    CONTACT_DATA: ("Reserved. Contact names/numbers entering cloud prompts "
                   "is a governance §0 policy event — decide before use."),
}

# Which tool RESULTS carry which class. Read by the agent loop's taint
# rail; extended here (one line) when a new source lands.
SOURCE_CLASSES = {
    "web.search": WEB_UNTRUSTED,
    "web.open": WEB_UNTRUSTED,
    "email.read": EMAIL_UNTRUSTED,
    "email.unread": EMAIL_UNTRUSTED,
    "email.important": EMAIL_UNTRUSTED,
    "email.search": EMAIL_UNTRUSTED,
    "faces.check_photo": BIOMETRIC,
    "memory.pending_list": PENDING_FACTS,
}


def source_class(tool_name: str) -> str | None:
    """The taint class a tool's result carries, or None for clean.
    Config-aware for mounted MCP servers (2026-08-31): a tool_servers
    entry declaring `untrusted: true` marks every one of its tools'
    results as third-party text — the write-lockout covers them the
    same way it covers web snippets."""
    fixed = SOURCE_CLASSES.get(tool_name)
    if fixed:
        return fixed
    try:
        from kyraan.control_plane import config as _config
        cfg = _config.load()
        server = (cfg.get("tools", {}) or {}).get(tool_name, {}).get("server")
        entry = (cfg.get("tool_servers", {}) or {}).get(server) or {}
        if entry.get("untrusted"):
            return WEB_UNTRUSTED
    except Exception:
        pass
    return None
