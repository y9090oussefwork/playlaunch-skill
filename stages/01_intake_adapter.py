"""Stage 1 — Dual-Mode Intake Adapter (01_intake_adapter.py).

Supports:
  - Mode A (`audio_workspace`): Strictly READ-ONLY integration with D:\\Projects\\Youssef_Audio_Workspace.
    Zero files are ever created or modified inside Youssef_Audio_Workspace.
  - Mode B (`standalone`): Any independent Kotlin, Java, or Flutter app from profiles/apps/<slug>.json.
    Works seamlessly on machines where Youssef_Audio_Workspace does not exist.

All outputs are written exclusively inside workspace_runs/<app-slug>/<run-timestamp>/.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

# CWD-independent skill root resolution (works from any external Android/Flutter folder)
SKILL_ROOT = Path(__file__).resolve().parent.parent
STUDIO_ROOT = SKILL_ROOT
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from core.config_loader import expand_win_path, get_audio_workspace_root, load_studio_config
from core.env_resolver import build_jvm_env, resolve_environment
from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso
from core.validators import sha256_file, validate_package_name, validate_slug

logger = get_logger("stage.01_intake")

VALID_FRAMEWORKS = frozenset({"kotlin", "java", "flutter"})


def _read_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_account_profile(
    account_id: str | None,
    explicit_path: Path | None = None,
    studio_root: Path = STUDIO_ROOT,
) -> tuple[dict[str, Any], list[str]]:
    """Load developer account profile (never exposes secret key file contents)."""
    warnings: list[str] = []
    candidate: Path | None = explicit_path
    if candidate is None and account_id:
        real_path = studio_root / "profiles" / "accounts" / f"{account_id}.json"
        example_path = studio_root / "profiles" / "accounts" / f"{account_id}.example.json"
        if real_path.exists():
            candidate = real_path
        elif example_path.exists():
            candidate = example_path

    if candidate and candidate.exists():
        raw = _read_json_file(candidate)
        return (
            {
                "account_id": raw.get("account_id", account_id or "unknown"),
                "display_name": raw.get("display_name", account_id or "Unknown Account"),
                "publish_mode": raw.get("publish_mode", "manual_bundle"),
                "default_track": raw.get("default_track", "draft"),
                "network_isolation": raw.get("network_isolation", {"mode": "router_reboot", "strict_subnet_block": True}),
                "profile_path": str(candidate),
                "profile_found": True,
            },
            warnings,
        )

    warnings.append(
        f"Account profile for '{account_id or 'default'}' not found in profiles/accounts/; "
        "defaulting safely to publish_mode='manual_bundle'."
    )
    return (
        {
            "account_id": account_id or "unassigned",
            "display_name": account_id or "Unassigned Account",
            "publish_mode": "manual_bundle",
            "default_track": "draft",
            "network_isolation": {"mode": "router_reboot", "strict_subnet_block": True},
            "profile_path": None,
            "profile_found": False,
        },
        warnings,
    )


def verify_aab_signature(aab_path: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify .aab signature using Android Studio JBR jarsigner (read-only)."""
    env_info = resolve_environment(config=config)
    if not env_info.jarsigner_bin:
        return {
            "verified": False,
            "tool": None,
            "java_source": env_info.java_source,
            "detail": "jarsigner binary not found in resolved JAVA_HOME.",
        }

    jvm_env = build_jvm_env(config=config)
    try:
        proc = subprocess.run(
            [env_info.jarsigner_bin, "-verify", str(aab_path)],
            capture_output=True,
            text=True,
            timeout=30,
            env=jvm_env,
            check=False,
        )
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        verified = proc.returncode == 0 and "jar verified" in combined.lower()
        return {
            "verified": verified,
            "tool": env_info.jarsigner_bin,
            "java_source": env_info.java_source,
            "java_version": env_info.java_version,
            "returncode": proc.returncode,
            "detail": combined[:500],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "verified": False,
            "tool": env_info.jarsigner_bin,
            "java_source": env_info.java_source,
            "detail": f"jarsigner invocation error: {exc}",
        }


