"""The second channel (§3d #5, audit top-10 risk, 2026-09-01): a local
terminal REPL, so Telegram being down never means Kyraan is mute — the
whole brain lives on this machine.

    .venv/bin/python -m kyraan.channels.cli

Scope, deliberately narrow (the dev-harness lessons apply):
- CONVERSATION ONLY. This process never calls scheduler/agent_tasks/
  goals/briefs init — the Telegram bot owns every proactive job, so a
  CLI session can never steal a reminder or double-fire a cycle (the
  audit-era TUI bug class, closed by construction here).
- Same owner chat id, so memory, documents, goals, and history are the
  SAME Kyraan you talk to on the phone (Redis session state is shared
  across processes since P3.4a); the confirm flow works by typing
  "yes"/"no" like any message.
- Owner viewer, fail-closed: no TELEGRAM_OWNER_ID, no session.
"""
import asyncio
import os
import sys


def _owner_chat() -> int:
    raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    if not raw.lstrip("-").isdigit():
        raise SystemExit("TELEGRAM_OWNER_ID missing from the environment — "
                         "run from the repo root with .env loaded")
    return int(raw)


async def repl() -> None:
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel

    chat_id = _owner_chat()
    kernel.set_viewer("owner", "owner")
    print("Kyraan CLI — same brain, same memory as Telegram. "
          "Ctrl-D to leave.")
    while True:
        try:
            text = await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        text = text.strip()
        if not text:
            continue
        try:
            reply = await orchestrator.handle_message(chat_id, text)
        except Exception as exc:  # the channel survives any turn's crash
            reply = f"(turn failed: {exc})"
        print(f"kyraan> {reply}\n")


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(repl())


if __name__ == "__main__":
    main()
