# PlayLaunch Studio — Master Architecture & Implementation Blueprint

## 1. Mission & Core Philosophy
You are tasked with planning and building **PlayLaunch Studio**, a standalone, team-ready, Windows-native **Google Play Growth, ASO, Screenshot, Audit, Build, and Strictly-Isolated Publishing System**.

### Mandatory Operating Rules
1. **Plan First, Build Second:** Before creating files or installing dependencies, inspect the local Windows environment (PowerShell, Python, Node.js, Git, Java/JDK, Android SDK/ADB, Flutter if present) and present a clear phased implementation plan for user approval.
2. **Modular Vendor Architecture (ZERO Code Merging):** Every open-source project MUST live unmodified inside its own dedicated directory under `vendors/`. Do NOT merge, fork, or rewrite vendor source code. Orchestrate them sequentially using lightweight Python adapter scripts in `stages/`.
3. **Zero Touch on Existing Audio Workspace:** This project must integrate cleanly in read-only/output-only mode with the existing workspace at `D:\Projects\Youssef_Audio_Workspace` WITHOUT modifying a single line of code inside `01_Platform_Source/`. On remote teammates' machines where `Youssef_Audio_Workspace` does not exist, the system must work seamlessly in `standalone` mode with zero errors.
4. **Universal AI Agent Compatibility:** The entire pipeline must be runnable with a single command by any AI coding agent (Antigravity, Gemini CLI, Codex, Cursor) via standardized instruction/skill files (`AGENTS.md`, `GEMINI.md`, `.cursorrules`, `.antigravity/`).
5. **Hybrid Design:** Build a clean CLI/Agent-first staged engine first, topped with a lightweight local Windows Desktop Review & Control UI (Flask + `pywebview`, running on `127.0.0.1:5055` to avoid conflicting with the existing workspace's port `5000`) with full Arabic (RTL) and English (LTR) UI support.
6. **Strict Account Isolation by Default:** Preventing Google Play Developer Account association is the #1 non-negotiable security requirement. Manual export mode (`manual_bundle`) is the default; automated Google Play API calls (publishing, draft upload, vitals checks, or review reads) are opt-in and MUST ALL pass through `security/isolation_guard.py`.

---

## 2. Open-Source Vendor Projects (Inside `vendors/` — Unmodified)
Clone/install each project cleanly inside `vendors/` as an independent module via `vendors/setup_vendors.ps1`:

| Stage / Purpose | GitHub Repository | Runtime | Role in Pipeline |
| :--- | :--- | :--- | :--- |
| **Competitor & Store Scraping** | `https://github.com/facundoolano/google-play-scraper` | Node.js | Fetch competitor titles, descriptions, rankings, categories, and Play Store search autocomplete suggestions (`suggest`). |
| **ASO Keyword Intelligence** | `https://github.com/facundoolano/aso` | Node.js | Calculate keyword difficulty, traffic scores, and discover high-opportunity keywords from competitors. |
| **Fast Review Mining** | `https://github.com/JoMingyu/google-play-scraper` | Python | Pull 1-star and 2-star reviews of top competitors to extract real user complaints and convert them into app strengths & store copy. |
| **Store Screenshots & Framing** | `https://github.com/dotnetdreamer/open-screenshot-generator` | Project Native | Primary engine to wrap raw app screenshots into device frames with marketing headlines, backgrounds, and localized store visuals. |
| **Kotlin Static Code Audit** | `https://github.com/detekt/detekt` | JVM / CLI | Static code quality and crash/performance smell analysis for standalone Kotlin Android apps (alongside native Android Lint & Flutter Analyze). |
| **Play Store Metadata, Vitals & Publishing** | Official `google-api-python-client` (Google Play Android Publisher API v3 & Play Developer Reporting API) + Fastlane-compatible metadata folder format (`https://github.com/fastlane/fastlane` layout) | Python 3 (Windows-native, no Ruby required) | Upload drafts/releases, localized store listings, graphics, and optionally check Android Vitals (Crash/ANR rates). Keep output folder structure 100% compatible with Fastlane `supply` (`metadata/android/<locale>/`) as an optional secondary adapter. |

*(Note on Arabic RTL Screenshots: Inspect `dotnetdreamer/open-screenshot-generator` first during Phase 1/2. If it has any Arabic/RTL font shaping limitations, add an optional clean fallback renderer using Playwright + HTML/CSS templates inside `stages/` without modifying the vendor repository.)*

---

## 3. Target Repository Folder Structure

```text
PlayLaunch_Studio/
├── AGENTS.md                        # Universal operating manual for all AI agents
├── GEMINI.md                        # Gemini & Antigravity specific workflow rules
├── .cursorrules                     # Cursor/Codex compatible workflow rules
├── .antigravity/                    # Agent skills & one-command stage workflows
├── PROJECT_BLUEPRINT.md             # This master blueprint
├── README.md                        # Setup guide for Windows & remote team members
├── requirements.txt                 # Isolated Python dependencies (installed inside .venv/)
├── .gitignore                       # STRICTLY ignores .venv/, config/studio.config.json, secrets/, real profiles/, security/*.sqlite3, workspace_runs/, vendor clones
├── config/
│   ├── studio.config.example.json   # Global paths (optional Youssef_Audio_Workspace path, Android SDK, etc.)
│   └── play_policies.json           # Hard Play Store limits (title 30, short 80, full 4000, banned terms, Vitals thresholds)
├── vendors/                         # Unmodified open-source projects (gitignored clones + tracked setup script)
│   ├── setup_vendors.ps1            # One-click PowerShell setup script for Windows team members
│   ├── google-play-scraper/
│   ├── aso/
│   ├── open-screenshot-generator/
│   └── detekt/
├── core/                            # Shared utilities (run_state manager, logger, validators, schema models)
├── stages/                          # Independent sequential Python scripts (each reads/writes run_state.json)
│   ├── run_pipeline.py              # Master orchestrator (runs all stages or resumes from a specific stage)
│   ├── 01_intake_adapter.py         # Dual-mode intake: Audio Workspace OR Standalone (Kotlin/Java/Flutter)
│   ├── 02_audit_and_build.py        # Static analysis + build + signing verification (auto-skipped if pre-built)
│   ├── 03_capture_screens.py        # Emulator/ADB raw screenshot capture per locale
│   ├── 04_aso_research.py           # Runs vendor scraper/ASO/review mining tools -> outputs aso_research.json
│   ├── 05_copy_and_policy_guard.py  # Prepares AI copy brief + validates localized listings against Play policies
│   ├── 06_render_visuals.py         # Runs open-screenshot-generator + 512x512 icon + 1024x500 feature graphic
│   └── 07_isolated_publish.py       # Export manual bundle OR execute Strictly-Isolated Auto-Publish / Vitals check
├── security/
│   ├── isolation_guard.py           # IP Ledger + Router Reboot Monitor + Proxy/Network verifier + 1-to-1 Key Lock
│   ├── anti_repetition.py           # Tracks visual templates, fonts & copy styles per developer account
│   └── ip_ledger.sqlite3            # Local persistent DB of every IP & key hash ever used per account (gitignored)
├── profiles/
│   ├── accounts/                    # One JSON profile per developer account (gitignored real files + *.example.json)
│   └── apps/                        # One JSON profile per standalone app (gitignored real files + *.example.json)
├── secrets/                         # Isolated per-account Play API keys (gitignored, 1-to-1 locked)
├── ui/                              # Lightweight Local Desktop Control Center (Flask 127.0.0.1:5055 + pywebview, AR/EN)
│   ├── server.py
│   ├── desktop_window.py
│   └── templates/                   # Visual review for screenshots/copy, publish toggle, live IP & router reboot screen
├── tests/                           # Automated unit tests (mandatory coverage for isolation_guard, policy_guard, intake)
├── Launch_Studio.bat                # Double-click Windows launcher for the Desktop UI
└── workspace_runs/                  # Per-app execution runs, artifacts, and manual export packages (gitignored)
```

---

## 4. Dual Integration Modes (Stage 1: `01_intake_adapter.py`)

The system MUST support two seamless modes via `--mode`:

### Mode A: `audio_workspace` (Integration with `D:\Projects\Youssef_Audio_Workspace`)
Automatically detects and reads (read-only) an app by `--app-slug` or `--flavor` from the existing workspace:
1. Reads `D:\Projects\Youssef_Audio_Workspace\workspace.json` (path configurable in `config/studio.config.json`).
2. Reads identity & branding from:
   - `03_App_Configurations\<app-slug>\app.json`
   - `03_App_Configurations\<app-slug>\branding\branding.json` (accent color, icon source)
   - `03_App_Configurations\<app-slug>\store\listing.json` (display names, artist name)
3. Reads real audio catalog (songs/hymns & artists) from:
   - `01_Platform_Source\apps\android\app\src\<flavor>\assets\offline\music_data.json`
   *(Passes top tracks/artists into Stage 4 & 5 so ASO keywords and descriptions reflect authentic app content.)*
4. Reads launcher icon from:
   - `01_Platform_Source\apps\android\app\src\<flavor>\res\mipmap-xxxhdpi\ic_launcher.png`
5. Locates the latest verified, signed release `.aab` by checking in order:
   - Local API `http://127.0.0.1:5000/api/agent/jobs` (if running), OR
   - Read-only SQLite query (`?mode=ro`) on `%LOCALAPPDATA%\YoussefAudioPlatform\app-manager\agent_jobs.sqlite3` (`job_type='android_build_aab'`, `local_status='completed'`, `build_variant='release'`), OR
   - Latest `.aab` inside `05_Releases\agent-builds\*\<flavor>\release\`.
6. Auto-skips Stage 2 (Build & Sign) once the `.aab` signature and metadata are verified.
7. Maps approved output store assets cleanly to `02_Developer_Accounts\<account_id>\store-assets\<app-slug>\` and `release-notes\<app-slug>\` (in addition to `workspace_runs/`).
8. Optional Stage 3 hook: Can invoke `POST http://127.0.0.1:5000/api/android/run` if the local desktop server is running to launch the app on an emulator before ADB screenshot capture.

### Mode B: `standalone` (Any Independent Kotlin, Java, or Flutter App)
Works on any developer's Windows machine without `Youssef_Audio_Workspace`:
1. Reads app config from `profiles/apps/<app-slug>.json` (source code path, framework: `kotlin` | `java` | `flutter`, package name, target locales, developer account profile, signing reference).
2. Runs Stage 2:
   - **Kotlin/Java:** Runs `detekt` (if Kotlin) + `gradlew.bat lintRelease` + `gradlew.bat :app:bundleRelease`, checks memory/ANR risk patterns, verifies `.aab` signature via `jarsigner`.
   - **Flutter:** Runs `flutter analyze` + `flutter build appbundle --release`, verifies `.aab` signature.
3. Continues seamlessly through Stages 3 to 7.

---

## 5. Detailed 7-Stage Pipeline Specification

Each run creates `workspace_runs/<app-slug>/<run-timestamp>/run_state.json` tracking status (`pending`, `running`, `completed`, `skipped`, `failed`, `awaiting_human_approval`) and artifact paths for every stage so the pipeline is fully resumable.

1. **Stage 1 — Intake (`01_intake_adapter.py`):** Normalizes app metadata, branding, existing `.aab` (if any), and account profile into `intake_manifest.json`.
2. **Stage 2 — Audit & Build (`02_audit_and_build.py`):** Static code checks (Detekt / Android Lint / Flutter Analyze), Android Vitals risk check (flagging heavy main-thread operations, oversized assets, missing minify/shrink, bad-behavior risk vs. Play thresholds: Crash Rate < 1.09%, ANR Rate < 0.47%), release `.aab` build, and signature verification. Auto-skipped in `audio_workspace` mode when verified `.aab` exists.
3. **Stage 3 — Raw Screen Capture (`03_capture_screens.py`):** Uses ADB to capture clean raw screenshots from an Android emulator or connected device across defined screens/locales (or accepts pre-captured raw screenshots dropped in `raw_screenshots/`).
4. **Stage 4 — ASO & Competitor Intelligence (`04_aso_research.py`):** Executes Node.js `google-play-scraper` + `aso` and Python `google-play-scraper` review miner (public store scraping only — zero Play Console credentials). Outputs `aso_research.json` containing:
   - Top 10 competitor profiles, titles, short/long descriptions.
   - Keyword opportunity matrix (traffic vs. difficulty + autocomplete suggestions).
   - Categorized 1-star & 2-star competitor user complaints.
5. **Stage 5 — Copywriting, Localization & Policy Guard (`05_copy_and_policy_guard.py`):**
   - Operates in two explicit sub-commands so the active AI coding agent (Antigravity / Gemini / Codex) writes the copy directly using its own intelligence (zero extra LLM API keys required):
     - `prepare`: Builds a clean `copy_brief.json` combining `intake_manifest.json`, `aso_research.json`, target locales, and the account's allowed tone/style from `anti_repetition.py`.
     - `validate`: Validates the agent-generated localized files in Fastlane-standard structure (`metadata/android/<locale>/{title.txt, short_description.txt, full_description.txt, changelogs/<versionCode>.txt}` + `screenshot_headlines.json`).
   - Enforces automated **Policy Guard validation** against `config/play_policies.json`:
     - Title <= 30 chars, Short Description <= 80 chars, Full Description <= 4000 chars, Changelog <= 500 chars.
     - Rejects keyword stuffing (excessive word density/repetition), promotional claims banned by Play ("#1", "Best", "Free download" spam, emoji abuse), and accidental mentions of competitor trademark/app names.
     - Checks `anti_repetition.py` to ensure tone and structure differ across developer accounts.
6. **Stage 6 — Visual & Screenshot Generation (`06_render_visuals.py`):**
   - Feeds raw screenshots, brand colors, and localized headlines into `vendors/open-screenshot-generator` (or the clean RTL fallback renderer if needed for Arabic).
   - Enforces unique template/layout/font selection per developer account via `anti_repetition.py`.
   - Produces Google Play compliant assets per locale: Phone Screenshots (1080x1920), High-Res Icon (512x512 PNG), and Feature Graphic (1024x500 PNG).
7. **Stage 7 — Packaging, Account Isolation & Publishing (`07_isolated_publish.py`):**
   - **Mandatory Human Approval Gate:** Pipeline ALWAYS pauses after Stage 6 (`awaiting_human_approval`) until the user reviews texts and screenshots via Desktop UI or explicit CLI confirmation (`--approve`).
   - Supports `--dry-run` to simulate all isolation checks and upload payloads without contacting Google Play.
   - Reads `publish_mode` from the account profile:
     - `manual_bundle` **(DEFAULT):** Zero network calls to Google Play. Packages a clean, self-contained folder `READY_TO_UPLOAD/` containing the signed `.aab`, localized copy-paste text sheets (`listing_copy.txt` & `listing_copy.html`), icons, feature graphics, and framed screenshots so the user can transfer it to the isolated client device.
     - `isolated_auto_publish` **(OPT-IN):** Runs `security/isolation_guard.py` BEFORE touching ANY Google Play API (Publishing API or Developer Reporting API), then uploads as **`draft`** track by default (never live production without explicit `--promote-production` flag).

---

## 6. Strict Account Isolation Architecture (`security/isolation_guard.py`)

This module is a hard security gate. EVERY outbound call to Google Play APIs (publishing, draft upload, vitals reporting, or review management) MUST acquire a verified session from `isolation_guard.py`. If ANY check fails, execution aborts immediately BEFORE any network socket is opened.

### Layer 1: Persistent IP & Key Ledger (`security/ip_ledger.sqlite3`)
Schema tracks:
- `ip_usage`: `ip_address`, `subnet_24`, `account_id`, `publish_mode_used`, `isp_or_org`, `first_used_at`, `last_used_at`, `action_type`, `status`.
- `key_bindings`: `key_file_sha256` (UNIQUE), `service_account_email` (UNIQUE), `account_id`, `bound_at`.

### Layer 2: Dual Network Isolation Modes (Configurable per Account Profile)
1. **Mode 1: `router_reboot` (Dynamic Home/Mobile Router IP Enforcement)**
   - Queries current public egress IP (using redundant HTTPS IP-check endpoints with timeout handling) + `/24` subnet.
   - Checks `ip_ledger.sqlite3`:
     - **HARD BLOCK** if the exact IP (or `/24` subnet when `strict_subnet_block: true`) was EVER used by a **different** `account_id`.
     - **BLOCK** if `require_fresh_ip_every_run: true` and the IP already exists in the ledger for ANY account (including the same account).
   - When blocked, displays a clear instruction (in both CLI and Desktop UI): **"IP [X.X.X.X] is blocked/used. Please restart your router now to obtain a fresh IP."**
   - Polls automatically every 5 seconds until a new IP is detected, verifies the new IP is 100% clean against `ip_ledger.sqlite3`, records it bound to `account_id`, and unlocks the API session.
2. **Mode 2: `dedicated_network` (Per-Account Proxy or Isolated Network Interface/Hotspot)**
   - Supports binding an account profile to a dedicated HTTP/SOCKS5 proxy URL (credentials stored in Windows Credential Manager via `keyring`, never plain text) OR binding to a specific Windows Wi-Fi SSID / network adapter (e.g., dedicated client Android hotspot).
   - Verifies egress IP strictly through that proxy/interface, checks `ip_ledger.sqlite3` to guarantee zero cross-account IP overlap, and forces all Google Play API requests exclusively through that route (aborting if the proxy/adapter drops).

### Layer 3: Strict 1-to-1 Service Account Key Lock
- Every developer account profile stores the expected `service_account_email` and `key_file_sha256`.
- `isolation_guard.py` hashes the key file before every API session and verifies against `key_bindings` that this `key_file_sha256` and `service_account_email` are NEVER registered to any other `account_id`.

### Layer 4: Anti-Repetition & Anti-Footprint Guard (`security/anti_repetition.py`)
- Logs used screenshot templates, font families, color palettes, and copywriting structures per `account_id`.
- Warns or blocks if an agent attempts to reuse the exact same visual template or copy structure across two separate developer accounts.

---

## 7. Lightweight Local Windows Desktop UI (`ui/`)
- Built with **Python Flask (`127.0.0.1:5055`) + `pywebview`** (matching Windows native workflow, launched via `Launch_Studio.bat`), supporting clean **Arabic (RTL)** and **English (LTR)** toggle.
- **4 Core Screens:**
  1. **Dashboard & Runs:** Pick an app (auto-listed from `Youssef_Audio_Workspace` if configured, or `profiles/apps/`), view the 7-stage progress bar, and trigger/resume runs.
  2. **Visual & Copy Review:** Side-by-side preview of generated screenshots per language, feature graphic, icon, and Play Store copy with live character counters and Policy Guard badges.
  3. **Account Profiles & Isolation Settings:** Toggle per account between `manual_bundle` and `isolated_auto_publish`, configure `router_reboot` vs. `dedicated_network`, toggle `strict_subnet_block` and `require_fresh_ip_every_run`, and manage 1-to-1 key locks.
  4. **Live Publish & IP Guard Monitor:** Shows current public IP, full searchable `ip_ledger.sqlite3` history, live "Waiting for Router Reboot..." status indicator with auto-refresh, and one-click "Export Manual Bundle" or "Upload Draft to Play Console".

---

## 8. Universal AI Agent Skills & One-Command Operation
Generate clear, concise agent instruction files in the root (`AGENTS.md`, `GEMINI.md`, `.cursorrules`, and `.antigravity/workflows/full-release.md`) so that when a user types a single prompt like:
> "Run full PlayLaunch optimization for app `<slug>` in Arabic and English, pause before publishing for my review."

The agent knows the exact commands to execute Stages 1 -> 4, run `05_copy_and_policy_guard.py prepare`, write high-converting localized copy directly using Stage 4's real competitor/review data, validate with `05_copy_and_policy_guard.py validate`, render visuals in Stage 6, and stop cleanly at the Human Approval Gate.

---

## 9. Required Execution Phases (Present Plan First!)
Execute the project in 4 clean, testable phases:
- **Phase 1 (Foundation & Vendors):** Initialize Git repo, `.gitignore`, Python `.venv` + `requirements.txt`, folder layout, `vendors/setup_vendors.ps1` (installing all open-source vendors cleanly on Windows), `core/` state manager, and `01_intake_adapter.py` (testing read-only integration with `D:\Projects\Youssef_Audio_Workspace` + standalone sample).
- **Phase 2 (ASO, Copy Guard & Visuals):** Build `04_aso_research.py`, `05_copy_and_policy_guard.py` (`prepare` + `validate`), `security/anti_repetition.py`, and `06_render_visuals.py` wired to `vendors/open-screenshot-generator` (with Arabic RTL verification).
- **Phase 3 (Audit/Build, Isolation Guard & Publisher):** Build `02_audit_and_build.py`, `03_capture_screens.py`, `security/isolation_guard.py` (with automated unit tests in `tests/` verifying cross-account IP blocking, router reboot polling, proxy/hotspot isolation, and 1-to-1 key-lock enforcement), and `07_isolated_publish.py` (including `--dry-run`, `manual_bundle`, and `isolated_auto_publish`).
- **Phase 4 (Desktop Control UI & Agent Skills):** Build the local Flask + `pywebview` UI on port `5055` (AR/EN), `Launch_Studio.bat`, and the universal AI agent instruction files (`AGENTS.md`, `GEMINI.md`, `.cursorrules`, `.antigravity/`).

**START NOW:** Inspect the local Windows environment and tools available, then output your detailed Phase 1–4 implementation plan and ask for approval before writing code.