def _find_audio_app_dir(workspace_root: Path, app_slug: str | None, flavor: str | None) -> tuple[str, Path, dict[str, Any]]:
    apps_root = workspace_root / "03_App_Configurations"
    if not apps_root.exists():
        raise FileNotFoundError(f"Audio workspace app configs directory not found: {apps_root}")

    if app_slug:
        slug = validate_slug(app_slug)
        app_dir = apps_root / slug
        app_json_path = app_dir / "app.json"
        if not app_json_path.exists():
            raise FileNotFoundError(f"Audio workspace app.json not found for slug '{slug}': {app_json_path}")
        return slug, app_dir, _read_json_file(app_json_path)

    if flavor:
        target_flavor = flavor.strip().lower()
        for child in sorted(apps_root.iterdir()):
            app_json_path = child / "app.json"
            if child.is_dir() and app_json_path.exists():
                data = _read_json_file(app_json_path)
                if str(data.get("flavor", "")).strip().lower() == target_flavor:
                    return str(data.get("slug", child.name)), child, data
        raise FileNotFoundError(f"No app with flavor '{flavor}' found in {apps_root}")

    raise ValueError("Mode A (audio_workspace) requires --app-slug or --flavor.")


def _parse_audio_catalog(music_data_path: Path) -> dict[str, Any]:
    raw = _read_json_file(music_data_path)
    artists = [str(a.get("name", "")).strip() for a in raw.get("artists", []) if isinstance(a, dict) and a.get("name")]
    categories = [str(c.get("name", "")).strip() for c in raw.get("categories", []) if isinstance(c, dict) and c.get("name")]
    songs_raw = raw.get("songs", [])
    top_tracks: list[str] = []
    for song in songs_raw:
        if isinstance(song, dict) and song.get("title"):
            top_tracks.append(str(song["title"]).strip())
            if len(top_tracks) >= 30:
                break
    app_block = raw.get("application", {}) if isinstance(raw.get("application"), dict) else {}
    return {
        "available": True,
        "source_path": str(music_data_path),
        "api_version": raw.get("api_version"),
        "catalog_revision": raw.get("catalog_revision"),
        "app_version": app_block.get("version"),
        "catalog_app_name": app_block.get("name"),
        "artist_names": list(dict.fromkeys(artists)),
        "category_names": list(dict.fromkeys(categories)),
        "song_count": len(songs_raw) if isinstance(songs_raw, list) else 0,
        "top_tracks": top_tracks,
    }


def _find_aab_via_local_api(api_url: str, app_slug: str, flavor: str) -> tuple[Path | None, str | None]:
    endpoint = api_url.rstrip("/") + "/api/agent/jobs"
    try:
        resp = requests.get(endpoint, timeout=1.5)
        if resp.status_code != 200:
            return None, None
        payload = resp.json()
    except Exception:
        return None, None

    jobs = payload if isinstance(payload, list) else payload.get("jobs", []) if isinstance(payload, dict) else []
    slug_l = app_slug.lower()
    flavor_l = flavor.lower()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("job_type") != "android_build_aab":
            continue
        if job.get("local_status") != "completed":
            continue
        if str(job.get("build_variant", "")).lower() != "release":
            continue
        j_slug = str(job.get("app_slug") or job.get("slug") or "").lower()
        j_flavor = str(job.get("flavor") or "").lower()
        if j_slug not in ("", slug_l) and j_flavor not in ("", flavor_l):
            continue
        if not j_slug and not j_flavor:
            continue
        raw_path = job.get("aab_path") or job.get("artifact_path") or job.get("output_path")
        if raw_path:
            p = Path(str(raw_path))
            if p.exists() and p.suffix.lower() == ".aab":
                return p, f"local_api:{endpoint}"
    return None, None


