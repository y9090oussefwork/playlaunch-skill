"""Automated tests for Stage 2 (Audit/Build + JBR), Stage 3 (ADB Fallback), and Stage 7 (Publish Gate).

STRICT ISOLATION GUARANTEE:
All tests run in `tmp_path`, use mocked network/subprocesses, never touch
`security/ip_ledger.sqlite3`, never touch `Youssef_Audio_Workspace`, and never call Google APIs.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from core.run_state import RunStateManager
from core.validators import sha256_file
from security.isolation_guard import IsolationGuard

stage2 = importlib.import_module("stages.02_audit_and_build")
stage3 = importlib.import_module("stages.03_capture_screens")
stage7 = importlib.import_module("stages.07_isolated_publish")


def _create_ready_run(tmp_path: Path, mode: str = "audio_workspace") -> tuple[Path, Path, Path]:
    runs_root = tmp_path / "workspace_runs"
    fake_ws = tmp_path / "Fake_Audio_Workspace"
    manager, state = RunStateManager.create_new("demo-app", mode=mode, runs_root=runs_root)

    manifest = {
        "schema_version": 1,
        "mode": mode,
        "app": {
            "slug": "demo-app",
            "display_name": "تطبيق تجريبي",
            "package_name": "com.example.demoapp",
            "framework": "kotlin",
            "target_locales": ["ar", "en-US"],
        },
        "account": {
            "account_id": "dev_acc_01",
            "publish_mode": "manual_bundle",
            "default_track": "draft",
        },
        "release_artifact": {"found": False, "path": None, "signature_verified": False},
        "stage2_auto_skip": False,
        "audio_workspace_refs": {
            "workspace_root": str(fake_ws),
            "planned_export_targets": {
                "store_assets_dir": str(fake_ws / "02_Developer_Accounts" / "dev_acc_01" / "store-assets" / "demo-app"),
                "release_notes_dir": str(fake_ws / "02_Developer_Accounts" / "dev_acc_01" / "release-notes" / "demo-app"),
            },
        },
    }
    manager.write_json_artifact("intake_manifest.json", manifest)

    # Populate Fastlane copy + visuals
    for loc, title, short_d in (
        ("ar", "تطبيق تجريبي بدون نت", "وصف قصير عربي واضح ومختصر."),
        ("en-US", "Demo Offline App", "Clean short English description."),
    ):
        loc_dir = manager.run_dir / "metadata" / "android" / loc
        (loc_dir / "changelogs").mkdir(parents=True, exist_ok=True)
        (loc_dir / "title.txt").write_text(title, encoding="utf-8")
        (loc_dir / "short_description.txt").write_text(short_d, encoding="utf-8")
        (loc_dir / "full_description.txt").write_text(f"Full listing text for {loc}.", encoding="utf-8")
        (loc_dir / "changelogs" / "1.txt").write_text("Initial release.", encoding="utf-8")

        fg_dir = manager.run_dir / "visuals" / "feature_graphic" / loc
        fg_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1024, 500), (20, 40, 60)).save(fg_dir / "feature_graphic.png")

        sc_dir = manager.run_dir / "visuals" / "screenshots" / loc
        sc_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1080, 1920), (15, 25, 35)).save(sc_dir / "01.png")

    Image.new("RGBA", (512, 512), (34, 197, 94, 255)).save(manager.run_dir / "visuals" / "icon_512.png")
    manager.update_stage(state, "06_render_visuals", "completed")
    manager.update_stage(state, "07_isolated_publish", "awaiting_human_approval")
    return runs_root, manager.run_dir, fake_ws


# ---------------------------------------------------------------------------
# 1. Stage 7 Mandatory Human Approval Gate & Manual Bundle Tests
# ---------------------------------------------------------------------------

def test_stage7_refuses_without_human_approval(tmp_path: Path) -> None:
    runs_root, run_dir, _ = _create_ready_run(tmp_path)

    with pytest.raises(stage7.HumanApprovalRequiredError):
        stage7.run_stage_07(run_dir=run_dir, runs_root=runs_root, approve=False, dry_run=False)


def test_stage7_manual_bundle_creates_ready_to_upload_and_keeps_audio_workspace_untouched(tmp_path: Path) -> None:
    runs_root, run_dir, fake_ws = _create_ready_run(tmp_path)

    # Default (sync_to_audio_workspace=False) MUST NOT touch fake_ws at all
    report_path, report = stage7.run_stage_07(
        run_dir=run_dir,
        runs_root=runs_root,
        approve=True,
        dry_run=False,
        sync_to_audio_workspace=False,
    )

    assert report_path.exists()
    assert report["publish_mode"] == "manual_bundle"
    assert report["result"]["network_calls"] == 0
    assert report["sync_to_audio_workspace"] is None
    assert not fake_ws.exists()  # ZERO writes outside workspace_runs/

    ready = run_dir / "READY_TO_UPLOAD"
    assert (ready / "listing_copy.txt").exists()
    assert (ready / "listing_copy.html").exists()
    assert (ready / "README_UPLOAD_STEPS.txt").exists()
    assert (ready / "bundle_manifest.json").exists()
    assert (ready / "assets" / "icon_512.png").exists()
    assert (ready / "assets" / "ar" / "feature_graphic.png").exists()
    assert (ready / "assets" / "ar" / "screenshots" / "01.png").exists()
    assert (ready / "metadata" / "android" / "ar" / "title.txt").exists()

    # Now verify explicit --sync-to-audio-workspace DOES mirror approved assets when asked
    _, sync_report = stage7.run_stage_07(
        run_dir=run_dir,
        runs_root=runs_root,
        approve=True,
        dry_run=False,
        sync_to_audio_workspace=True,
    )
    assert sync_report["sync_to_audio_workspace"]["synced"] is True
    assert (fake_ws / "02_Developer_Accounts" / "dev_acc_01" / "store-assets" / "demo-app" / "listing_copy.html").exists()
    assert (fake_ws / "02_Developer_Accounts" / "dev_acc_01" / "release-notes" / "demo-app" / "ar" / "1.txt").exists()


def test_stage7_isolated_auto_publish_dry_run_enforces_guard_and_defaults_to_draft(tmp_path: Path) -> None:
    runs_root, run_dir, _ = _create_ready_run(tmp_path, mode="standalone")
    key_path = tmp_path / "svc_key.json"
    key_path.write_text(
        json.dumps({"type": "service_account", "client_email": "bot@test-proj.iam.gserviceaccount.com", "private_key": "FAKE"}),
        encoding="utf-8",
    )
    acc_profile_path = tmp_path / "acc_auto.json"
    acc_profile_path.write_text(
        json.dumps(
            {
                "account_id": "auto_acc_01",
                "publish_mode": "isolated_auto_publish",
                "default_track": "draft",
                "network_isolation": {"mode": "router_reboot", "strict_subnet_block": True},
                "play_api_credentials": {
                    "service_account_email": "bot@test-proj.iam.gserviceaccount.com",
                    "key_file_path": str(key_path),
                    "key_file_sha256": sha256_file(key_path),
                },
            }
        ),
        encoding="utf-8",
    )

    isolated_guard = IsolationGuard(
        ledger_db_path=tmp_path / "test_ledger.sqlite3",
        live_status_path=tmp_path / "test_live.json",
        ip_fetcher=lambda **kw: ("203.0.113.120", "203.0.113.0/24", "mock"),
    )

    report_path, report = stage7.run_stage_07(
        run_dir=run_dir,
        runs_root=runs_root,
        approve=False,  # --dry-run works even before approval
        dry_run=True,
        account_profile_path=acc_profile_path,
        guard_override=isolated_guard,
        wait_for_router_reboot=False,
    )
    assert report_path.name == "publish_dry_run_report.json"
    assert report["publish_mode"] == "isolated_auto_publish"
    assert report["result"]["play_api_calls_executed"] == 0
    assert report["result"]["api_plan"]["target_track"] == "draft"
    assert report["result"]["api_plan"]["release_status"] == "draft"
    assert report["result"]["isolation_session"]["verified_ip"] == "203.0.113.120"
    # Dry-run MUST NOT mutate real ledger rows
    assert isolated_guard.get_ip_history() == []


# ---------------------------------------------------------------------------
# 2. Stage 2 Audit/Build (JBR JAVA_HOME + Android Vitals Risk Scanner)
# ---------------------------------------------------------------------------

def test_stage2_scans_vitals_risks_and_uses_jbr_java_home(tmp_path: Path) -> None:
    runs_root = tmp_path / "workspace_runs"
    src = tmp_path / "StandaloneKotlinApp"
    (src / "app" / "src" / "main" / "java" / "com" / "demo").mkdir(parents=True)
    (src / "app" / "src" / "main" / "java" / "com" / "demo" / "MainActivity.kt").write_text(
        "class MainActivity { fun onCreate() { Thread.sleep(2000); db.allowMainThreadQueries() } }\n",
        encoding="utf-8",
    )
    (src / "app" / "build.gradle.kts").write_text(
        "android { buildTypes { release { isMinifyEnabled = false } } }\n",
        encoding="utf-8",
    )

    manager, _ = RunStateManager.create_new("kotlin-audit-demo", mode="standalone", runs_root=runs_root)
    manager.write_json_artifact(
        "intake_manifest.json",
        {
            "mode": "standalone",
            "app": {"slug": "kotlin-audit-demo", "package_name": "com.demo.app", "framework": "kotlin", "android_module": "app"},
            "source": {"source_path": str(src), "source_exists": True},
            "stage2_auto_skip": False,
        },
    )

    captured_envs: list[dict] = []

    def fake_subproc(cmd, cwd, env, timeout_seconds=300):
        captured_envs.append(dict(env))
        return {"executed": True, "cmd": cmd, "returncode": 0, "passed": True, "java_home_used": env.get("JAVA_HOME")}

    with mock.patch.object(stage2, "_run_tool_subprocess", side_effect=fake_subproc):
        report_path, report = stage2.run_stage_02(run_dir=manager.run_dir, runs_root=runs_root, audit_only=True)

    assert report_path.exists()
    rule_ids = {f["rule_id"] for f in report["vitals_risk_report"]["findings"]}
    assert "MAIN_THREAD_SLEEP" in rule_ids
    assert "ROOM_MAIN_THREAD_QUERIES" in rule_ids
    assert "RELEASE_MINIFY_DISABLED" in rule_ids
    assert report["vitals_risk_report"]["high_severity_count"] >= 2
    if captured_envs:
        assert "jbr" in (captured_envs[0].get("JAVA_HOME") or "").lower()


# ---------------------------------------------------------------------------
# 3. Stage 3 Screen Capture Graceful ADB Fallback
# ---------------------------------------------------------------------------

def test_stage3_graceful_fallback_without_adb_device(tmp_path: Path) -> None:
    runs_root = tmp_path / "workspace_runs"
    manager, _ = RunStateManager.create_new("adb-fallback-demo", mode="standalone", runs_root=runs_root)
    manager.write_json_artifact(
        "intake_manifest.json",
        {"mode": "standalone", "app": {"slug": "adb-fallback-demo", "package_name": "com.demo.adb", "target_locales": ["ar"]}},
    )

    # Case 1: No ADB device & empty raw_screenshots -> gracefully skipped (does NOT fail!)
    with mock.patch.object(stage3, "list_connected_adb_devices", return_value=[]):
        _, rep_empty = stage3.run_stage_03(run_dir=manager.run_dir, runs_root=runs_root)
    assert rep_empty["source_mode"] == "none"
    _, st1 = RunStateManager.load(manager.run_dir, runs_root=runs_root)
    assert st1.stages["03_capture_screens"].status == "skipped"

    # Case 2: No ADB device, but pre-captured PNG exists in raw_screenshots -> completed!
    Image.new("RGB", (1080, 2400), (30, 30, 30)).save(manager.run_dir / "raw_screenshots" / "01.png")
    with mock.patch.object(stage3, "list_connected_adb_devices", return_value=[]):
        _, rep_pre = stage3.run_stage_03(run_dir=manager.run_dir, runs_root=runs_root)
    assert rep_pre["source_mode"] == "pre_captured_existing"
    assert rep_pre["total_raw_screenshots"] == 1
    _, st2 = RunStateManager.load(manager.run_dir, runs_root=runs_root)
    assert st2.stages["03_capture_screens"].status == "completed"
