"""Dev-only local harness: exercises orchestrator.handle_message without a
Telegram bot, so intent normalization / reminders / Q&A can be tested
against the real configured model provider before Telegram creds exist.

Not part of the installed package — run directly: `python scripts/chat.py`

Limitation: reminders scheduled here use plain asyncio tasks, not the real
JobQueue, so `cancel_reminder` only removes the persisted record — an
already-scheduled asyncio task will still fire. Fine for a dev harness,
not for production use.
"""
import asyncio
from datetime import datetime

try:
    import readline  # noqa: F401 — enables input() history/line-editing on macOS/Linux
except ImportError:
    pass

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

from kyraan.agents import orchestrator
from kyraan.control_plane import kill_switch
from kyraan.triggers import scheduler

CHAT_ID = 0
console = Console()

HELP_TEXT = """\
**Slash commands**
- `/help` — this message
- `/reminders` — list pending reminders (no model call)
- `/kill` — engage the kill switch
- `/unkill` — disengage the kill switch
- `/clear` — clear the screen
- `/quit`, `/exit` — quit (or Ctrl-D)

Anything else is sent to the orchestrator, same as a real Telegram message.
"""


def schedule_fn(job_name: str, run_at: datetime, payload: dict) -> None:
    delay = max((run_at - datetime.now().astimezone()).total_seconds(), 0)

    async def fire_later():
        await asyncio.sleep(delay)
        await scheduler.fire(payload["reminder_id"], payload["chat_id"], payload["text"])

    asyncio.get_event_loop().create_task(fire_later())


def cancel_fn(job_name: str) -> None:
    pass  # best-effort no-op — see module docstring


async def send_fn(chat_id: int, text: str) -> None:
    console.print(f"\n[bold yellow]\U0001f514 {text}[/bold yellow]")


def _print_reminders() -> None:
    pending = scheduler.store.list_pending(CHAT_ID)
    if not pending:
        console.print("[dim]No pending reminders.[/dim]")
        return
    for r in pending:
        console.print(f"[dim]-[/dim] [cyan]{r.id[:8]}[/cyan] {r.text} [dim]at {r.when_iso}[/dim]")


def handle_slash(cmd: str) -> bool:
    """Returns True if `cmd` was a recognized slash command."""
    if cmd == "/help":
        console.print(Markdown(HELP_TEXT))
    elif cmd == "/clear":
        console.clear()
    elif cmd == "/kill":
        kill_switch.engage("engaged from local CLI")
        console.print("[bold red]Kill switch engaged.[/bold red] All skill execution and proactive sends are now blocked.")
    elif cmd == "/unkill":
        kill_switch.disengage()
        console.print("[bold green]Kill switch disengaged.[/bold green]")
    elif cmd == "/reminders":
        _print_reminders()
    else:
        return False
    return True


async def main() -> None:
    load_dotenv()
    scheduler.init(schedule_fn=schedule_fn, cancel_fn=cancel_fn, send_fn=send_fn)
    console.print("[bold cyan]Kyraan[/bold cyan] local CLI — real model calls, no Telegram needed.")
    console.print("Type [bold]/help[/bold] for commands, Ctrl-D to quit.\n")

    loop = asyncio.get_event_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, lambda: console.input("[bold green]>[/bold green] "))
        except EOFError:
            console.print("\n[dim]bye[/dim]")
            break

        text = text.strip()
        if not text:
            continue
        if text in ("/quit", "/exit"):
            console.print("[dim]bye[/dim]")
            break
        if text.startswith("/"):
            if not handle_slash(text):
                console.print(f"[dim]Unknown command: {text} (try /help)[/dim]")
            continue

        with console.status("[dim]thinking...[/dim]", spinner="dots"):
            reply = await orchestrator.handle_message(CHAT_ID, text)
        console.print(Markdown(reply))


if __name__ == "__main__":
    asyncio.run(main())
