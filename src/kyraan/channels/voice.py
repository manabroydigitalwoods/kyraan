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


def available() -> bool:
    if _cfg().get("enabled") is False:
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _transcribe_sync(path: str) -> str:
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
