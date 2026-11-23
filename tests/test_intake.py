"""Automated unit & integration tests for Phase 1 (Environment Resolver, RunState, Stage 1 Intake).

Verifies:
1. core/env_resolver.py prioritizes Android Studio JBR over system Java 23+ for JAVA_HOME.
2. core/run_state.py creates resumable run_state.json strictly inside workspace_runs/.
3. Stage 1 Mode A (audio_workspace) reads app metadata, branding, listing, music_data.json,
   and discovers release .aab via SQLite (?mode=ro) / filesystem WITHOUT writing a single byte
   inside the audio workspace.
4. Stage 1 Mode B (standalone) works cleanly with zero dependency on Youssef_Audio_Workspace.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from core.env_resolver import build_jvm_env, resolve_environment, resolve_java_home
from core.run_state import RunStateManager

intake = importlib.import_module("stages.01_intake_adapter")


# ---------------------------------------------------------------------------
# 1. Environment Resolver Tests (Android Studio JBR priority over Java 23)
# ---------------------------------------------------------------------------

def test_env_resolver_prefers_android_studio_jbr_over_java23(tmp_path: Path) -> None:
    fake_jbr = tmp_path / "Android Studio" / "jbr"
    (fake_jbr / "bin").mkdir(parents=True)
    (fake_jbr / "bin" / "java.exe").write_text("", encoding="utf-8")
    (fake_jbr / "bin" / "jarsigner.exe").write_text("", encoding="utf-8")
    (fake_jbr / "release").write_text('JAVA_VERSION="21.0.10"\n', encoding="utf-8")

    fake_jdk23 = tmp_path / "jdk-23"
    (fake_jdk23 / "bin").mkdir(parents=True)
    (fake_jdk23 / "bin" / "java.exe").write_text("", encoding="utf-8")
    (fake_jdk23 / "release").write_text('JAVA_VERSION="23.0.1"\n', encoding="utf-8")

    cfg = {
        "environment": {
            "java_home_override": "",
            "prefer_android_studio_jbr": True,
            "android_studio_jbr_candidates": [str(fake_jbr)],
        }
    }
    fake_env = {"JAVA_HOME": str(fake_jdk23), "PATH": str(fake_jdk23 / "bin")}

    java_home, source, version = resolve_java_home(config=cfg, env=fake_env)
    assert java_home == fake_jbr
    assert source == "android_studio_jbr"
    assert version == "21.0.10"

    jvm_env = build_jvm_env(config=cfg, base_env=fake_env)
    assert jvm_env["JAVA_HOME"] == str(fake_jbr)
    assert jvm_env["PATH"].startswith(str(fake_jbr / "bin"))


def test_env_resolver_on_local_machine_selects_jbr_if_installed() -> None:
    jbr_path = Path(r"C:\Program Files\Android\Android Studio\jbr")
    if not (jbr_path / "bin" / "java.exe").exists():
        pytest.skip("Android Studio JBR not installed on this runner.")
    resolved = resolve_environment()
    assert resolved.java_home == str(jbr_path)
    assert resolved.java_source == "android_studio_jbr"
    assert resolved.jarsigner_bin is not None
    assert "23" not in (resolved.java_version or "")


# ---------------------------------------------------------------------------
# 2. RunStateManager Tests
# ---------------------------------------------------------------------------

def test_run_state_manager_resumable_and_sandboxed(tmp_path: Path) -> None:
    runs_root = tmp_path / "workspace_runs"
    manager, state = RunStateManager.create_new("demo-app", mode="standalone", runs_root=runs_root)
    assert (manager.run_dir / "run_state.json").exists()
    assert state.app_slug == "demo-app"

    manager.update_stage(
        state,
        "01_intake",
        "completed",
        message="Done",
        artifacts={"intake_manifest": str(manager.run_dir / "intake_manifest.json")},
    )
    loaded_mgr, loaded_state = RunStateManager.load(manager.run_dir, runs_root=runs_root)
    assert loaded_state.stages["01_intake"].status == "completed"
    assert RunStateManager.find_latest_run("demo-app", runs_root=runs_root) == loaded_mgr.run_dir

    # Path traversal outside workspace_runs MUST raise PermissionError
    with pytest.raises(PermissionError):
        loaded_mgr.write_json_artifact("../../outside.json", {"bad": True})


# ---------------------------------------------------------------------------
# 3. Stage 1 — Mode A (audio_workspace) Read-Only Tests
# ---------------------------------------------------------------------------

def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """Return {relative_path: (size, mtime_ns)} for every file under root."""
    snap: dict[str, tuple[int, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return snap


def test_mode_a_audio_workspace_full_readonly_and_sqlite_aab_discovery(tmp_path: Path) -> None:
    ws = tmp_path / "Youssef_Audio_Workspace"
    (ws / "03_App_Configurations" / "eslam-kabonga" / "branding").mkdir(parents=True)
    (ws / "03_App_Configurations" / "eslam-kabonga" / "store").mkdir(parents=True)
    flavor_root = ws / "01_Platform_Source" / "apps" / "android" / "app" / "src" / "eslamKabonga"
    (flavor_root / "assets" / "offline").mkdir(parents=True)
    (flavor_root / "res" / "mipmap-xxxhdpi").mkdir(parents=True)
    release_dir = ws / "05_Releases" / "agent-builds" / "job-101" / "eslamKabonga" / "release"
    release_dir.mkdir(parents=True)

    (ws / "workspace.json").write_text(
        json.dumps({"schema_version": 1, "layout": {"accounts": "02_Developer_Accounts"}}),
        encoding="utf-8",
    )
    (ws / "03_App_Configurations" / "eslam-kabonga" / "app.json").write_text(
        json.dumps(
            {
                "app_id": "app-001",
                "slug": "eslam-kabonga",
                "display_name": "اسلام كابونجا",
                "package_name": "com.Eslam_Kabonga.Song_Offline",
                "flavor": "eslamKabonga",
                "developer_account": "03_Songs",
                "android_source_set": "01_Platform_Source/apps/android/app/src/eslamKabonga",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (ws / "03_App_Configurations" / "eslam-kabonga" / "branding" / "branding.json").write_text(
        json.dumps({"theme_accent_color": "#00FF00", "icon_source_url": "https://example.com/icon.jpg"}),
        encoding="utf-8",
    )
    (ws / "03_App_Configurations" / "eslam-kabonga" / "store" / "listing.json").write_text(
        json.dumps(
            {
                "runtime_display_name": "اسلام كابونجا",
                "device_display_name": "اسلام كابونجا",
                "artist_name": "اسلام كابونجا",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (flavor_root / "assets" / "offline" / "music_data.json").write_text(
        json.dumps(
            {
                "api_version": "2.6",
                "catalog_revision": 42,
                "application": {"name": "مهرجانات اسلام كابونجا", "version": "1.2"},
                "artists": [{"id": "a1", "name": "اسلام كابونجا"}],
                "categories": [{"id": "c1", "name": "مهرجانات"}],
                "songs": [
                    {"id": "s1", "title": "مهرجان الشارع"},
                    {"id": "s2", "title": "محسوبك اجدع خلق الله"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (flavor_root / "res" / "mipmap-xxxhdpi" / "ic_launcher.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

    aab_file = release_dir / "eslam-kabonga-release.aab"
    aab_file.write_bytes(b"PK\x03\x04FAKE_AAB_PAYLOAD")

    # Populate read-only SQLite agent_jobs DB pointing to the AAB
    sqlite_db = tmp_path / "agent_jobs.sqlite3"
    conn = sqlite3.connect(sqlite_db)
    conn.execute(
        """
        CREATE TABLE agent_jobs (
            id INTEGER PRIMARY KEY,
            app_slug TEXT,
            flavor TEXT,
            job_type TEXT,
            local_status TEXT,
            build_variant TEXT,
            aab_path TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_jobs (app_slug, flavor, job_type, local_status, build_variant, aab_path, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("eslam-kabonga", "eslamKabonga", "android_build_aab", "completed", "release", str(aab_file), "2026-11-23T10:00:00Z"),
    )
    conn.commit()
    conn.close()

    cfg_path = tmp_path / "studio.config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "audio_workspace": {
                    "enabled": True,
                    "root_path": str(ws),
                    "local_api_url": "http://127.0.0.1:59999",
                    "agent_jobs_sqlite_path": str(sqlite_db),
                    "read_only": True,
                }
            }
        ),
        encoding="utf-8",
    )

    runs_root = tmp_path / "workspace_runs"
    before_ws = _snapshot_tree(ws)

    # Mock jarsigner verification as passing to test stage2_auto_skip=True
    with mock.patch.object(
        intake,
        "verify_aab_signature",
        return_value={"verified": True, "tool": "jbr/jarsigner.exe", "java_source": "android_studio_jbr"},
    ):
        run_dir, manifest = intake.run_stage_01(
            mode="audio_workspace",
            flavor="eslamKabonga",
            runs_root=runs_root,
            config_path=cfg_path,
        )

    # STRICT ZERO-TOUCH GUARANTEE on Youssef_Audio_Workspace
    after_ws = _snapshot_tree(ws)
    assert before_ws == after_ws

    # Outputs must live strictly inside workspace_runs/
    assert run_dir.is_relative_to(runs_root)
    assert (run_dir / "intake_manifest.json").exists()
    assert (run_dir / "run_state.json").exists()

    assert manifest["mode"] == "audio_workspace"
    assert manifest["app"]["slug"] == "eslam-kabonga"
    assert manifest["app"]["package_name"] == "com.Eslam_Kabonga.Song_Offline"
    assert manifest["audio_catalog"]["available"] is True
    assert manifest["audio_catalog"]["song_count"] == 2
    assert "اسلام كابونجا" in manifest["audio_catalog"]["artist_names"]
    assert manifest["branding"]["icon_local_path"].endswith("ic_launcher.png")
    assert manifest["release_artifact"]["found"] is True
    assert manifest["release_artifact"]["discovery_source"].startswith("sqlite_ro:")
    assert manifest["stage2_auto_skip"] is True

    _, loaded_state = RunStateManager.load(run_dir, runs_root=runs_root)
    assert loaded_state.stages["01_intake"].status == "completed"
    assert loaded_state.stages["02_audit_and_build"].status == "skipped"


