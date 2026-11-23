---
description: Run PlayLaunch portable skill (Stages 1 -> 6 + mandatory Stage 2B Compliance Audit) from any Android/Flutter project or Audio Workspace, pausing cleanly for chat review.
---

# PlayLaunch — Full Release & Compliance Workflow

Read **`SKILL.md`** first (Single Source of Truth). Let `$SKILL` be the PlayLaunch skill directory and `$PY = "$SKILL\.venv\Scripts\python.exe"`.

> **MANDATORY SAFETY LOCK:** Never pass `--approve`, `--promote-production`, `--acknowledge-compliance-risks`, `--sync-to-audio-workspace`, or run `stages/07_isolated_publish.py` automatically. Always pause at `awaiting_human_approval` and present preview files directly in chat.

1. **Stage 1 — Intake:**
   - Inside any open Android/Flutter project: `& $PY "$SKILL\stages\01_intake_adapter.py" --mode standalone --project-dir "." --locales ar,en-US`
   - Or Audio Workspace (read-only): `& $PY "$SKILL\stages\01_intake_adapter.py" --mode audio_workspace --app-slug <slug> --locales ar,en-US`
2. **Stage 2 — Static Audit & Vitals Risks (JBR enforced):**
   `& $PY "$SKILL\stages\02_audit_and_build.py" --run-dir "<RUN_DIR>" --audit-only`
3. **Stage 3 — Raw Screen Capture (ADB or graceful fallback):**
   `& $PY "$SKILL\stages\03_capture_screens.py" --run-dir "<RUN_DIR>"`
4. **Stage 4 — Live ASO & Competitor Review Mining:**
   `& $PY "$SKILL\stages\04_aso_research.py" --run-dir "<RUN_DIR>"`
5. **Stage 5 — Prepare Brief → Write Bilingual Copy → Validate Policy Guard:**
   - `& $PY "$SKILL\stages\05_copy_and_policy_guard.py" prepare --run-dir "<RUN_DIR>"`
   - Write localized Fastlane copy (`metadata/android/<locale>/`) & `screenshot_headlines.json` using `<RUN_DIR>\copy_brief.json`.
   - `& $PY "$SKILL\stages\05_copy_and_policy_guard.py" validate --run-dir "<RUN_DIR>"` (repeat until `passed: true`).
6. **Stage 6 — Render Store Visuals (`512×512`, `1024×500`, `1080×1920` AR RTL + EN LTR):**
   `& $PY "$SKILL\stages\06_render_visuals.py" --run-dir "<RUN_DIR>"`
7. **Stage 2B — Mandatory Google Play Compliance & Rejection Prevention Audit:**
   `& $PY "$SKILL\stages\02b_compliance_audit.py" --run-dir "<RUN_DIR>" [--artifact "<path.aab|.apk>"] [--privacy-policy-url "https://..."]`
8. **Stop & Hand Off in Chat:**
   Present ASO highlights, copy char counts, Compliance Readiness Score (`BLOCKERS`/`WARNINGS`), and direct links to `play_console_declarations_checklist.html`, `compliance_audit_report.json`, and generated PNG visuals for user review before any Stage 7 action.