def _find_aab_via_sqlite_ro(sqlite_path: Path | None, app_slug: str, flavor: str) -> tuple[Path | None, str | None]:
    if not sqlite_path or not sqlite_path.exists():
        return None, None

    uri = f"file:{quote(str(sqlite_path.resolve()), safe=':/\\')}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        target_table = "agent_jobs" if "agent_jobs" in tables else (tables[0] if tables else None)
        if not target_table:
            return None, None

        cols = {r[1] for r in cur.execute(f'PRAGMA table_info("{target_table}")').fetchall()}
        required = {"job_type", "local_status", "build_variant"}
        if not required.issubset(cols):
            return None, None

        order_col = "updated_at" if "updated_at" in cols else ("created_at" if "created_at" in cols else "rowid")
        rows = cur.execute(
            f'SELECT * FROM "{target_table}" '
            "WHERE job_type = 'android_build_aab' "
            "AND local_status = 'completed' "
            "AND lower(build_variant) = 'release' "
            f'ORDER BY "{order_col}" DESC'
        ).fetchall()

        slug_l = app_slug.lower()
        flavor_l = flavor.lower()
        for row in rows:
            rd = dict(row)
            r_slug = str(rd.get("app_slug") or rd.get("slug") or "").lower()
            r_flavor = str(rd.get("flavor") or "").lower()
            if r_slug != slug_l and r_flavor != flavor_l:
                continue
            for key in ("aab_path", "artifact_path", "output_path", "release_aab_path"):
                val = rd.get(key)
                if val:
                    p = Path(str(val))
                    if p.exists() and p.suffix.lower() == ".aab":
                        return p, f"sqlite_ro:{sqlite_path}"
    except sqlite3.Error:
        return None, None
    finally:
        if conn is not None:
            conn.close()
    return None, None


def _find_aab_via_filesystem(workspace_root: Path, app_slug: str, flavor: str, config: dict[str, Any]) -> tuple[Path | None, str | None]:
    search_patterns: list[tuple[Path, str, str]] = [
        (workspace_root / "05_Releases" / "agent-builds", f"*/{flavor}/release/*.aab", "workspace_agent_builds"),
        (workspace_root / "05_Releases" / "android" / "production" / app_slug, "**/*.aab", "workspace_android_production"),
    ]
    local_app_data = expand_win_path(r"%LOCALAPPDATA%\YoussefAudioPlatform\app-manager\releases\agent-builds")
    if local_app_data:
        search_patterns.append((local_app_data, f"*/{flavor}/release/*.aab", "localappdata_agent_builds"))

    found: list[tuple[float, Path, str]] = []
    for base_dir, pattern, tag in search_patterns:
        if not base_dir.exists():
            continue
        for candidate in base_dir.glob(pattern):
            if candidate.is_file() and candidate.suffix.lower() == ".aab":
                found.append((candidate.stat().st_mtime, candidate, f"fs:{tag}"))

    if not found:
        return None, None
    found.sort(key=lambda item: item[0], reverse=True)
    _, best_path, source_tag = found[0]
    return best_path, source_tag


def locate_audio_workspace_aab(
    workspace_root: Path,
    app_slug: str,
    flavor: str,
    config: dict[str, Any],
    verify_signature: bool = True,
) -> dict[str, Any]:
    """Locate latest signed release .aab using blueprint priority: Local API -> SQLite RO -> FS scan."""
    aw_cfg = config.get("audio_workspace", {})
    api_url = str(aw_cfg.get("local_api_url") or "http://127.0.0.1:5000")
    sqlite_path = expand_win_path(aw_cfg.get("agent_jobs_sqlite_path"))

    aab_path, source = _find_aab_via_local_api(api_url, app_slug, flavor)
    if not aab_path:
        aab_path, source = _find_aab_via_sqlite_ro(sqlite_path, app_slug, flavor)
    if not aab_path:
        aab_path, source = _find_aab_via_filesystem(workspace_root, app_slug, flavor, config)

    if not aab_path:
        return {
            "found": False,
            "path": None,
            "discovery_source": None,
            "size_bytes": 0,
            "sha256": None,
            "signature_verified": False,
            "signature_check": None,
        }

    sig_result = verify_aab_signature(aab_path, config=config) if verify_signature else {
        "verified": False,
        "skipped": True,
        "detail": "Signature verification skipped by flag.",
    }
    return {
        "found": True,
        "path": str(aab_path.resolve()),
        "discovery_source": source,
        "size_bytes": aab_path.stat().st_size,
        "sha256": sha256_file(aab_path),
        "signature_verified": bool(sig_result.get("verified", False)),
        "signature_check": sig_result,
    }


