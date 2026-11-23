---
name: playlaunch
description: Standalone, CWD-independent Google Play Growth, ASO, Arabic/English Store Copy & Visuals, Policy Compliance & Rejection Prevention Audit, and Strictly-Isolated Publishing engine for Android (Kotlin/Java/Flutter) apps. Activate whenever the user asks to optimize a Google Play listing, run ASO competitor/review research, audit an .aab/.apk for Google Play rejection risks (Manifest, permissions, 16 KB alignment, Data Safety, AdMob/UMP/Families, IP/impersonation), render localized AR RTL / EN LTR store screenshots and graphics, or prepare an isolated upload bundle.
---

# PlayLaunch — Portable Google Play Growth, Compliance & Isolated Publishing Skill

Use this skill from **any open Android/Flutter project directory** (or against `Youssef_Audio_Workspace` in read-only Mode A). All scripts resolve `SKILL_ROOT` from their own file location (`Path(__file__).resolve().parent.parent`), never from `cwd`.

Let `<SKILL>` be the directory containing this `SKILL.md` (e.g., `D:\Projects\PlayLaunch_Studio`), and always run Python via:
```powershell
$PY = "<SKILL>\.venv\Scripts\python.exe"
```

---

## Non-Negotiable Safety Rules (ZERO EXCEPTIONS)

1. **MANDATORY HUMAN APPROVAL & COMPLIANCE BLOCKER LOCK:**
   - **NEVER** pass `--approve`, `--promote-production`, `--acknowledge-compliance-risks`, `--sync-to-audio-workspace`, or run `stages/07_isolated_publish.py` automatically without explicit, unambiguous permission from the user in the **same message**.
   - Always stop cleanly after Stage 6 + Stage 2B (`overall_status: awaiting_human_approval`) and present the generated preview files directly in chat (`listing_copy.html`, `play_console_declarations_checklist.html`, and rendered PNG screenshots/feature graphics under `workspace_runs/`).
