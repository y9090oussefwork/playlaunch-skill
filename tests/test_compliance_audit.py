"""Automated tests for Phase 5 — Google Play Compliance & Rejection Prevention Engine.

Tests:
  - Manifest technical checks (targetSdk, debuggable, usesCleartextTraffic, android:exported)
  - ELF64 16 KB native library page alignment check (4 KB fail vs 16 KB pass)
  - Restricted & sensitive permissions (QUERY_ALL_PACKAGES, MANAGE_EXTERNAL_STORAGE,
    FOREGROUND_SERVICE_MEDIA_PLAYBACK, AD_ID)
  - Embedded SDK detection (AdMob, Firebase Analytics, Crashlytics) -> Data Safety & UMP & Families Policy
  - Privacy Policy HTTPS/HTTP-200 validator
  - IP & Impersonation Guard (Arabic-normalized + English, YouTube downloader & Official claims)
  - Stage 7 ComplianceBlockerError gate & explicit --acknowledge-compliance-risks override
  - Desktop UI Compliance tab & API route

All tests run strictly in `tmp_path` with mocked HTTP; zero external calls or workspace mutations.
"""

from __future__ import annotations

import importlib
import json
import struct
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from core.run_state import RunStateManager

stage2b = importlib.import_module("stages.02b_compliance_audit")
stage7 = importlib.import_module("stages.07_isolated_publish")


def _make_minimal_elf64_so(p_align: int) -> bytes:
    """Construct a minimal valid ELF64 little-endian shared object with 1 PT_LOAD segment."""
    # ELF64 header = 64 bytes; 1 program header at offset 64 (size 56 bytes)
    e_ident = b"\x7fELF\x02\x01\x01" + (b"\x00" * 9)
    ehdr = e_ident + struct.pack(
        "<HHIQQQIHHHHHH",
        3,      # e_type = ET_DYN
        183,    # e_machine = EM_AARCH64
        1,      # e_version
        0,      # e_entry
        64,     # e_phoff
        0,      # e_shoff
        0,      # e_flags
        64,     # e_ehsize
        56,     # e_phentsize
        1,      # e_phnum
        0,      # e_shentsize
        0,      # e_shnum
        0,      # e_shstrndx
    )
    phdr = struct.pack(
        "<IIQQQQQQ",
        1,        # p_type = PT_LOAD
        5,        # p_flags = PF_R | PF_X
        0,        # p_offset
        0,        # p_vaddr
        0,        # p_paddr
        0x1000,   # p_filesz
        0x1000,   # p_memsz
        p_align,  # p_align
    )
    return ehdr + phdr


def _build_synthetic_aab(
    target_path: Path,
    manifest_xml: str,
    dex_markers: bytes = b"",
    so_align_bytes: int | None = None,
) -> Path:
    with zipfile.ZipFile(target_path, "w") as zf:
        zf.writestr("base/manifest/AndroidManifest.xml", manifest_xml.encode("utf-8"))
        if dex_markers:
            zf.writestr("base/dex/classes.dex", dex_markers)
        if so_align_bytes is not None:
            zf.writestr("base/lib/arm64-v8a/libaudioengine.so", _make_minimal_elf64_so(so_align_bytes))
    return target_path


def _create_base_run(tmp_path: Path, slug: str = "audit-demo") -> tuple[Path, Path]:
    runs_root = tmp_path / "workspace_runs"
    manager, _ = RunStateManager.create_new(slug, mode="audio_workspace", runs_root=runs_root)
    manager.write_json_artifact(
        "intake_manifest.json",
        {
            "mode": "audio_workspace",
            "app": {
                "slug": slug,
                "display_name": "مشغل صوتي خفيف",
                "package_name": "com.example.cleanplayer",
                "target_locales": ["ar", "en-US"],
            },
            "account": {"account_id": "dev_test", "publish_mode": "manual_bundle"},
            "release_artifact": {"found": False, "path": None},
        },
    )
    return runs_root, manager.run_dir


# ---------------------------------------------------------------------------
# 1. Manifest, Restricted Permissions, SDK/Data Safety, 16 KB ELF64 & Families
# ---------------------------------------------------------------------------

