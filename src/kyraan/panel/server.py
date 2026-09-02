"""The panel's HTTP server — stdlib only, loopback only, read-only.

No web framework on purpose. Phase A serves five static bytes-on-disk
files and a handful of JSON endpoints; FastAPI + uvicorn would add two
dependency trees and an ASGI stack to a single-owner localhost tool whose
entire job is reading files. If Phase C's control surfaces outgrow this,
that is the moment to reconsider — not before.

Security posture (docs/design/web_panel.md):
- binds 127.0.0.1 by default; reach it from a phone over Tailscale
- one owner token, compared with hmac.compare_digest, carried in an
  HttpOnly SameSite=Strict cookie after a one-time ?token= handshake
- Host header allowlist, so a DNS-rebinding page cannot make the browser
  replay that cookie against us
- CSP with no inline script or style: the page is three separate files,
  which is also why the API may never emit HTML
- the token is never written to a log line
"""
import hmac
import json
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kyraan.control_plane import logging_setup
from kyraan.panel import queries

log = logging_setup.get_logger("kyraan.panel")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "kyraan_panel"
DEFAULT_PORT = 8765

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}

# No inline script or style, no network destination but ourselves, no
# framing. The API returns JSON and the page writes textContent — event
# text is attacker-reachable (web snippets, MCP output, mail subjects),
# and an XSS here would be running next to the kernel's own machine.
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def resolve_token() -> tuple[str, bool]:
    """(token, generated). KYRAAN_PANEL_TOKEN if set, else a fresh one
    printed once at startup — a panel with no token is not an option."""
    configured = os.environ.get("KYRAAN_PANEL_TOKEN", "").strip()
    if configured:
        return configured, False
    return secrets.token_urlsafe(24), True


