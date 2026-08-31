"""The web panel — Phase A: read-only glass over what Kyraan already logs.

Design record: docs/design/web_panel.md. Two rules govern this package:

1. **No writes.** Not to Postgres, not to the file stores, not to the log.
   Every control surface (Phase C) must go through
   `control_plane/kernel.py` so enforcement never lives in two places;
   Phase A sidesteps the question entirely by being a reader.
2. **Nothing here renders HTML.** The API emits JSON; the page builds its
   DOM with textContent only. Events carry web snippets, MCP output, and
   email subjects — attacker-reachable text on a machine holding OAuth
   tokens and face embeddings.

Bound to 127.0.0.1 with an owner token. Reach it from a phone over
Tailscale, never a forwarded port.
"""
