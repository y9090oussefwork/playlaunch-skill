"""PlayLaunch Studio shared core package (CWD-independent skill root)."""

from pathlib import Path

STUDIO_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = STUDIO_ROOT

__all__ = ["STUDIO_ROOT", "SKILL_ROOT"]
