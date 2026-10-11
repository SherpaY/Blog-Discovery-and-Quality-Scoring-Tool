"""Configuration loading: shipped defaults (config.yaml) + local overrides (data/user_config.yaml)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
USER_CONFIG_PATH = PROJECT_ROOT / "data" / "user_config.yaml"


def deep_merge(base: dict, override: dict) -> dict:
    """Return a copy of base with override applied recursively (lists are replaced)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_config(path: str | Path | None = None, user_path: str | Path | None = None) -> dict:
    """Load defaults, then overlay the user's saved settings."""
    base = _read_yaml(Path(path) if path else DEFAULT_CONFIG_PATH)
    user = _read_yaml(Path(user_path) if user_path else USER_CONFIG_PATH)
    return deep_merge(base, user)


def save_user_config(config: dict, user_path: str | Path | None = None) -> Path:
    target = Path(user_path) if user_path else USER_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, sort_keys=False, allow_unicode=True)
    return target


def resolve_path(config: dict, key: str) -> Path:
    """Resolve a configured directory (data_dir/output_dir/log_dir) relative to the project root."""
    raw = (config.get("paths") or {}).get(key) or key.replace("_dir", "")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def get(config: dict, dotted: str, default: Any = None) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