def _allowed_hosts(bind_host: str) -> set:
    hosts = set(_LOOPBACK_HOSTS)
    hosts.add(bind_host)
    extra = os.environ.get("KYRAAN_PANEL_ALLOWED_HOSTS", "")
    hosts |= {h.strip().lower() for h in extra.split(",") if h.strip()}
    return hosts


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kyraan-panel"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        """Never log the query string: the handshake carries the token in
        it, and this log is the same disk as everything else."""
        path = self.path.split("?", 1)[0]
        log.info("%s %s", self.command, path)

    def _security_headers(self):
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _send(self, code: int, body: bytes, content_type: str, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200):
        body = json.dumps(payload, default=str).encode()
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str):
        self._json({"error": message}, code=code)

    # -- auth -------------------------------------------------------------

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in self.server.allowed_hosts

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return urllib.parse.unquote(value)
        return ""

    def _authed(self, params) -> bool:
        expected = self.server.token
        for candidate in (self._cookie_token(),
                          self.headers.get("X-Kyraan-Token") or "",
                          (params.get("token") or [""])[0]):
            if candidate and hmac.compare_digest(candidate, expected):
                return True
        return False

    # -- routing ----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            # DNS rebinding: the browser would happily attach our cookie.
            self._error(421, "host not allowed")
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        # One-time handshake: ?token=… moves the secret out of the URL and
        # into an HttpOnly cookie, then redirects so it leaves the address
        # bar (and never reaches history or a screenshot).
        supplied = (params.get("token") or [""])[0]
        if supplied and not path.startswith("/api"):
            # Any page path, not only "/": a deep link such as
            # /brain?token=… used to serve the page (the query token
            # authenticates one request) and then 401 its own app.css and
            # app.js, which arrive with no token and no cookie — an
            # unstyled page with a stuck "connecting…". Set the cookie and
            # bounce to the same path with only the token removed.
            if not hmac.compare_digest(supplied, self.server.token):
                self._error(403, "bad token")
                return
            cookie = (f"{COOKIE_NAME}={urllib.parse.quote(supplied)}; "
                      "HttpOnly; SameSite=Strict; Path=/; Max-Age=604800")
            rest = {k: v for k, v in params.items() if k != "token"}
            location = path + ("?" + urllib.parse.urlencode(rest, doseq=True)
                               if rest else "")
            self._send(303, b"", "text/plain",
                       extra=(("Location", location), ("Set-Cookie", cookie)))
            return

        if not self._authed(params):
            self._error(401, "unauthorized — open the URL printed at startup")
            return

        if path.startswith("/api"):
            self._api(path, params)
        else:
            self._static(path)

    def _static(self, path: str):
        name = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        inside = str(target).startswith(str(STATIC_DIR.resolve()))
        if inside and target.is_file():
            content_type = _STATIC_TYPES.get(target.suffix,
                                             "application/octet-stream")
            self._send(200, target.read_bytes(), content_type)
            return
        # Deep-link fallback: /brain, /turns and friends are sectors of the
        # one page, not files. Serve the page and let it route, so a reload
        # or a bookmark lands where the reader was instead of dumping them
        # back on the overview.
        #
        # Extension-less only: /app.cs (a typo for /app.css) must still be
        # a hard 404 rather than silently returning HTML that never loads.
        # And `inside` still gates it: /../../etc/hosts has no extension
        # either, and answering that with 200 would be an odd thing for
        # this server to do even though it leaks nothing.
        if inside and "." not in Path(name).name:
            self._send(200, (STATIC_DIR / "index.html").read_bytes(),
                       _STATIC_TYPES[".html"])
            return
        self._error(404, "not found")

    def _api(self, path: str, params):
        def arg(name, default, cast=str):
            try:
                return cast((params.get(name) or [default])[0])
            except (TypeError, ValueError):
                return default

        try:
            if path == "/api/status":
                self._json(queries.status())
            elif path == "/api/health":
                self._json(queries.health(force=arg("force", "") == "1"))
            elif path == "/api/usage":
                self._json(queries.usage(days=arg("days", 7, int)))
            elif path == "/api/triggers":
                self._json(queries.triggers())
            elif path == "/api/events":
                self._json(queries.events(
                    limit=arg("limit", 200, int), hours=arg("hours", 24, float),
                    kind=arg("kind", ""), turn_id=arg("turn_id", ""),
                    anomalies_only=arg("anomalies", "") == "1",
                    query=arg("q", ""),
                    tools=tuple(t for t in arg("tools", "").split(",") if t)))
            elif path == "/api/event_kinds":
                self._json(queries.event_kinds(hours=arg("hours", 24, float)))
            elif path == "/api/turns":
                self._json(queries.turns(
                    limit=arg("limit", 50, int), hours=arg("hours", 24, float),
                    sort=arg("sort", "recent"),
                    tools=tuple(t for t in arg("tools", "").split(",") if t)))
            elif path == "/api/turn":
                self._json(queries.turn_detail(
                    arg("id", ""), full=arg("full", "") == "1"))
            elif path == "/api/actions":
                self._json(queries.actions(
                    limit=arg("limit", 200, int), days=arg("days", 30, float),
                    chat_id=(int(arg("chat", 0, int)) or None)))
            elif path == "/api/routines":
                self._json(queries.routines(hours=arg("hours", 24, float)))
            elif path == "/api/host":
                self._json(queries.host_now())
            elif path == "/api/host/history":
                self._json(queries.host_history())
            elif path == "/api/workload":
                self._json(queries.workload(hours=arg("hours", 24, float)))
            elif path == "/api/contacts":
                self._json(queries.contacts_search(arg("q", ""), limit=arg("limit", 8, int)))
            elif path == "/api/brain":
                self._json(queries.brain_graph(
                    synapse_floor=arg("floor", queries._SYNAPSE_FLOOR, float),
                    fresh=arg("fresh", "") == "1"))
            elif path == "/api/memory/map":
                self._json(queries.memory_map(
                    limit=arg("limit", 400, int),
                    include_inactive=arg("inactive", "1") == "1"))
            elif path == "/api/memory/links":
                self._json(queries.memory_links())
            elif path == "/api/memory/review":
                self._json(queries.memory_review())
            elif path == "/api/stream":
                self._stream()
            else:
                self._error(404, "no such endpoint")
        except BrokenPipeError:
            raise
        except Exception as exc:  # a panel bug must not take the panel down
            log.exception("panel endpoint failed: %s", path)
            self._error(500, f"{type(exc).__name__}: {exc}")

    # -- live tail --------------------------------------------------------

    def _stream(self):
        """SSE tail of the event log. Rotation-aware: the log is replaced
        under us at local midnight and at 5MB, and a reader holding the old
        handle would go quiet for the rest of the day."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        self.close_connection = True

        path = logging_setup.EVENT_LOG
        handle = None
        marker = None
        last_beat = time.monotonic()
        try:
            while not self.server.stopping:
                try:
                    stat = path.stat()
                    ident = (stat.st_ino, stat.st_dev)
                except OSError:
                    stat = ident = None

                if ident != marker:
                    # First pass or a rotation: reopen. On the first pass
                    # seek to the end (the page already loaded history via
                    # /api/events); a fresh post-rotation file is read whole.
                    if handle is not None:
                        handle.close()
                    handle = None
                    if ident is not None:
                        handle = open(path, "r", errors="replace")
                        if marker is None:
                            handle.seek(0, os.SEEK_END)
                    marker = ident

                sent_any = False
                if handle is not None:
                    for line in handle:
                        if not line.endswith("\n"):
                            # Partial write; rewind and wait for the rest.
                            handle.seek(handle.tell() - len(line))
                            break
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = json.dumps(
                            {k: queries._clip(v) for k, v in record.items()},
                            default=str)
                        self.wfile.write(f"event: log\ndata: {payload}\n\n".encode())
                        sent_any = True

                now = time.monotonic()
                if sent_any:
                    self.wfile.flush()
                    last_beat = now
                elif now - last_beat > 15:
                    # Comment frame: keeps proxies and the browser from
                    # deciding a quiet assistant is a dead connection.
                    self.wfile.write(b": beat\n\n")
                    self.wfile.flush()
                    last_beat = now
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab closed
        finally:
            if handle is not None:
                handle.close()


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, token: str, allowed_hosts: set):
        super().__init__(addr, _Handler)
        self.token = token
        self.allowed_hosts = allowed_hosts
        self.stopping = False

    def shutdown(self):
        self.stopping = True   # lets open SSE tails end their loop
        super().shutdown()


def build(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          token: str = "") -> PanelServer:
    token = token or resolve_token()[0]
    return PanelServer((host, port), token, _allowed_hosts(host))


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    token, generated = resolve_token()
    server = build(host, port, token)
    shown = host if ":" not in host else f"[{host}]"
    # flush=True: redirected to a file, stdout is block-buffered and the
    # URL — the only place the token is ever shown — sits invisible in a
    # buffer until the process exits.
    print(f"Kyraan panel — read-only — http://{shown}:{server.server_port}/"
          f"?token={token}", flush=True)
    if generated:
        print("(ephemeral token: this URL stops working when the panel "
              "restarts. Set KYRAAN_PANEL_TOKEN to keep one.)", flush=True)
    if host not in _LOOPBACK_HOSTS:
        print(f"WARNING: bound to {host}, not loopback. The panel reads the "
              "audit log, memory facts, and mail subjects — put it behind "
              "Tailscale rather than a forwarded port.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