2. **ZERO TOUCH ON `vendors/` & EXTERNAL WORKSPACES:**
   - Never edit files inside `<SKILL>\vendors\`.
   - Never write files into `D:\Projects\Youssef_Audio_Workspace` (read-only Mode A) or the user's open Android/Flutter project folder. All outputs stay strictly inside `<SKILL>\workspace_runs\<app-slug>\<run-timestamp>\`.
3. **ENVIRONMENT DISCIPLINE:**
   - Always use `<SKILL>\.venv\Scripts\python.exe` (Python 3.12).
   - `core/env_resolver.py` enforces **Android Studio JBR** as `JAVA_HOME` for `detekt`, `gradlew.bat`, and `jarsigner` (never system Java 23).
4. **ZERO SECRET EXPOSURE:**
   - Never read, print, or commit `secrets/`, real `profiles/`, `config/studio.config.json`, or `security/*.sqlite3`.

---

## Operating Modes (Pick Only What the User Asked For)

### Mode 1: Full Optimization & Compliance Pipeline (`Stages 1 → 6 + 2B`)
Use when the user says: *"Optimize this app for Google Play in Arabic and English and check compliance before publishing."*

1. **Stage 1 — Intake (from current project folder OR Audio Workspace):**
   - From any open Kotlin/Java/Flutter project folder (zero manual profile needed):
     ```powershell
     & $PY "<SKILL>\stages\01_intake_adapter.py" --mode standalone --project-dir "." --locales ar,en-US
     ```
   - Or for `Youssef_Audio_Workspace` (strictly read-only):
     ```powershell
     & $PY "<SKILL>\stages\01_intake_adapter.py" --mode audio_workspace --app-slug <slug> --locales ar,en-US
     ```
2. **Stage 2 — Static Audit, Vitals Risks & Build (JBR enforced, auto-skipped if verified AAB exists):**
   ```powershell
   & $PY "<SKILL>\stages\02_audit_and_build.py" --run-dir "<RUN_DIR>" --audit-only
   ```
3. **Stage 3 — Raw Screen Capture (ADB if attached; gracefully falls back to `raw_screenshots/` or `skipped`):**
   ```powershell
   & $PY "<SKILL>\stages\03_capture_screens.py" --run-dir "<RUN_DIR>"
   ```
4. **Stage 4 — Live ASO Intelligence & 1–2 Star Competitor Review Mining:**
   ```powershell
   & $PY "<SKILL>\stages\04_aso_research.py" --run-dir "<RUN_DIR>"
   ```
5. **Stage 5 — Copy Brief, Localization & Policy Guard:**
   ```powershell
   & $PY "<SKILL>\stages\05_copy_and_policy_guard.py" prepare --run-dir "<RUN_DIR>"
   ```
   Read `<RUN_DIR>\copy_brief.json` and write high-converting localized copy into `<RUN_DIR>\metadata\android\<locale>\{title.txt,short_description.txt,full_description.txt,changelogs\1.txt}` + `<RUN_DIR>\screenshot_headlines.json`, then validate until `passed: true`:
   ```powershell
   & $PY "<SKILL>\stages\05_copy_and_policy_guard.py" validate --run-dir "<RUN_DIR>"
   ```
6. **Stage 6 — Visual & Screenshot Rendering (`512×512`, `1024×500`, `1080×1920` AR RTL / EN LTR):**
   ```powershell
   & $PY "<SKILL>\stages\06_render_visuals.py" --run-dir "<RUN_DIR>"
   ```
7. **Stage 2B — Mandatory Google Play Compliance & Rejection Prevention Audit:**
   ```powershell
   & $PY "<SKILL>\stages\02b_compliance_audit.py" --run-dir "<RUN_DIR>" [--artifact "<PATH_TO_AAB_OR_APK>"] [--privacy-policy-url "https://..."]
   ```
8. **STOP & Present Review Package in Chat:**
   Summarize ASO insights, copy char counts, Compliance Readiness Score (`BLOCKERS`/`WARNINGS`), and link directly to:
   - `<RUN_DIR>\play_console_declarations_checklist.html`
   - `<RUN_DIR>\visuals\screenshots\<locale>\01.png` & `feature_graphic.png`
   - `<RUN_DIR>\compliance_audit_report.json`

### Mode 2: Compliance & Rejection Audit Only
Run Stage 1 (`--project-dir "."` or `--app-slug`) followed directly by `stages\02b_compliance_audit.py --run-dir "<RUN_DIR>" --artifact "<path.aab|.apk>" --privacy-policy-url "<https-url>"`. Read `references/google-play-policies.md`.

### Mode 3: ASO Research & Store Copy Only
Run Stages `1 → 4 → 5 (prepare -> write copy -> validate)`. Read `references/aso-and-copy-playbook.md`.

### Mode 4: Store Visuals & Arabic RTL Screenshots Only
Ensure `<RUN_DIR>\screenshot_headlines.json` and localized metadata exist, then run `stages\06_render_visuals.py --run-dir "<RUN_DIR>"`. Read `references/visuals-and-rtl-guide.md`.

### Mode 5: Post-Approval Manual Bundle Export or Isolated Draft Publish (USER PERMISSION REQUIRED)
**Only after the user explicitly approves in chat:**
- Safe dry-run simulation (allowed anytime):
  ```powershell
  & $PY "<SKILL>\stages\07_isolated_publish.py" --run-dir "<RUN_DIR>" --dry-run
  ```
- Export isolated manual transfer bundle (`READY_TO_UPLOAD/` — default, zero Play Console network calls):
  ```powershell
  & $PY "<SKILL>\stages\07_isolated_publish.py" --run-dir "<RUN_DIR>" --approve
  ```
  Then instruct the user to open `<RUN_DIR>\READY_TO_UPLOAD\listing_copy.html` and `play_console_declarations_checklist.html`.
- Opt-in isolated Play Console **Draft** upload (strictly gated by `security/isolation_guard.py` — read `references/account-isolation-rules.md` first):
  ```powershell
  & $PY "<SKILL>\stages\07_isolated_publish.py" --run-dir "<RUN_DIR>" --approve --publish-mode isolated_auto_publish
  ```

---

## Progressive Disclosure References (Read Only What You Need)

| Task Area | Reference File |
| :--- | :--- |
| ASO research, Arabic normalization & Policy Guard rules | [references/aso-and-copy-playbook.md](references/aso-and-copy-playbook.md) |
| Google Play rejection prevention, Manifest, 16 KB, Data Safety & Families | [references/google-play-policies.md](references/google-play-policies.md) |
| Fail-Closed IPv4 isolation, `/24` subnet lock, Proxy & Wi-Fi SSID guard | [references/account-isolation-rules.md](references/account-isolation-rules.md) |
| Visual templates, Arabic RTL Chromium renderer & Play dimensions | [references/visuals-and-rtl-guide.md](references/visuals-and-rtl-guide.md) |
| Read-only integration with `Youssef_Audio_Workspace` (Mode A) | [references/audio-workspace-bridge.md](references/audio-workspace-bridge.md) |
