"""Master Pipeline Orchestrator (stages/run_pipeline.py).

Orchestrates all 7 stages sequentially using run_state.json for full resumability:
  - Stage 1: 01_intake_adapter.py
  - Stage 2: 02_audit_and_build.py (Android Studio JBR enforced)
  - Stage 3: 03_capture_screens.py (ADB or pre-captured graceful fallback)
  - Stage 4: 04_aso_research.py
  - Stage 5: 05_copy_and_policy_guard.py (prepare / validate)
  - Stage 6: 06_render_visuals.py (pauses at awaiting_human_approval)
  - Stage 7: 07_isolated_publish.py (requires --approve or --dry-run)
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT

logger = get_logger("pipeline.orchestrator")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Master 7-Stage Pipeline Orchestrator")
    parser.add_argument("--mode", choices=("auto", "audio_workspace", "standalone"), default="auto")
    parser.add_argument("--app-slug", help="App slug")
    parser.add_argument("--flavor", help="Android flavor (Mode A)")
    parser.add_argument("--project-dir", type=Path, help="Standalone Android/Flutter project directory (e.g. \".\")")
    parser.add_argument("--app-profile", type=Path, help="Standalone app profile path (Mode B)")
    parser.add_argument("--account-profile", type=Path, help="Developer account profile path")
    parser.add_argument("--locales", help="Comma-separated locales (e.g. ar,en-US)")
    parser.add_argument("--from-stage", type=int, default=1, choices=range(1, 8))
    parser.add_argument("--to-stage", type=int, default=6, choices=range(1, 8))
    parser.add_argument("--stage5-action", choices=("prepare", "validate"), default="prepare")
    parser.add_argument("--audit-only", action="store_true", help="Stage 2: static & Vitals audit only")
    parser.add_argument("--approve", action="store_true", help="Stage 7: explicit human approval")
    parser.add_argument("--dry-run", action="store_true", help="Stage 7: simulate without contacting Google Play")
    parser.add_argument("--sync-to-audio-workspace", action="store_true", help="Stage 7: explicit opt-in sync")
    parser.add_argument("--run-dir", type=Path, help="Resume an existing run directory")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    args = parser.parse_args(argv)

    if args.from_stage > args.to_stage:
        parser.error("--from-stage cannot be greater than --to-stage")

    locales = [x.strip() for x in args.locales.split(",") if x.strip()] if args.locales else None
    current_run_dir = args.run_dir

    if args.from_stage <= 1 <= args.to_stage:
        stage1 = importlib.import_module("stages.01_intake_adapter")
        current_run_dir, _ = stage1.run_stage_01(
            mode=args.mode,
            app_slug=args.app_slug,
            flavor=args.flavor,
            app_profile=args.app_profile,
            project_dir=args.project_dir,
            account_profile=args.account_profile,
            locales=locales,
            run_dir=current_run_dir,
            runs_root=args.runs_root,
        )

    if current_run_dir is None:
        parser.error("--run-dir is required when starting after Stage 1.")

    if args.from_stage <= 2 <= args.to_stage:
        stage2 = importlib.import_module("stages.02_audit_and_build")
        stage2.run_stage_02(run_dir=current_run_dir, runs_root=args.runs_root, audit_only=args.audit_only)

    if args.from_stage <= 3 <= args.to_stage:
        stage3 = importlib.import_module("stages.03_capture_screens")
        stage3.run_stage_03(run_dir=current_run_dir, runs_root=args.runs_root)

    if args.from_stage <= 4 <= args.to_stage:
        stage4 = importlib.import_module("stages.04_aso_research")
        stage4.run_stage_04(run_dir=current_run_dir, runs_root=args.runs_root)

    if args.from_stage <= 5 <= args.to_stage:
        stage5 = importlib.import_module("stages.05_copy_and_policy_guard")
        if args.stage5_action == "prepare":
            stage5.prepare_copy_brief(run_dir=current_run_dir, runs_root=args.runs_root)
        else:
            _, rep = stage5.validate_copy_run(run_dir=current_run_dir, runs_root=args.runs_root)
            if not rep["passed"]:
                logger.error("Stage 5 policy validation failed. Halting before Stage 6.")
                return 1

    if args.from_stage <= 6 <= args.to_stage:
        stage6 = importlib.import_module("stages.06_render_visuals")
        stage6.run_stage_06(run_dir=current_run_dir, runs_root=args.runs_root)
        # Mandatory pre-submission Google Play Compliance & Rejection Prevention audit
        stage2b = importlib.import_module("stages.02b_compliance_audit")
        stage2b.run_stage_02b(run_dir=current_run_dir, runs_root=args.runs_root)

    if args.from_stage <= 7 <= args.to_stage:
        stage7 = importlib.import_module("stages.07_isolated_publish")
        stage7.run_stage_07(
            run_dir=current_run_dir,
            runs_root=args.runs_root,
            approve=args.approve,
            dry_run=args.dry_run,
            sync_to_audio_workspace=args.sync_to_audio_workspace,
            account_profile_path=args.account_profile,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