def intake_audio_workspace(
    config: dict[str, Any],
    app_slug: str | None = None,
    flavor: str | None = None,
    locales_override: list[str] | None = None,
    account_profile_path: Path | None = None,
    verify_signature: bool = True,
    studio_root: Path = STUDIO_ROOT,
) -> tuple[dict[str, Any], list[str]]:
    """Read-only Mode A intake from Youssef_Audio_Workspace."""
    warnings: list[str] = []
    workspace_root = get_audio_workspace_root(config)
    if not workspace_root:
        raise FileNotFoundError(
            "Audio workspace is not configured or not found on disk. "
            "Check config/studio.config.json -> audio_workspace.root_path or use --mode standalone."
        )

    ws_json_path = workspace_root / "workspace.json"
    ws_meta = _read_json_file(ws_json_path) if ws_json_path.exists() else {}
    if not ws_json_path.exists():
        warnings.append(f"workspace.json not found at {ws_json_path}; proceeding with default layout.")

    resolved_slug, app_dir, app_json = _find_audio_app_dir(workspace_root, app_slug, flavor)
    resolved_flavor = str(app_json.get("flavor") or flavor or resolved_slug)
    package_name = validate_package_name(str(app_json["package_name"]))
    developer_account = str(app_json.get("developer_account") or "")

    branding_path = app_dir / "branding" / "branding.json"
    branding_json = _read_json_file(branding_path) if branding_path.exists() else {}
    if not branding_path.exists():
        warnings.append(f"branding.json missing at {branding_path}")

    listing_path = app_dir / "store" / "listing.json"
    listing_json = _read_json_file(listing_path) if listing_path.exists() else {}
    if not listing_path.exists():
        warnings.append(f"listing.json missing at {listing_path}")

    # Resolve Android source set (strictly read-only)
    rel_source_set = app_json.get("android_source_set")
    source_set_candidates: list[Path] = []
    if rel_source_set:
        source_set_candidates.append(workspace_root / Path(str(rel_source_set)))
    default_flavor_src = workspace_root / "01_Platform_Source" / "apps" / "android" / "app" / "src" / resolved_flavor
    if default_flavor_src not in source_set_candidates:
        source_set_candidates.append(default_flavor_src)

    # Read audio catalog (music_data.json)
    music_data_path: Path | None = None
    for src_root in source_set_candidates:
        candidate = src_root / "assets" / "offline" / "music_data.json"
        if candidate.exists():
            music_data_path = candidate
            break

    if music_data_path:
        audio_catalog = _parse_audio_catalog(music_data_path)
    else:
        warnings.append(
            f"music_data.json not found under flavor '{resolved_flavor}' "
            f"(checked: {', '.join(str(p) for p in source_set_candidates)}). "
            "ASO will rely on listing/app metadata until offline catalog is synced."
        )
        audio_catalog = {
            "available": False,
            "source_path": None,
            "artist_names": [listing_json["artist_name"]] if listing_json.get("artist_name") else [],
            "category_names": [],
            "song_count": 0,
            "top_tracks": [],
        }

    # Locate launcher icon (read-only)
    icon_path: Path | None = None
    for src_root in source_set_candidates:
        for icon_name in ("ic_launcher.png", "ic_launcher.webp"):
            cand = src_root / "res" / "mipmap-xxxhdpi" / icon_name
            if cand.exists():
                icon_path = cand
                break
        if icon_path:
            break
    if not icon_path:
        warnings.append(
            f"Local mipmap-xxxhdpi launcher icon not found for flavor '{resolved_flavor}'; "
            "using icon_source_url from branding.json if available."
        )

    # Locate latest signed release .aab
    aab_info = locate_audio_workspace_aab(
        workspace_root=workspace_root,
        app_slug=resolved_slug,
        flavor=resolved_flavor,
        config=config,
        verify_signature=verify_signature,
    )
    if not aab_info["found"]:
        warnings.append(
            f"No release .aab found for '{resolved_slug}' (flavor '{resolved_flavor}') via Local API, SQLite RO, or 05_Releases."
        )
    elif not aab_info["signature_verified"]:
        warnings.append("Release .aab was located, but signature verification did not pass; Stage 2 will NOT auto-skip.")

    account_info, acc_warnings = _resolve_account_profile(
        account_id=developer_account or None,
        explicit_path=account_profile_path,
        studio_root=studio_root,
    )
    warnings.extend(acc_warnings)

    target_locales = locales_override or list(config.get("defaults", {}).get("target_locales", ["ar", "en-US"]))
    accounts_layout = ws_meta.get("layout", {}).get("accounts", "02_Developer_Accounts")

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "mode": "audio_workspace",
        "read_only_source": True,
        "app": {
            "app_id": app_json.get("app_id"),
            "slug": resolved_slug,
            "display_name": app_json.get("display_name") or listing_json.get("runtime_display_name") or resolved_slug,
            "runtime_display_name": listing_json.get("runtime_display_name"),
            "device_display_name": listing_json.get("device_display_name"),
            "artist_name": listing_json.get("artist_name"),
            "package_name": package_name,
            "flavor": resolved_flavor,
            "framework": "kotlin",
            "primary_category": "MUSIC_AND_AUDIO",
            "target_locales": target_locales,
        },
        "branding": {
            "accent_color": branding_json.get("theme_accent_color", "#1DB954"),
            "icon_local_path": str(icon_path) if icon_path else None,
            "icon_source_url": branding_json.get("icon_source_url") or None,
            "splash_logo_source_url": branding_json.get("splash_logo_source_url") or None,
        },
        "audio_catalog": audio_catalog,
        "release_artifact": aab_info,
        "stage2_auto_skip": bool(aab_info["found"] and aab_info["signature_verified"]),
        "account": account_info,
        "audio_workspace_refs": {
            "workspace_root": str(workspace_root),
            "app_config_dir": str(app_dir),
            "local_api_url": str(config.get("audio_workspace", {}).get("local_api_url", "http://127.0.0.1:5000")),
            "developer_account_folder": developer_account,
            "planned_export_targets": {
                "store_assets_dir": str(
                    workspace_root / accounts_layout / (developer_account or "unassigned") / "store-assets" / resolved_slug
                ),
                "release_notes_dir": str(
                    workspace_root / accounts_layout / (developer_account or "unassigned") / "release-notes" / resolved_slug
                ),
            },
        },
        "warnings": warnings,
    }
    return manifest, warnings


