"""Dev-only local TUI: same orchestrator.handle_message path as chat.py, but
a full-screen dashboard (session stats, per-turn provider/model/latency/
tokens, live sidebar, collapsible "Thought" sections) in the style of
Claude Code / OpenCode's terminal UI, built on Textual
(https://textual.textualize.io/).

Not part of the installed package — run directly: `python scripts/tui.py`

Threading note: router.call() is a blocking synchronous network call, so
each orchestrator call runs in a worker thread (via asyncio.to_thread) to
keep the UI responsive. Reminder scheduling still needs to land back on
this app's own persistent event loop (a fresh per-call event loop in a
worker thread would be destroyed before a delayed reminder ever fires), so
schedule_fn uses asyncio.run_coroutine_threadsafe(..., app_loop) — safe to
call from any thread, unlike scheduling on "whatever loop is current".

The chat area is a VerticalScroll of mounted widgets (Static for our own
Rich-markup UI text, Markdown for model-generated content, Collapsible for
reasoning) rather than a RichLog — a Collapsible is a real interactive
widget and can't be written into a text-only log stream, and Markdown
syntax needs a real Markdown renderer rather than Rich console markup.

Same reminder-cancellation limitation as chat.py: cancel_fn is a no-op, so
an already-scheduled reminder still fires even if cancelled from the store.
"""
import asyncio
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Collapsible, Footer, Header, Input, LoadingIndicator, Markdown, Static

from kyraan.agents import orchestrator
from kyraan.control_plane import config, kill_switch
from kyraan.control_plane.dnd import local_now
from kyraan.model_router import router
from kyraan.triggers import scheduler

def _build_stamp() -> str:
    """Git hash + time of the running code — a dev harness process outlives
    commits, and a stale one silently lacks the latest fixes. The stamp
    makes staleness visible at a glance."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h %ad", "--date=format:%H:%M"],
            capture_output=True, text=True, cwd=__file__.rsplit("/", 2)[0], timeout=3,
        ).stdout.strip()
        return f"build {out} — restart me after new commits"
    except Exception:
        return "build unknown"


CHAT_ID = 0
TRANSCRIPT_DIR = Path(__file__).resolve().parents[1] / "data" / "transcripts"

HELP_TEXT = """\
[b]Slash commands[/b]
  /help              this message
  /reminders         list pending reminders (no model call)
  /retry             resend the last message you sent
  /tier <t> <p> <m>  point tier t at provider p, model m (this session only,
                      e.g. /tier frontier openai gpt-5-nano)
  /tier              show current tier config
  /export            save the transcript to data/transcripts/
  /kill              engage the kill switch
  /unkill            disengage the kill switch
  /clear             clear the chat log
  /quit,/exit        quit (or Ctrl-C)

