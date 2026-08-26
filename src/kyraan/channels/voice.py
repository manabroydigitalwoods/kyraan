"""Local voice-note transcription — speech becomes text BEFORE the brain.

Whisper (large-v3-turbo via Apple-MLX) runs on this machine's GPU: the
audio never leaves the Mac, consistent with every privacy boundary in the
system. By the time the agent loop sees a voice note it IS text, so every
guard, gate, and invariant applies unchanged.

The model (~1.6GB) downloads on first use and loads lazily; transcription
runs in a worker thread (the G-01 rule: nothing blocks the event loop).
"""
import asyncio
from pathlib import Path

from kyraan.control_plane import config
from kyraan.control_plane.logging_setup import log_event

_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


def _cfg() -> dict:
    return (config.load().get("voice") or {})


_native_probe: bool | None = None  # cached for the process lifetime
_probe_thread = None


def _run_probe() -> None:
    """Import mlx_whisper in a SACRIFICIAL SUBPROCESS: a broken native
    install SIGABRTs the child, not the bot — no in-process import can be
    guarded any other way, because SIGABRT is not an exception (audit
    rounds 1+2, P1)."""
    global _native_probe
    import subprocess
    import sys
    try:
        _native_probe = subprocess.run(
            [sys.executable, "-c", "import mlx_whisper"],
            capture_output=True, timeout=90).returncode == 0
    except Exception:
        _native_probe = False
    if not _native_probe:
        log_event("voice_native_probe_failed")


def _native_import_ok() -> bool:
    """Non-blocking: the probe runs in a daemon thread; until it reports,
    voice counts as unavailable (a synchronous subprocess here blocked
    the event loop for the probe's duration — audit round 3, P2). The
    capability brief self-corrects on the next turn."""
    global _probe_thread
    if _native_probe is not None:
        return _native_probe
    if _probe_thread is None:
        import threading
        _probe_thread = threading.Thread(target=_run_probe, daemon=True)
        _probe_thread.start()
    return False


def available() -> bool:
    if _cfg().get("enabled") is False:
        return False
    import importlib.util
    if importlib.util.find_spec("mlx_whisper") is None:
        return False
    return _native_import_ok()


def start_probe() -> None:
    """Kick the background probe eagerly (bot startup calls this) so the
    verdict exists before the first voice note can arrive — without it,
    a voice note as the FIRST message after startup was rejected on a
    healthy install because the probe was still running (Bugbot P2)."""
    if _cfg().get("enabled") is False:
        return
    import importlib.util
    if importlib.util.find_spec("mlx_whisper") is not None:
        _native_import_ok()


async def wait_available(timeout: float = 90.0) -> bool:
    """available(), but a voice note in hand is worth waiting for: joins
    the in-flight probe (in a worker thread — the event loop stays free)
    instead of treating "still probing" as "unavailable"."""
    if _cfg().get("enabled") is False:
        return False
    import importlib.util
    if importlib.util.find_spec("mlx_whisper") is None:
        return False
    if _native_import_ok():
        return True
    if _probe_thread is not None:
        await asyncio.to_thread(_probe_thread.join, timeout)
    return bool(_native_probe)


def _transcribe_sync(path: str) -> str:
    import os
    import shutil

    # launchd services get a bare PATH without /opt/homebrew/bin — the
    # first three live voice notes all failed with "ffmpeg not found"
    # while shell-run tests passed. Make the dependency explicit.
    if shutil.which("ffmpeg") is None:
        extras = ("/opt/homebrew/bin", "/usr/local/bin")
        os.environ["PATH"] = os.pathsep.join(
            [*extras, os.environ.get("PATH", "")])
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "ffmpeg not found — install it (brew install ffmpeg); voice "
                "notes need it to decode Telegram's audio")

    import mlx_whisper

    result = mlx_whisper.transcribe(
        path, path_or_hf_repo=_cfg().get("model", _DEFAULT_MODEL))
    return str(result.get("text", "")).strip()


async def transcribe(path: Path) -> str:
    """Transcribed text, '' when nothing intelligible. Errors are logged
    and surface as '' — the caller owns the honest user-facing reply."""
    try:
        text = await asyncio.to_thread(_transcribe_sync, str(path))
        log_event("voice_transcribed", chars=len(text))
        return text
    except Exception as exc:
        log_event("voice_transcription_error", error=str(exc)[:200],
                  error_type=type(exc).__name__)
        return ""
