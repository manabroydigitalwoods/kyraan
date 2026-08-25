"""Loads config/permissions.yaml once and exposes typed lookups."""
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "permissions.yaml"


@lru_cache
def load() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def skill_config(skill_name: str) -> dict:
    cfg = load()
    return cfg["skills"].get(skill_name, cfg["defaults"])


def model_for_tier(tier: str) -> str:
    return load()["model_tiers"][tier]["model"]


def reload() -> None:
    """Drop the cache — call after editing permissions.yaml at runtime."""
    load.cache_clear()
