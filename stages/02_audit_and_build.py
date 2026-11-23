"""Stage 2 — Static Audit, Vitals Risk Check, Release Build & Signature Verification (stages/02_audit_and_build.py).

- Auto-skips in Mode A (`audio_workspace`) when a verified signed `.aab` already exists.
- For Mode B (`standalone` Kotlin/Java/Flutter):
    * Kotlin: runs unmodified `vendors/detekt/detekt-cli-*-all.jar` + `gradlew.bat lintRelease` + `gradlew.bat :<module>:bundleRelease`
    * Java:   runs `gradlew.bat lintRelease` + `gradlew.bat :<module>:bundleRelease`
    * Flutter: runs `flutter analyze` + `flutter build appbundle --release`
    * Scans source/Gradle files for Android Vitals Crash/ANR bad-behavior risk patterns.
    * Verifies release `.aab` signature via `jarsigner -verify`.
- MANDATORY: Passes `core.env_resolver.build_jvm_env()` (Android Studio JBR) to ALL
  `detekt`, `gradlew.bat`, and `jarsigner` invocations so system Java 23 is never picked.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.config_loader import load_play_policies, load_studio_config
from core.env_resolver import build_jvm_env, resolve_environment
from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso
from core.validators import sha256_file

logger = get_logger("stage.02_audit_and_build")

# Heuristic patterns linked to Google Play Android Vitals bad behavior (ANR > 0.47% / Crash > 1.09%)
VITALS_CODE_RULES: tuple[dict[str, str], ...] = (
    {
        "id": "MAIN_THREAD_SLEEP",
        "severity": "high",
        "pattern": r"\bThread\.sleep\s*\(",
        "message": "Thread.sleep() detected; blocking the main/UI thread risks ANR bad behavior (>0.47%).",
    },
    {
        "id": "ROOM_MAIN_THREAD_QUERIES",
        "severity": "high",
        "pattern": r"\.allowMainThreadQueries\s*\(",
        "message": "Room allowMainThreadQueries() enabled; synchronous DB I/O on UI thread causes ANRs.",
    },
    {
        "id": "RUN_BLOCKING_UI",
        "severity": "medium",
        "pattern": r"\brunBlocking\s*\{",
        "message": "Kotlin runBlocking {} found; ensure it is never called from Activity/Fragment/Application main thread.",
    },
    {
        "id": "STRICT_UNSAFE_FORCE_UNWRAP",
        "severity": "low",
        "pattern": r"!!\.",
        "message": "Kotlin non-null assertion (!!.) found; verify null-safety to avoid NullPointerException crashes.",
    },
)


def scan_android_vitals_risks(source_root: Path, policies: dict[str, Any]) -> dict[str, Any]:
    """Static scan for ANR/Crash/release-shrink risk patterns against Play Vitals thresholds."""
    thresholds = policies.get("vitals_thresholds", {})
    findings: list[dict[str, Any]] = []
    scanned_files = 0
    oversized_assets: list[dict[str, Any]] = []

    if not source_root.exists():
        return {
            "scanned": False,
            "thresholds": thresholds,
            "findings": [],
            "oversized_assets": [],
            "high_severity_count": 0,
        }

    # 1. Scan Kotlin/Java/Dart source files
    code_exts = {".kt", ".java", ".dart"}
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        parts_lower = {p.lower() for p in path.parts}
        if any(skip in parts_lower for skip in ("build", ".gradle", ".git", "test", "androidtest")):
            continue

        # Check oversized raw assets (> 10 MB)
        if ("assets" in parts_lower or "raw" in parts_lower) and path.stat().st_size > 10 * 1024 * 1024:
            oversized_assets.append(
                {
                    "file": str(path),
                    "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
                    "severity": "medium",
                    "message": "Single asset exceeds 10 MB; consider compression or Play Asset Delivery.",
                }
            )

        if path.suffix.lower() in code_exts:
            scanned_files += 1
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for rule in VITALS_CODE_RULES:
                for match in re.finditer(rule["pattern"], content):
                    line_no = content.count("\n", 0, match.start()) + 1
                    findings.append(
                        {
                            "rule_id": rule["id"],
                            "severity": rule["severity"],
                            "file": str(path),
                            "line": line_no,
                            "message": rule["message"],
                        }
                    )
                    if len(findings) >= 100:
                        break

    # 2. Check Gradle release minify/shrink config
    for gradle_name in ("build.gradle", "build.gradle.kts"):
        for gfile in source_root.rglob(gradle_name):
            if "build" in {p.lower() for p in gfile.parts}:
                continue
            try:
                gtxt = gfile.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if re.search(r"(?:isMinifyEnabled|minifyEnabled)\s*=?\s*false", gtxt):
                findings.append(
                    {
                        "rule_id": "RELEASE_MINIFY_DISABLED",
                        "severity": "medium",
                        "file": str(gfile),
                        "line": 1,
                        "message": "minifyEnabled=false detected; enable R8 minification & resource shrinking for release AAB.",
                    }
                )

    high_count = sum(1 for f in findings if f["severity"] == "high")
    return {
        "scanned": True,
        "scanned_code_files": scanned_files,
        "thresholds": thresholds,
        "findings": findings,
        "oversized_assets": oversized_assets,
        "high_severity_count": high_count,
    }


def _run_tool_subprocess(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "executed": True,
            "cmd": cmd,
            "returncode": proc.returncode,
            "passed": proc.returncode == 0,
            "java_home_used": env.get("JAVA_HOME"),
            "stdout_tail": (proc.stdout or "").strip()[-1500:],
            "stderr_tail": (proc.stderr or "").strip()[-1500:],
        }
    except subprocess.TimeoutExpired:
        return {
            "executed": True,
            "cmd": cmd,
            "returncode": -1,
            "passed": False,
            "java_home_used": env.get("JAVA_HOME"),
            "error": f"Command timed out after {timeout_seconds}s.",
        }
    except OSError as exc:
        return {
            "executed": False,
            "cmd": cmd,
            "returncode": -1,
            "passed": False,
            "java_home_used": env.get("JAVA_HOME"),
            "error": str(exc),
        }


def verify_aab_with_jbr(aab_path: Path, jvm_env: dict[str, str], jarsigner_bin: str | None) -> dict[str, Any]:
    if not jarsigner_bin:
        return {"verified": False, "error": "jarsigner binary not found in Android Studio JBR."}
    res = _run_tool_subprocess(
        [jarsigner_bin, "-verify", str(aab_path)],
        cwd=aab_path.parent,
        env=jvm_env,
        timeout_seconds=60,
    )
    combined = (res.get("stdout_tail", "") + "\n" + res.get("stderr_tail", "")).lower()
    res["verified"] = bool(res.get("passed") and "jar verified" in combined)
    res["sha256"] = sha256_file(aab_path)
    res["size_bytes"] = aab_path.stat().st_size
    return res


def run_stage_02(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    audit_only: bool = False,
    skip_detekt: bool = False,
    config_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Execute Stage 2 Static Audit, Vitals Risk Check, Build & Signing Verification."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    manifest = json.loads((manager.run_dir / "intake_manifest.json").read_text(encoding="utf-8"))
    config = load_studio_config(config_path=config_path)
    policies = load_play_policies()
    env_info = resolve_environment(config=config)
    jvm_env = build_jvm_env(config=config)

    # 1. Auto-skip in Mode A if a verified signed .aab already exists
    if manifest.get("mode") == "audio_workspace" and manifest.get("stage2_auto_skip"):
        aab_path = Path(manifest["release_artifact"]["path"])
        if aab_path.exists():
            report = {
                "schema_version": 1,
                "generated_at": utc_now_iso(),
                "mode": "audio_workspace",
                "auto_skipped": True,
                "reason": "Pre-built, signature-verified release .aab provided by audio_workspace intake.",
                "java_home_used": jvm_env.get("JAVA_HOME"),
                "release_aab": manifest["release_artifact"],
            }
            report_path = manager.write_json_artifact("audit_and_build_report.json", report)
            manager.update_stage(
                state,
                "02_audit_and_build",
                "skipped",
                message="Auto-skipped: verified signed release .aab already present from audio_workspace.",
                artifacts={
                    "audit_and_build_report": str(report_path),
                    "release_aab": str(aab_path),
                },
            )
            return report_path, report

    manager.update_stage(state, "02_audit_and_build", "running", message="Running static audit & build checks (JBR JAVA_HOME)")

    app = manifest.get("app", {})
    framework = str(app.get("framework") or "kotlin").lower()
    android_module = str(app.get("android_module") or "app")
    warnings: list[str] = []

    if manifest.get("mode") == "audio_workspace":
        # Audio workspace is strictly read-only — never build inside Youssef_Audio_Workspace!
        warnings.append(
            "Audio workspace mode is strictly read-only and no verified .aab was located; "
            "build the release .aab inside Youssef_Audio_Workspace first, then re-run Stage 1."
        )
        report = {
            "schema_version": 1,
            "generated_at": utc_now_iso(),
            "mode": "audio_workspace",
            "auto_skipped": False,
            "read_only_guarded": True,
            "java_home_used": jvm_env.get("JAVA_HOME"),
            "warnings": warnings,
        }
        report_path = manager.write_json_artifact("audit_and_build_report.json", report)
        manager.update_stage(
            state,
            "02_audit_and_build",
            "skipped",
            message="Audio workspace is read-only; no local build executed (manual build required in workspace).",
            artifacts={"audit_and_build_report": str(report_path)},
            warnings=warnings,
        )
        return report_path, report

    # Mode B (Standalone)
    source_raw = (manifest.get("source") or {}).get("source_path")
    source_path = Path(source_raw) if source_raw else None
    if not source_path or not source_path.exists():
        msg = f"Standalone source_path does not exist: {source_raw}"
        report = {
            "schema_version": 1,
            "generated_at": utc_now_iso(),
            "mode": "standalone",
            "framework": framework,
            "java_home_used": jvm_env.get("JAVA_HOME"),
            "error": msg,
        }
        report_path = manager.write_json_artifact("audit_and_build_report.json", report)
        manager.update_stage(
            state,
            "02_audit_and_build",
            "failed",
            message=msg,
            artifacts={"audit_and_build_report": str(report_path)},
        )
        return report_path, report

    # 2. Android Vitals static risk scan
    vitals_risks = scan_android_vitals_risks(source_path, policies=policies)

    static_checks: dict[str, Any] = {}
    build_result: dict[str, Any] = {"skipped": audit_only}
    aab_result: dict[str, Any] = {"found": False, "path": None, "signature_verified": False}
    overall_ok = True

    if framework in ("kotlin", "java"):
        # Detekt (Kotlin only, strictly via Android Studio JBR)
        if framework == "kotlin" and not skip_detekt:
            if env_info.java_bin and env_info.detekt_jar:
                detekt_report_txt = manager.run_dir / "detekt_report.txt"
                detekt_res = _run_tool_subprocess(
                    [
                        env_info.java_bin,
                        "-jar",
                        env_info.detekt_jar,
                        "--input",
                        str(source_path),
                        "--report",
                        f"txt:{detekt_report_txt}",
                    ],
                    cwd=source_path,
                    env=jvm_env,
                    timeout_seconds=180,
                )
                detekt_res["report_file"] = str(detekt_report_txt) if detekt_report_txt.exists() else None
                static_checks["detekt"] = detekt_res
                if not detekt_res.get("passed"):
                    warnings.append("Detekt reported code quality issues (see detekt_report.txt).")
            else:
                warnings.append("Detekt JAR or JBR java.exe unavailable; skipped Detekt.")

        gradlew = source_path / "gradlew.bat"
        if gradlew.exists():
            if not audit_only:
                lint_res = _run_tool_subprocess(
                    [str(gradlew), "lintRelease"],
                    cwd=source_path,
                    env=jvm_env,
                    timeout_seconds=300,
                )
                static_checks["gradle_lint_release"] = lint_res
                if not lint_res.get("passed"):
                    warnings.append("gradlew lintRelease reported issues.")

                bundle_res = _run_tool_subprocess(
                    [str(gradlew), f":{android_module}:bundleRelease"],
                    cwd=source_path,
                    env=jvm_env,
                    timeout_seconds=600,
                )
                build_result = bundle_res
                if not bundle_res.get("passed"):
                    overall_ok = False
                else:
                    aab_candidates = sorted(
                        (source_path / android_module / "build" / "outputs" / "bundle").rglob("*.aab"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if aab_candidates:
                        aab_path = aab_candidates[0]
                        sig = verify_aab_with_jbr(aab_path, jvm_env=jvm_env, jarsigner_bin=env_info.jarsigner_bin)
                        aab_result = {
                            "found": True,
                            "path": str(aab_path),
                            "signature_verified": bool(sig.get("verified")),
                            "signature_check": sig,
                        }
                        if not sig.get("verified"):
                            warnings.append("Built .aab failed jarsigner verification.")
            else:
                static_checks["gradle_lint_release"] = {"skipped": True, "reason": "audit_only=True"}
        else:
            warnings.append(f"gradlew.bat not found in {source_path}.")
            if not audit_only:
                overall_ok = False

    elif framework == "flutter":
        flutter_bin = env_info.flutter_bin or "flutter"
        analyze_res = _run_tool_subprocess(
            [flutter_bin, "analyze"],
            cwd=source_path,
            env=jvm_env,
            timeout_seconds=240,
        )
        static_checks["flutter_analyze"] = analyze_res
        if not analyze_res.get("passed"):
            warnings.append("flutter analyze reported issues.")

        if not audit_only:
            build_res = _run_tool_subprocess(
                [flutter_bin, "build", "appbundle", "--release"],
                cwd=source_path,
                env=jvm_env,
                timeout_seconds=600,
            )
            build_result = build_res
            if not build_res.get("passed"):
                overall_ok = False
            else:
                aab_path = source_path / "build" / "app" / "outputs" / "bundle" / "release" / "app-release.aab"
                if aab_path.exists():
                    sig = verify_aab_with_jbr(aab_path, jvm_env=jvm_env, jarsigner_bin=env_info.jarsigner_bin)
                    aab_result = {
                        "found": True,
                        "path": str(aab_path),
                        "signature_verified": bool(sig.get("verified")),
                        "signature_check": sig,
                    }

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "mode": "standalone",
        "framework": framework,
        "audit_only": audit_only,
        "java_home_used": jvm_env.get("JAVA_HOME"),
        "java_source": env_info.java_source,
        "java_version": env_info.java_version,
        "vitals_risk_report": vitals_risks,
        "static_checks": static_checks,
        "build_result": build_result,
        "release_aab": aab_result,
        "warnings": warnings,
    }

    report_path = manager.write_json_artifact("audit_and_build_report.json", report)
    artifacts: dict[str, str] = {"audit_and_build_report": str(report_path)}
    if aab_result.get("found") and aab_result.get("path"):
        artifacts["release_aab"] = str(aab_result["path"])

    manager.update_stage(
        state,
        "02_audit_and_build",
        "completed" if overall_ok else "failed",
        message=(
            f"Stage 2 {'audit-only ' if audit_only else ''}completed using JBR ({env_info.java_version})."
            if overall_ok
            else "Stage 2 build failed — inspect audit_and_build_report.json."
        ),
        artifacts=artifacts,
        warnings=warnings,
    )
    return report_path, report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 2 Audit & Build (JBR enforced)")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--audit-only", action="store_true", help="Run static & Vitals audit without building .aab")
    parser.add_argument("--skip-detekt", action="store_true")
    parser.add_argument("--config", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report_path, report = run_stage_02(
        run_dir=args.run_dir,
        runs_root=args.runs_root,
        audit_only=args.audit_only,
        skip_detekt=args.skip_detekt,
        config_path=args.config,
    )
    print(
        json.dumps(
            {
                "status": "ok" if not report.get("error") else "failed",
                "audit_and_build_report": str(report_path),
                "java_home_used": report.get("java_home_used"),
                "auto_skipped": report.get("auto_skipped", False),
                "high_vitals_risks": (report.get("vitals_risk_report") or {}).get("high_severity_count", 0),
                "warnings_count": len(report.get("warnings", [])),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