Anything else is sent to the orchestrator, same as a real Telegram message."""


class KyraanTUI(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #body {
        height: 1fr;
    }
    #chat-log {
        width: 3fr;
        border: solid $panel;
        padding: 0 1;
    }
    #sidebar {
        width: 1fr;
        border: solid $panel;
        padding: 1 2;
    }
    #chat-log LoadingIndicator {
        height: 1;
        width: auto;
        align: left top;
    }
    .user-message {
        background: $boost;
        border-left: thick $accent;
        padding: 0 1;
        margin: 1 0;
    }
    """
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.app_loop: asyncio.AbstractEventLoop | None = None
        self.session_start = local_now()
        self.message_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_user_message: str | None = None
        self.transcript_lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield VerticalScroll(id="chat-log")
            yield Static(id="sidebar")
        yield Input(placeholder="Ask anything, or /help for commands...", id="chat-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.app_loop = asyncio.get_running_loop()
        scheduler.init(schedule_fn=self._schedule_fn, cancel_fn=self._cancel_fn, send_fn=self._send_fn)
        self.title = "Kyraan"
        self.sub_title = f"local TUI — real model calls — {_build_stamp()}"
        await self._log("[bold cyan]Kyraan[/bold cyan] — type a message, or /help for commands.")
        self._refresh_sidebar()
        self.query_one("#chat-input", Input).focus()

    def _tier_line(self, tier: str) -> str:
        tier_cfg = config.load()["model_tiers"][tier]
        return f"{tier_cfg['provider']}/{tier_cfg['model']}"

    def _cost_line(self) -> str:
        spent = router.session_cost_usd
        budget = router.daily_budget_usd()
        pct = (spent / budget * 100) if budget else 0
        alert_pct = router.budget_alert_threshold_pct()
        text = f"${spent:.4f} / ${budget:.2f} ({pct:.0f}%)"
        if pct >= 100:
            return f"[bold red]{text}[/bold red]"
        if pct >= alert_pct:
            return f"[bold yellow]{text}[/bold yellow]"
        return text

    def _refresh_sidebar(self) -> None:
        elapsed = local_now() - self.session_start
        lines = [
            "[b]Session[/b]",
            f"started {self.session_start.strftime('%H:%M:%S')}",
            f"elapsed {str(elapsed).split('.')[0]}",
            f"messages {self.message_count}",
            f"kill switch {'[bold red]ENGAGED[/bold red]' if kill_switch.is_engaged() else '[green]off[/green]'}",
            "",
            "[b]Tiers[/b]",
            f"cheap    {self._tier_line('cheap')}",
            f"frontier {self._tier_line('frontier')}",
            "",
            "[b]Tokens (session)[/b]",
            f"in  {self.total_input_tokens}",
            f"out {self.total_output_tokens}",
            "",
            "[b]Cost (of daily budget)[/b]",
            self._cost_line(),
        ]
        last = router.last_call
        if last is not None:
            lines += [
                "",
                "[b]Last call[/b]",
                f"tier     {last.tier_used}",
                f"provider {last.provider}",
                f"model    {last.model}",
                f"latency  {last.latency_ms:.0f}ms",
                f"tokens   in={last.usage.input_tokens} out={last.usage.output_tokens}",
                f"cost     ${last.cost_usd:.4f}",
                f"reasoning {'yes' if last.reasoning else 'no'}",
            ]
        self.query_one("#sidebar", Static).update("\n".join(lines))

    async def _log(self, text: str) -> None:
        """Rich console markup (e.g. [bold red]...[/bold red]) — for our own
        UI/system messages, not model-generated content."""
        from rich.text import Text

        log = self.query_one("#chat-log", VerticalScroll)
        await log.mount(Static(text))
        log.scroll_end()
        self.transcript_lines.append(Text.from_markup(text).plain)

    async def _log_user(self, text: str) -> None:
        """The user's own message — boxed with a left accent bar (matching
        OpenCode's UI) instead of a bare `>` prefix, so a long conversation
        stays visually easy to scan for "who said this"."""
        log = self.query_one("#chat-log", VerticalScroll)
        await log.mount(Static(text, classes="user-message"))
        log.scroll_end()
        self.transcript_lines.append(f"> {text}")

    async def _log_markdown(self, text: str) -> None:
        """CommonMark — for actual model output, which may contain **bold**,
        lists, code fences, etc. that should render, not show as literal
        asterisks."""
        log = self.query_one("#chat-log", VerticalScroll)
        await log.mount(Markdown(text))
        log.scroll_end()
        self.transcript_lines.append(text)

    async def _log_thought(self, reasoning: str, latency_ms: float) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        await log.mount(Collapsible(Markdown(reasoning), title=f"Thought · {latency_ms:.0f}ms", collapsed=True))
        log.scroll_end()
        self.transcript_lines.append(f"<details><summary>Thought · {latency_ms:.0f}ms</summary>\n\n{reasoning}\n\n</details>")

    async def _show_thinking(self) -> LoadingIndicator:
        """Mount an inline spinner directly in the conversation flow, right
        where the reply will land — matches how OpenCode shows an in-place
        "Thinking" placeholder rather than a separate persistent status bar."""
        log = self.query_one("#chat-log", VerticalScroll)
        indicator = LoadingIndicator()
        await log.mount(indicator)
        log.scroll_end()
        return indicator

    def _schedule_fn(self, job_name: str, run_at: datetime, payload: dict) -> None:
        delay = max((run_at - local_now()).total_seconds(), 0)

        async def fire_later() -> None:
            await asyncio.sleep(delay)
            await scheduler.fire(payload["reminder_id"], payload["chat_id"], payload["text"])

        assert self.app_loop is not None
        asyncio.run_coroutine_threadsafe(fire_later(), self.app_loop)

    def _cancel_fn(self, job_name: str) -> None:
        pass  # best-effort no-op — see module docstring

    async def _send_fn(self, chat_id: int, text: str) -> None:
        await self._log(f"[bold yellow]\U0001f514 {text}[/bold yellow]")

    async def _call_orchestrator(self, text: str) -> str:
        def runner() -> str:
            return asyncio.run(orchestrator.handle_message(CHAT_ID, text))

        return await asyncio.to_thread(runner)

    async def _process_message(self, text: str) -> None:
        self.last_user_message = text
        await self._log_user(text)
        input_widget = self.query_one("#chat-input", Input)
        input_widget.disabled = True
        thinking = await self._show_thinking()
        try:
            reply = await self._call_orchestrator(text)
        finally:
            await thinking.remove()
            input_widget.disabled = False
            input_widget.focus()

        last = router.last_call
        if last is not None and last.reasoning:
            await self._log_thought(last.reasoning, last.latency_ms)
        await self._log_markdown(reply)
        if last is not None:
            cost_part = f" · ${last.cost_usd:.4f}" if last.cost_usd else ""
            await self._log(
                f"[dim]{last.tier_used} · {last.provider}/{last.model} · "
                f"{last.latency_ms:.0f}ms · in={last.usage.input_tokens} out={last.usage.output_tokens}{cost_part}[/dim]"
            )
            self.total_input_tokens += last.usage.input_tokens or 0
            self.total_output_tokens += last.usage.output_tokens or 0
        self.message_count += 1
        self._refresh_sidebar()

    @on(Input.Submitted, "#chat-input")
    async def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        input_widget = self.query_one("#chat-input", Input)
        input_widget.value = ""

        if not text:
            return
        if text in ("/quit", "/exit"):
            self.exit()
            return
        if text.startswith("/"):
            await self._handle_slash(text)
            return

        await self._process_message(text)

    async def _handle_slash(self, cmd: str) -> None:
        parts = cmd.split()
        command = parts[0]
        args = parts[1:]

        if command == "/help":
            await self._log(HELP_TEXT)
        elif command == "/clear":
            await self.query_one("#chat-log", VerticalScroll).remove_children()
            self.transcript_lines.clear()
        elif command == "/kill":
            kill_switch.engage("engaged from TUI")
            await self._log("[bold red]Kill switch engaged.[/bold red]")
        elif command == "/unkill":
            kill_switch.disengage()
            await self._log("[bold green]Kill switch disengaged.[/bold green]")
        elif command == "/reminders":
            pending = scheduler.store.list_pending(CHAT_ID)
            if not pending:
                await self._log("[dim]No pending reminders.[/dim]")
            else:
                for r in pending:
                    await self._log(f"[dim]-[/dim] [cyan]{r.id[:8]}[/cyan] {r.text} [dim]at {r.when_iso}[/dim]")
        elif command == "/retry":
            if self.last_user_message is None:
                await self._log("[dim]Nothing to retry yet.[/dim]")
            else:
                await self._process_message(self.last_user_message)
        elif command == "/tier":
            await self._handle_tier_command(args)
        elif command == "/export":
            await self._handle_export_command()
        else:
            await self._log(f"[dim]Unknown command: {command} (try /help)[/dim]")
        self._refresh_sidebar()

    async def _handle_tier_command(self, args: list[str]) -> None:
        if not args:
            tiers = config.load()["model_tiers"]
            lines = [f"{name}: {cfg['provider']}/{cfg['model']}" for name, cfg in tiers.items()]
            await self._log("[b]Current tiers[/b]\n" + "\n".join(lines))
            return
        if len(args) != 3:
            await self._log("[dim]Usage: /tier <tier> <provider> <model>  (or /tier with no args to show current)[/dim]")
            return
        tier, provider, model = args
        try:
            config.set_tier_override(tier, provider, model)
        except ValueError as exc:
            await self._log(f"[bold red]{exc}[/bold red]")
            return
        await self._log(f"[bold green]{tier}[/bold green] now points at {provider}/{model} (this session only).")

    async def _handle_export_command(self) -> None:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        path = TRANSCRIPT_DIR / f"{local_now().strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text("\n\n".join(self.transcript_lines))
        await self._log(f"[dim]Saved transcript to {path}[/dim]")


if __name__ == "__main__":
    load_dotenv()
    KyraanTUI().run()
