"""Configuration loader for PlayLaunch Studio.

Loads config/studio.config.json (if present) layered over config/studio.config.example.json
so standalone machines without local config work cleanly with zero errors.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from core import STUDIO_ROOT

CONFIG_DIR = STUDIO_ROOT / "config"
LOCAL_CONFIG_PATH = CONFIG_DIR / "studio.config.json"
EXAMPLE_CONFIG_PATH = CONFIG_DIR / "studio.config.example.json"
POLICIES_PATH = CONFIG_DIR / "play_policies.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def expand_win_path(raw: str | None) -> Path | None:
    """Expand Windows environment variables (%LOCALAPPDATA%, etc.) and user home."""
    if not raw or not str(raw).strip():
        return None
    expanded = os.path.expandvars(os.path.expanduser(str(raw).strip()))
    return Path(expanded)


def load_studio_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load merged studio configuration."""
    base: dict[str, Any] = {}
    if EXAMPLE_CONFIG_PATH.exists():
        base = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))

    target = config_path if config_path is not None else LOCAL_CONFIG_PATH
    if target.exists():
        override = json.loads(target.read_text(encoding="utf-8"))
        return _deep_merge(base, override)
    return base


def load_play_policies(policies_path: Path | None = None) -> dict[str, Any]:
    """Load Google Play Store policy limits and Vitals thresholds."""
    target = policies_path if policies_path is not None else POLICIES_PATH
    if not target.exists():
        raise FileNotFoundError(f"Play policies file not found: {target}")
    return json.loads(target.read_text(encoding="utf-8"))


def get_audio_workspace_root(config: dict[str, Any] | None = None) -> Path | None:
    """Return configured Youssef_Audio_Workspace path if enabled and present on disk."""
    cfg = config if config is not None else load_studio_config()
    aw = cfg.get("audio_workspace", {})
    if not aw.get("enabled", False):
        return None
    root = expand_win_path(aw.get("root_path"))
    if root and root.exists() and root.is_dir():
        return root
    return None
