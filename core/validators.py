"""Validation and path safety helpers for PlayLaunch Studio."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def validate_slug(slug: str) -> str:
    cleaned = slug.strip().lower()
    if not SLUG_RE.match(cleaned):
        raise ValueError(f"Invalid app slug '{slug}'. Expected lowercase alphanumeric/hyphen/underscore.")
    return cleaned


def validate_package_name(package_name: str) -> str:
    cleaned = package_name.strip()
    if not PACKAGE_NAME_RE.match(cleaned):
        raise ValueError(f"Invalid Android package name: '{package_name}'")
    return cleaned


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def assert_path_within(path: Path, allowed_root: Path) -> Path:
    """Guarantee that a write target stays strictly inside `allowed_root`."""
    resolved_target = path.resolve()
    resolved_root = allowed_root.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PermissionError(
            f"Write target '{resolved_target}' escapes allowed root '{resolved_root}'."
        ) from exc
    return resolved_target