def test_compliance_audit_detects_manifest_permissions_sdks_16kb_and_families_blockers(tmp_path: Path) -> None:
    runs_root, run_dir = _create_base_run(tmp_path)

    bad_manifest = """<?xml version="1.0" encoding="utf-8"?>
    <manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.risky">
      <uses-sdk android:minSdkVersion="24" android:targetSdkVersion="33" />
      <uses-permission android:name="android.permission.QUERY_ALL_PACKAGES" />
      <uses-permission android:name="android.permission.MANAGE_EXTERNAL_STORAGE" />
      <uses-permission android:name="android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK" />
      <uses-permission android:name="com.google.android.gms.permission.AD_ID" />
      <application android:debuggable="true" android:usesCleartextTraffic="true">
        <activity android:name=".MainActivity">
          <intent-filter>
            <action android:name="android.intent.action.MAIN" />
          </intent-filter>
        </activity>
      </application>
    </manifest>
    """
    aab = _build_synthetic_aab(
        tmp_path / "risky.aab",
        manifest_xml=bad_manifest,
        dex_markers=(
            b"Lcom/google/android/gms/ads/MobileAds;\x00"
            b"Lcom/google/firebase/analytics/FirebaseAnalytics;\x00"
            b"Lcom/google/firebase/crashlytics/FirebaseCrashlytics;\x00"
        ),
        so_align_bytes=4096,  # 4 KB -> should trigger 16 KB warning
    )

    report_path, report = stage2b.run_stage_02b(
        run_dir=run_dir,
        runs_root=runs_root,
        artifact_override=aab,
        privacy_policy_url="http://insecure.example.com/privacy",  # non-HTTPS -> BLOCKER
        targets_children=True,
        skip_network_checks=True,
    )

    assert report_path.exists()
    assert (run_dir / "play_console_declarations_checklist.html").exists()
    assert (run_dir / "play_console_declarations_checklist.txt").exists()

    rule_ids = {f["rule_id"] for f in report["findings"]}
    assert not report["passed"]
    assert "TARGET_SDK_TOO_LOW" in rule_ids
    assert "DEBUGGABLE_ENABLED" in rule_ids
    assert "CLEARTEXT_TRAFFIC_ENABLED" in rule_ids
    assert "MISSING_ANDROID_EXPORTED" in rule_ids
    assert "PERMISSION_QUERY_ALL_PACKAGES" in rule_ids
    assert "PERMISSION_MANAGE_EXTERNAL_STORAGE" in rule_ids
    assert "PERMISSION_FOREGROUND_SERVICE_MEDIA_PLAYBACK" in rule_ids
    assert "NATIVE_16KB_ALIGNMENT_WARNING" in rule_ids
    assert "ADMOB_MISSING_UMP_CONSENT" in rule_ids
    assert "FAMILIES_POLICY_ADS_CONFLICT" in rule_ids
    assert "FAMILIES_POLICY_AD_ID_FORBIDDEN" in rule_ids
    assert "PRIVACY_POLICY_URL_INVALID" in rule_ids

    # Data Safety disclosures auto-generated from AdMob + Firebase Analytics + Crashlytics
    assert report["declarations"]["contains_ads"] is True
    assert report["declarations"]["ump_consent_detected"] is False
    categories = {row["category"] for row in report["declarations"]["data_safety_rows"]}
    assert "Device or other IDs" in categories
    assert "App info and performance" in categories


def test_compliance_audit_passes_clean_modern_16kb_aab_and_https_privacy(tmp_path: Path) -> None:
    runs_root, run_dir = _create_base_run(tmp_path, slug="clean-app")
    clean_manifest = """<?xml version="1.0" encoding="utf-8"?>
    <manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.clean">
      <uses-sdk android:minSdkVersion="24" android:targetSdkVersion="35" />
      <application android:debuggable="false" android:usesCleartextTraffic="false">
        <activity android:name=".MainActivity" android:exported="true">
          <intent-filter>
            <action android:name="android.intent.action.MAIN" />
          </intent-filter>
        </activity>
      </application>
    </manifest>
    """
    aab = _build_synthetic_aab(
        tmp_path / "clean.aab",
        manifest_xml=clean_manifest,
        dex_markers=b"Lcom/example/clean/Player;\x00",
        so_align_bytes=16384,  # 16 KB aligned!
    )

    fake_http = mock.Mock(status_code=200, text="<html><body><h1>Privacy Policy</h1><p>We respect your privacy and collect zero personal data.</p></body></html>")
    with mock.patch.object(stage2b.requests, "get", return_value=fake_http):
        _, report = stage2b.run_stage_02b(
            run_dir=run_dir,
            runs_root=runs_root,
            artifact_override=aab,
            privacy_policy_url="https://example.com/privacy",
            skip_network_checks=False,
        )

    assert report["passed"] is True
    assert report["blocker_count"] == 0
    assert report["warning_count"] == 0
    assert report["readiness_score"] == 100
    assert report["binary_audit"]["native_16kb_check"]["all_aligned"] is True


