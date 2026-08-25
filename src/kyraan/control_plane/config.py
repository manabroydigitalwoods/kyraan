"""Loads config/permissions.yaml once and exposes typed lookups."""
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "permissions.yaml"

# In-memory tier overrides — e.g. the TUI's /tier command, so a dev can try
# a different provider/model without editing the YAML file and restarting.
# Process-lifetime only, never written back to disk.
_tier_overrides: dict[str, dict] = {}


@lru_cache
def _load_file() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def load() -> dict:
    cfg = _load_file()
    if not _tier_overrides:
        return cfg
    cfg = dict(cfg)
    cfg["model_tiers"] = {**cfg["model_tiers"], **_tier_overrides}
    return cfg


def skill_config(skill_name: str) -> dict:
    cfg = load()
    return cfg["skills"].get(skill_name, cfg["defaults"])


def reload() -> None:
    """Drop the file cache — call after editing permissions.yaml at runtime."""
    _load_file.cache_clear()


def set_tier_override(tier: str, provider: str, model: str) -> None:
    """Point a tier at a different provider/model for the rest of this
    process, without touching permissions.yaml. Raises for an unknown tier
    name or a provider that isn't registered — this only repoints an
    existing tier, it doesn't add a new provider (that still requires
    editing the config's `providers` section and providing its API key)."""
    base_cfg = _load_file()
    if tier not in base_cfg["model_tiers"]:
        raise ValueError(f"Unknown tier {tier!r} — must be one of {sorted(base_cfg['model_tiers'])}")
    if provider not in base_cfg["providers"]:
        raise ValueError(f"Unknown provider {provider!r} — add it to config/permissions.yaml's providers section first")
    _tier_overrides[tier] = {"provider": provider, "model": model}


def clear_tier_overrides() -> None:
    _tier_overrides.clear()