def _slugify_folder_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    return cleaned or "standalone-app"


def _auto_detect_project_metadata(project_dir: Path, app_slug_override: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Auto-discover standalone Kotlin/Java/Flutter metadata directly from --project-dir (read-only)."""
    warnings: list[str] = []
    resolved = project_dir.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise FileNotFoundError(f"--project-dir does not exist or is not a directory: {resolved}")

    is_flutter = (resolved / "pubspec.yaml").exists()
    android_root = (resolved / "android") if is_flutter and (resolved / "android").exists() else resolved
    app_module_dir = android_root / "app" if (android_root / "app").exists() else android_root

    # Detect framework
    if is_flutter:
        framework = "flutter"
    else:
        has_kt = any(app_module_dir.rglob("*.kt")) if app_module_dir.exists() else False
        has_kts = (app_module_dir / "build.gradle.kts").exists() or (android_root / "build.gradle.kts").exists()
        framework = "kotlin" if (has_kt or has_kts) else "java"

    # Detect applicationId / package_name
    package_name: str | None = None
    for gradle_cand in (
        app_module_dir / "build.gradle.kts",
        app_module_dir / "build.gradle",
        android_root / "build.gradle.kts",
        android_root / "build.gradle",
    ):
        if gradle_cand.exists():
            txt = gradle_cand.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'applicationId\s*=?\s*["\']([A-Za-z][A-Za-z0-9_.]+)["\']', txt)
            if not m:
                m = re.search(r'namespace\s*=?\s*["\']([A-Za-z][A-Za-z0-9_.]+)["\']', txt)
            if m:
                package_name = m.group(1).strip()
                break

    if not package_name:
        manifest_cand = app_module_dir / "src" / "main" / "AndroidManifest.xml"
        if manifest_cand.exists():
            txt = manifest_cand.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'package\s*=\s*["\']([A-Za-z][A-Za-z0-9_.]+)["\']', txt)
            if m:
                package_name = m.group(1).strip()

    slug = validate_slug(app_slug_override or _slugify_folder_name(resolved.name))
    if not package_name:
        safe_pkg_tail = re.sub(r"[^a-z0-9]", "", slug.lower()) or "app"
        package_name = f"com.example.{safe_pkg_tail}"
        warnings.append(
            f"Could not auto-detect applicationId in {resolved}; defaulted to '{package_name}'. "
            "Pass --app-profile or configure applicationId in build.gradle(.kts)."
        )

    # Detect display name from Android strings.xml or Flutter pubspec.yaml
    display_name = resolved.name
    strings_xml = app_module_dir / "src" / "main" / "res" / "values" / "strings.xml"
    if strings_xml.exists():
        stxt = strings_xml.read_text(encoding="utf-8", errors="ignore")
        m_name = re.search(r'<string\s+name=["\']app_name["\'][^>]*>([^<]+)</string>', stxt)
        if m_name and m_name.group(1).strip():
            display_name = m_name.group(1).strip()

    app_summary = ""
    if is_flutter:
        ptxt = (resolved / "pubspec.yaml").read_text(encoding="utf-8", errors="ignore")
        m_desc = re.search(r"^description:\s*(.+)$", ptxt, re.MULTILINE)
        if m_desc:
            app_summary = m_desc.group(1).strip().strip("\"'")
        if display_name == resolved.name:
            m_pname = re.search(r"^name:\s*([A-Za-z0-9_-]+)", ptxt, re.MULTILINE)
            if m_pname:
                display_name = m_pname.group(1).replace("_", " ").title()

    # Auto-detect launcher icon (read-only)
    icon_path = ""
    for ic in (
        app_module_dir / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher.png",
        app_module_dir / "src" / "main" / "res" / "mipmap-xxhdpi" / "ic_launcher.png",
        app_module_dir / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher.webp",
    ):
        if ic.exists():
            icon_path = str(ic)
            break

    return (
        {
            "slug": slug,
            "display_name": display_name,
            "package_name": package_name,
            "framework": framework,
            "source_path": str(resolved),
            "android_module": "app",
            "account_id": None,
            "brand": {"accent_color": "#2563EB", "background_color": "#0F172A", "icon_path": icon_path},
            "store_seed": {
                "app_summary": app_summary or f"{display_name} Android application",
                "seed_keywords": [display_name],
            },
            "signing": {"verify_signature": True},
        },
        warnings,
    )


def intake_standalone(
    config: dict[str, Any],
    app_slug: str | None = None,
    app_profile_path: Path | None = None,
    project_dir: Path | None = None,
    locales_override: list[str] | None = None,
    account_profile_path: Path | None = None,
    studio_root: Path = STUDIO_ROOT,
) -> tuple[dict[str, Any], list[str]]:
    """Mode B intake for any standalone Kotlin, Java, or Flutter app (via profile OR --project-dir)."""
    warnings: list[str] = []

    profile_file: Path | None = app_profile_path
    if project_dir is not None and profile_file is None:
        raw, detect_warnings = _auto_detect_project_metadata(project_dir, app_slug_override=app_slug)
        warnings.extend(detect_warnings)
        profile_label = f"auto_detected:{project_dir.resolve()}"
    else:
        if profile_file is None:
            if not app_slug:
                raise ValueError("Mode B (standalone) requires --project-dir, --app-slug, or --app-profile.")
            slug = validate_slug(app_slug)
            real_profile = studio_root / "profiles" / "apps" / f"{slug}.json"
            example_profile = studio_root / "profiles" / "apps" / f"{slug}.example.json"
            if real_profile.exists():
                profile_file = real_profile
            elif example_profile.exists():
                profile_file = example_profile
                warnings.append(f"Using example app profile {example_profile.name}; create {real_profile.name} for production.")
            else:
                raise FileNotFoundError(
                    f"Standalone app profile not found: {real_profile} (or {example_profile}). "
                    "Alternatively, pass --project-dir \".\" to auto-detect the current project."
                )
        elif not profile_file.exists():
            raise FileNotFoundError(f"Specified app profile does not exist: {profile_file}")

        raw = _read_json_file(profile_file)
        profile_label = str(profile_file.resolve())

    resolved_slug = validate_slug(
        str(raw.get("slug") or app_slug or (profile_file.stem.replace(".example", "") if profile_file else "app"))
    )
    package_name = validate_package_name(str(raw["package_name"]))
    framework = str(raw.get("framework", "kotlin")).strip().lower()
    if framework not in VALID_FRAMEWORKS:
        raise ValueError(f"Unsupported framework '{framework}'. Must be one of: {sorted(VALID_FRAMEWORKS)}")

    source_path = expand_win_path(raw.get("source_path"))
    source_exists = bool(source_path and source_path.exists())
    build_markers: dict[str, bool] = {}
    if source_exists and source_path is not None:
        if framework in ("kotlin", "java"):
            build_markers["gradlew_bat"] = (source_path / "gradlew.bat").exists()
            build_markers["settings_gradle"] = (
                (source_path / "settings.gradle").exists() or (source_path / "settings.gradle.kts").exists()
            )
        elif framework == "flutter":
            build_markers["pubspec_yaml"] = (source_path / "pubspec.yaml").exists()
    else:
        warnings.append(
            f"Standalone source_path '{raw.get('source_path')}' does not exist on this machine yet; "
            "Stage 2 build will require a valid local source checkout."
        )

    brand = raw.get("brand", {}) if isinstance(raw.get("brand"), dict) else {}
    icon_raw = expand_win_path(brand.get("icon_path"))
    icon_local_path = str(icon_raw) if (icon_raw and icon_raw.exists()) else None
    if brand.get("icon_path") and not icon_local_path:
        warnings.append(f"Configured icon_path does not exist: {brand.get('icon_path')}")

    account_info, acc_warnings = _resolve_account_profile(
        account_id=raw.get("account_id"),
        explicit_path=account_profile_path,
        studio_root=studio_root,
    )
    warnings.extend(acc_warnings)

    target_locales = locales_override or list(
        raw.get("target_locales") or config.get("defaults", {}).get("target_locales", ["ar", "en-US"])
    )

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "mode": "standalone",
        "read_only_source": False,
        "app": {
            "app_id": None,
            "slug": resolved_slug,
            "display_name": str(raw.get("display_name") or resolved_slug),
            "package_name": package_name,
            "framework": framework,
            "android_module": str(raw.get("android_module") or "app"),
            "primary_category": str(raw.get("primary_category") or "TOOLS"),
            "target_locales": target_locales,
        },
        "source": {
            "profile_path": profile_label,
            "source_path": str(source_path) if source_path else None,
            "source_exists": source_exists,
            "build_markers": build_markers,
        },
        "branding": {
            "accent_color": brand.get("accent_color", "#2563EB"),
            "background_color": brand.get("background_color", "#0F172A"),
            "icon_local_path": icon_local_path,
            "icon_source_url": brand.get("icon_source_url") or None,
        },
        "store_seed": raw.get("store_seed", {}),
        "signing": raw.get("signing", {"verify_signature": True}),
        "release_artifact": {
            "found": False,
            "path": None,
            "discovery_source": None,
            "size_bytes": 0,
            "sha256": None,
            "signature_verified": False,
            "signature_check": None,
        },
        "stage2_auto_skip": False,
        "account": account_info,
        "warnings": warnings,
    }
    return manifest, warnings


def detect_mode(
    requested_mode: str,
    config: dict[str, Any],
    app_slug: str | None,
    flavor: str | None,
    app_profile_path: Path | None,
    project_dir: Path | None = None,
    studio_root: Path = STUDIO_ROOT,
) -> str:
    if requested_mode in ("audio_workspace", "standalone"):
        return requested_mode
    if project_dir is not None or app_profile_path is not None:
        return "standalone"
    if flavor:
        return "audio_workspace"
    if app_slug:
        slug = validate_slug(app_slug)
        if (studio_root / "profiles" / "apps" / f"{slug}.json").exists():
            return "standalone"
        ws = get_audio_workspace_root(config)
        if ws and (ws / "03_App_Configurations" / slug / "app.json").exists():
            return "audio_workspace"
        if (studio_root / "profiles" / "apps" / f"{slug}.example.json").exists():
            return "standalone"
    raise ValueError(
        "Unable to auto-detect mode. Pass --project-dir \".\", --mode audio_workspace, or --mode standalone explicitly."
    )


def run_stage_01(
    mode: str = "auto",
    app_slug: str | None = None,
    flavor: str | None = None,
    app_profile: Path | None = None,
    project_dir: Path | None = None,
    account_profile: Path | None = None,
    locales: list[str] | None = None,
    run_dir: Path | None = None,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    config_path: Path | None = None,
    verify_signature: bool = True,
    studio_root: Path = STUDIO_ROOT,
) -> tuple[Path, dict[str, Any]]:
    """Execute Stage 1 Intake and persist intake_manifest.json + run_state.json strictly in workspace_runs/."""
    config = load_studio_config(config_path=config_path)
    resolved_mode = detect_mode(
        requested_mode=mode,
        config=config,
        app_slug=app_slug,
        flavor=flavor,
        app_profile_path=app_profile,
        project_dir=project_dir,
        studio_root=studio_root,
    )

    if resolved_mode == "audio_workspace":
        manifest, warnings = intake_audio_workspace(
            config=config,
            app_slug=app_slug,
            flavor=flavor,
            locales_override=locales,
            account_profile_path=account_profile,
            verify_signature=verify_signature,
            studio_root=studio_root,
        )
    else:
        manifest, warnings = intake_standalone(
            config=config,
            app_slug=app_slug,
            app_profile_path=app_profile,
            project_dir=project_dir,
            locales_override=locales,
            account_profile_path=account_profile,
            studio_root=studio_root,
        )

    resolved_slug = manifest["app"]["slug"]
    env_snapshot = resolve_environment(config=config, studio_root=studio_root).to_dict()
    manifest["environment"] = {
        "java_home": env_snapshot["java_home"],
        "java_source": env_snapshot["java_source"],
        "java_version": env_snapshot["java_version"],
        "android_sdk": env_snapshot["android_sdk"],
        "detekt_jar": env_snapshot["detekt_jar"],
        "node_bin": env_snapshot["node_bin"],
    }

    if run_dir is not None:
        manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    else:
        manager, state = RunStateManager.create_new(
            app_slug=resolved_slug,
            mode=resolved_mode,
            runs_root=runs_root,
        )

    manager.update_stage(state, "01_intake", "running", message=f"Running Stage 1 intake ({resolved_mode})")
    manifest_path = manager.write_json_artifact("intake_manifest.json", manifest)

    manager.update_stage(
        state,
        "01_intake",
        "completed",
        message=f"Intake completed for '{resolved_slug}' ({resolved_mode}).",
        artifacts={"intake_manifest": str(manifest_path)},
        warnings=warnings,
    )

    if manifest.get("stage2_auto_skip"):
        manager.update_stage(
            state,
            "02_audit_and_build",
            "skipped",
            message="Auto-skipped: verified signed release .aab already present from audio_workspace.",
            artifacts={"release_aab": str(manifest["release_artifact"]["path"])},
        )

    logger.info("Stage 1 completed: mode=%s slug=%s run_dir=%s", resolved_mode, resolved_slug, manager.run_dir)
    if warnings:
        for w in warnings:
            logger.warning("  [warn] %s", w)
    return manager.run_dir, manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 1 Dual-Mode Intake Adapter")
    parser.add_argument("--mode", choices=("auto", "audio_workspace", "standalone"), default="auto")
    parser.add_argument("--app-slug", help="App slug (e.g., eslam-kabonga or sample-standalone-app)")
    parser.add_argument("--flavor", help="Android product flavor (Mode A audio_workspace)")
    parser.add_argument("--project-dir", type=Path, help="Auto-detect standalone Android/Flutter project directly (e.g. --project-dir \".\")")
    parser.add_argument("--app-profile", type=Path, help="Explicit path to standalone app JSON profile (Mode B)")
    parser.add_argument("--account-profile", type=Path, help="Optional explicit path to developer account JSON profile")
    parser.add_argument("--locales", help="Comma-separated target locales override (e.g. ar,en-US)")
    parser.add_argument("--run-dir", type=Path, help="Existing run directory to update instead of creating a new run")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT, help="Override workspace_runs root dir")
    parser.add_argument("--config", type=Path, help="Optional path to studio.config.json")
    parser.add_argument(
        "--no-verify-aab-signature",
        action="store_true",
        help="Skip jarsigner verification when a release .aab is discovered",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    locales = [x.strip() for x in args.locales.split(",") if x.strip()] if args.locales else None

    run_dir, manifest = run_stage_01(
        mode=args.mode,
        app_slug=args.app_slug,
        flavor=args.flavor,
        app_profile=args.app_profile,
        project_dir=args.project_dir,
        account_profile=args.account_profile,
        locales=locales,
        run_dir=args.run_dir,
        runs_root=args.runs_root,
        config_path=args.config,
        verify_signature=not args.no_verify_aab_signature,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "run_dir": str(run_dir),
                "mode": manifest["mode"],
                "app_slug": manifest["app"]["slug"],
                "package_name": manifest["app"]["package_name"],
                "stage2_auto_skip": manifest["stage2_auto_skip"],
                "warnings_count": len(manifest.get("warnings", [])),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
