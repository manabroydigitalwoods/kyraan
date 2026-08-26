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
    # find_spec only LOCATES the package — importing mlx_whisper executes
    # native init, and a broken install SIGABRTs the whole process, which
    # no except clause can catch (Bugbot P1). The real import happens in
    # the transcription worker, at use time.
    import importlib.util
    return importlib.util.find_spec("mlx_whisper") is not None


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