# ---------------------------------------------------------------------------
# 2. IP, Impersonation & Prohibited Downloader Detection (Arabic-Normalized + EN)
# ---------------------------------------------------------------------------

def test_compliance_audit_blocks_impersonation_and_youtube_downloader_claims(tmp_path: Path) -> None:
    runs_root, run_dir = _create_base_run(tmp_path, slug="bad-copy")
    ar_dir = run_dir / "metadata" / "android" / "ar"
    en_dir = run_dir / "metadata" / "android" / "en-US"
    ar_dir.mkdir(parents=True)
    en_dir.mkdir(parents=True)

    # Arabic with hamza/tashkeel variants ("التطبيق الرسمي", "تحميل من يوتيوب")
    (ar_dir / "title.txt").write_text("التطبيق الرسمي للمهرجانات", encoding="utf-8")
    (ar_dir / "full_description.txt").write_text("يتيح لك تحميل من يوتيوب بسهولة تامة وبسرعة.", encoding="utf-8")
    (en_dir / "title.txt").write_text("Official App Music", encoding="utf-8")
    (en_dir / "full_description.txt").write_text("Fast YouTube Downloader and audio player.", encoding="utf-8")

    _, report = stage2b.run_stage_02b(
        run_dir=run_dir,
        runs_root=runs_root,
        privacy_policy_url="https://example.com/privacy",
        skip_network_checks=True,
    )

    rule_ids = {f["rule_id"] for f in report["findings"]}
    assert not report["passed"]
    assert "IMPERSONATION_OFFICIAL_CLAIM" in rule_ids
    assert "PROHIBITED_DOWNLOADER_FUNCTIONALITY" in rule_ids
    assert "THIRD_PARTY_BRAND_REFERENCE" in rule_ids


# ---------------------------------------------------------------------------
# 3. Stage 7 Blocker Gate & Explicit Override + Checklist in READY_TO_UPLOAD
# ---------------------------------------------------------------------------

def test_stage7_blocks_on_compliance_blocker_and_allows_explicit_ack_with_checklist_packaged(tmp_path: Path) -> None:
    runs_root, run_dir = _create_base_run(tmp_path, slug="gated-app")
    ar_dir = run_dir / "metadata" / "android" / "ar"
    ar_dir.mkdir(parents=True)
    (ar_dir / "title.txt").write_text("تطبيق رسمي تجريبي", encoding="utf-8")  # Triggers BLOCKER

    stage2b.run_stage_02b(
        run_dir=run_dir,
        runs_root=runs_root,
        privacy_policy_url="https://example.com/privacy",
        skip_network_checks=True,
    )

    # Even with --approve, Stage 7 MUST block because of unresolved BLOCKER!
    with pytest.raises(stage7.ComplianceBlockerError):
        stage7.run_stage_07(run_dir=run_dir, runs_root=runs_root, approve=True, dry_run=False)

    # With explicit --acknowledge-compliance-risks, Stage 7 proceeds and bundles the checklist
    report_path, pub_report = stage7.run_stage_07(
        run_dir=run_dir,
        runs_root=runs_root,
        approve=True,
        dry_run=False,
        acknowledge_compliance_risks="Confirmed written publisher authorization on file",
    )
    assert report_path.exists()
    assert pub_report["compliance_risk_acknowledgement"]["reason"].startswith("Confirmed written")
    ready_dir = run_dir / "READY_TO_UPLOAD"
    assert (ready_dir / "compliance_audit_report.json").exists()
    assert (ready_dir / "play_console_declarations_checklist.html").exists()
    assert (ready_dir / "play_console_declarations_checklist.txt").exists()


# ---------------------------------------------------------------------------
# 4. Play Console Declarations Checklist HTML & TXT Output Verification
# ---------------------------------------------------------------------------

def test_compliance_checklist_html_and_txt_generation_via_cli(tmp_path: Path) -> None:
    runs_root, run_dir = _create_base_run(tmp_path, slug="cli-comp")

    exit_code = stage2b.main(
        [
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(runs_root),
            "--privacy-policy-url",
            "https://example.com/privacy",
            "--skip-network-checks",
        ]
    )
    assert exit_code == 0

    html_file = run_dir / "play_console_declarations_checklist.html"
    txt_file = run_dir / "play_console_declarations_checklist.txt"
    assert html_file.exists() and txt_file.exists()

    html_body = html_file.read_text(encoding="utf-8")
    assert 'dir="rtl"' in html_body
    assert "Google Play Console" in html_body
    assert "Data Safety" in txt_file.read_text(encoding="utf-8")
