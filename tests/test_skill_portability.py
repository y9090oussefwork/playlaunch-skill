"""Automated tests for Pure Portable Agent Skill compliance & CWD-independent execution.

Verifies:
  1. SKILL.md follows the Open Agent Skill standard (YAML frontmatter `name: playlaunch`,
     clear `description`, safety locks, valid links to all `references/*.md`, zero desktop UI leftovers).
  2. AGENTS.md, GEMINI.md, .cursorrules, and .antigravity/workflows/full-release.md point to SKILL.md.
  3. Stages execute cleanly from an external working directory (outside SKILL_ROOT) using
     `--project-dir "."` auto-detection for both Kotlin Android and Flutter projects, without
     writing a single file into the external project directory.
  4. install-skill.ps1 cleanly installs into a custom temporary target directory.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from core import SKILL_ROOT


def test_skill_md_frontmatter_references_and_safety_rules() -> None:
    skill_md = SKILL_ROOT / "SKILL.md"
    assert skill_md.exists(), "SKILL.md must exist at repository root"
    content = skill_md.read_text(encoding="utf-8")

    # Validate YAML frontmatter
    fm_match = re.match(r"^---\r?\n(.*?)\r?\n---", content, re.DOTALL)
    assert fm_match is not None, "SKILL.md must begin with valid YAML frontmatter"
    frontmatter = fm_match.group(1)
    assert re.search(r"^name:\s*playlaunch\s*$", frontmatter, re.MULTILINE)
    desc_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
    assert desc_match is not None and len(desc_match.group(1).strip()) >= 40

    # Validate mandatory safety guardrails
    for flag in ("--approve", "--promote-production", "--acknowledge-compliance-risks", "--sync-to-audio-workspace"):
        assert flag in content, f"SKILL.md must document safety lock for {flag}"

    # Validate zero deleted desktop UI leftovers
    for banned in ("Launch_Studio.bat", "127.0.0.1:5055", "ui/server.py", "pywebview"):
        assert banned not in content, f"SKILL.md must not reference removed desktop UI artifact '{banned}'"

    # Validate every references/*.md link resolves to an existing non-empty file
    expected_refs = [
        "references/aso-and-copy-playbook.md",
        "references/google-play-policies.md",
        "references/account-isolation-rules.md",
        "references/visuals-and-rtl-guide.md",
        "references/audio-workspace-bridge.md",
    ]
    for ref in expected_refs:
        assert ref in content, f"SKILL.md missing link to {ref}"
        ref_path = SKILL_ROOT / ref
        assert ref_path.exists() and ref_path.stat().st_size > 200, f"Reference missing or empty: {ref}"

    # Verify desktop UI files were fully removed
    assert not (SKILL_ROOT / "ui").exists()
    assert not (SKILL_ROOT / "Launch_Studio.bat").exists()


def test_agent_entrypoints_point_to_skill_md_without_ui_leftovers() -> None:
    for fname in ("AGENTS.md", "GEMINI.md", ".cursorrules", ".antigravity/workflows/full-release.md"):
        p = SKILL_ROOT / fname
        assert p.exists(), f"Missing agent entrypoint: {fname}"
        text = p.read_text(encoding="utf-8")
        assert "SKILL.md" in text
        assert "Launch_Studio.bat" not in text
        assert "127.0.0.1:5055" not in text


def test_cwd_independent_execution_from_external_kotlin_project(tmp_path: Path) -> None:
    ext_proj = tmp_path / "MyExternalKotlinApp"
    (ext_proj / "app" / "src" / "main" / "res" / "values").mkdir(parents=True)
    (ext_proj / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi").mkdir(parents=True)
    (ext_proj / "app" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)

    (ext_proj / "settings.gradle.kts").write_text('rootProject.name = "MyExternalKotlinApp"\n', encoding="utf-8")
    (ext_proj / "app" / "build.gradle.kts").write_text(
        'android { defaultConfig { applicationId = "com.acme.portableapp" } }\n',
        encoding="utf-8",
    )
    (ext_proj / "app" / "src" / "main" / "res" / "values" / "strings.xml").write_text(
        '<resources><string name="app_name">Portable Focus</string></resources>\n',
        encoding="utf-8",
    )
    (ext_proj / "app" / "src" / "main" / "java" / "com" / "acme" / "MainActivity.kt").write_text(
        "class MainActivity\n", encoding="utf-8"
    )
    (ext_proj / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher.png").write_bytes(
        b"\x89PNG\r\n\x1a\nFAKE"
    )

    before_files = sorted(str(p.relative_to(ext_proj)) for p in ext_proj.rglob("*") if p.is_file())
    isolated_runs = tmp_path / "isolated_runs"

    # Run Stage 1 directly with cwd = external project dir and --project-dir "."
    proc1 = subprocess.run(
        [
            sys.executable,
            str(SKILL_ROOT / "stages" / "01_intake_adapter.py"),
            "--mode",
            "standalone",
            "--project-dir",
            ".",
            "--runs-root",
            str(isolated_runs),
        ],
        cwd=str(ext_proj),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc1.returncode == 0, f"Stage 1 failed from external cwd: {proc1.stderr}\n{proc1.stdout}"
    # Parse trailing JSON object printed to stdout
    json_start = proc1.stdout.index("{")
    s1_out = json.loads(proc1.stdout[json_start:])
    assert s1_out["mode"] == "standalone"
    assert s1_out["package_name"] == "com.acme.portableapp"

    run_dir = Path(s1_out["run_dir"])
    manifest = json.loads((run_dir / "intake_manifest.json").read_text(encoding="utf-8"))
    assert manifest["app"]["display_name"] == "Portable Focus"
    assert manifest["app"]["framework"] == "kotlin"
    assert manifest["branding"]["icon_local_path"].endswith("ic_launcher.png")

    # Run Stage 2B Compliance Audit from the SAME external cwd!
    proc2b = subprocess.run(
        [
            sys.executable,
            str(SKILL_ROOT / "stages" / "02b_compliance_audit.py"),
            "--run-dir",
            str(run_dir),
            "--runs-root",
            str(isolated_runs),
            "--privacy-policy-url",
            "https://example.com/privacy",
            "--skip-network-checks",
        ],
        cwd=str(ext_proj),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc2b.returncode == 0, f"Stage 2B failed from external cwd: {proc2b.stderr}\n{proc2b.stdout}"
    assert (run_dir / "compliance_audit_report.json").exists()
    assert (run_dir / "play_console_declarations_checklist.html").exists()

    # ZERO files created inside the user's external Android project!
    after_files = sorted(str(p.relative_to(ext_proj)) for p in ext_proj.rglob("*") if p.is_file())
    assert before_files == after_files


def test_cwd_independent_flutter_project_auto_detection(tmp_path: Path) -> None:
    fl_proj = tmp_path / "MyFlutterNotes"
    (fl_proj / "android" / "app").mkdir(parents=True)
    (fl_proj / "pubspec.yaml").write_text(
        "name: my_flutter_notes\ndescription: Clean offline notes app\n", encoding="utf-8"
    )
    (fl_proj / "android" / "app" / "build.gradle").write_text(
        'android { defaultConfig { applicationId "com.acme.flutternotes" } }\n', encoding="utf-8"
    )

    isolated_runs = tmp_path / "isolated_runs_flutter"
    proc = subprocess.run(
        [
            sys.executable,
            str(SKILL_ROOT / "stages" / "01_intake_adapter.py"),
            "--mode",
            "standalone",
            "--project-dir",
            ".",
            "--runs-root",
            str(isolated_runs),
        ],
        cwd=str(fl_proj),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"Flutter intake failed: {proc.stderr}\n{proc.stdout}"
    s1_out = json.loads(proc.stdout[proc.stdout.index("{") :])
    run_dir = Path(s1_out["run_dir"])
    manifest = json.loads((run_dir / "intake_manifest.json").read_text(encoding="utf-8"))
    assert manifest["app"]["framework"] == "flutter"
    assert manifest["app"]["package_name"] == "com.acme.flutternotes"
    assert manifest["app"]["display_name"] == "My Flutter Notes"


def test_install_skill_ps1_copy_mode_into_isolated_target(tmp_path: Path) -> None:
    custom_target = tmp_path / "agent_skills" / "playlaunch"
    proc = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SKILL_ROOT / "install-skill.ps1"),
            "-Mode",
            "Copy",
            "-OnlyCustomTargets",
            "-Target",
            str(custom_target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"install-skill.ps1 failed: {proc.stderr}\n{proc.stdout}"
    assert (custom_target / "SKILL.md").exists()
    assert (custom_target / "SKILL_ROOT.txt").read_text(encoding="utf-8").strip() == str(SKILL_ROOT)
    assert (custom_target / "references" / "google-play-policies.md").exists()
    assert "<SKILL>" not in (custom_target / "SKILL.md").read_text(encoding="utf-8")