# ---------------------------------------------------------------------------
# 4. Stage 1 — Mode B (standalone) Zero-Workspace-Dependency Tests
# ---------------------------------------------------------------------------

def test_mode_b_standalone_works_without_audio_workspace(tmp_path: Path) -> None:
    fake_source = tmp_path / "MyKotlinApp"
    fake_source.mkdir()
    (fake_source / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
    (fake_source / "settings.gradle.kts").write_text('rootProject.name = "Focus"\n', encoding="utf-8")

    app_profile = tmp_path / "focus-timer.json"
    app_profile.write_text(
        json.dumps(
            {
                "slug": "focus-timer",
                "display_name": "Focus Timer Pro",
                "package_name": "com.acme.focustimer",
                "framework": "kotlin",
                "source_path": str(fake_source),
                "account_id": "acme_dev",
                "target_locales": ["en-US", "ar"],
                "brand": {"accent_color": "#2563EB"},
            }
        ),
        encoding="utf-8",
    )

    acc_profile = tmp_path / "acme_dev.json"
    acc_profile.write_text(
        json.dumps(
            {
                "account_id": "acme_dev",
                "display_name": "Acme Mobile Studio",
                "publish_mode": "manual_bundle",
                "default_track": "draft",
            }
        ),
        encoding="utf-8",
    )

    # Config explicitly disables audio_workspace and points to a non-existent path
    cfg_path = tmp_path / "studio.config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "audio_workspace": {
                    "enabled": False,
                    "root_path": str(tmp_path / "DOES_NOT_EXIST_WORKSPACE"),
                }
            }
        ),
        encoding="utf-8",
    )

    runs_root = tmp_path / "workspace_runs"
    run_dir, manifest = intake.run_stage_01(
        mode="standalone",
        app_profile=app_profile,
        account_profile=acc_profile,
        runs_root=runs_root,
        config_path=cfg_path,
    )

    assert run_dir.is_relative_to(runs_root)
    assert (run_dir / "intake_manifest.json").exists()
    assert manifest["mode"] == "standalone"
    assert manifest["app"]["slug"] == "focus-timer"
    assert manifest["app"]["framework"] == "kotlin"
    assert manifest["source"]["source_exists"] is True
    assert manifest["source"]["build_markers"]["gradlew_bat"] is True
    assert manifest["stage2_auto_skip"] is False
    assert manifest["account"]["account_id"] == "acme_dev"
    assert manifest["account"]["publish_mode"] == "manual_bundle"

    _, loaded_state = RunStateManager.load(run_dir, runs_root=runs_root)
    assert loaded_state.stages["01_intake"].status == "completed"
    assert loaded_state.stages["02_audit_and_build"].status == "pending"
