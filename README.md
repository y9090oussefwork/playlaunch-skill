# PlayLaunch — Pure Portable AI Agent Skill for Google Play Growth, Compliance & Isolated Publishing

A standalone, team-ready, CWD-independent **Open Standard AI Agent Skill** (`SKILL.md` + Progressive Disclosure `references/`) for **Antigravity, Gemini CLI, Codex, Claude Code, and Cursor** on Windows.

Install once, then invoke directly inside **any Android (Kotlin/Java) or Flutter project folder** — zero desktop app, zero web UI, zero vendor code modifications.

---

## Capabilities

1. **Zero-Config Project Intake (`stages/01_intake_adapter.py`):**
   - **Standalone Mode (`--project-dir "."`):** Auto-detects framework (`kotlin` / `java` / `flutter`), `applicationId`, display name, and launcher icon directly from the currently open project folder with zero manual profile files.
   - **Audio Workspace Bridge (`--mode audio_workspace`):** Strictly **read-only** integration with `Youssef_Audio_Workspace` (zero writes to `01_Platform_Source/`).
2. **Static Audit, Vitals Risks & Build (`stages/02_audit_and_build.py`):**
   - Enforces **Android Studio JBR** (`JAVA_HOME`) via `core/env_resolver.py` for `detekt`, `gradlew.bat`, and `jarsigner` (never conflicts with Java 23).
   - Scans for Android Vitals Crash/ANR bad-behavior patterns (`Thread.sleep`, `allowMainThreadQueries`, `minifyEnabled=false`, oversized assets).
3. **Google Play Compliance & Rejection Prevention Engine (`stages/02b_compliance_audit.py`):**
   - Binary `.aab`/`.apk` inspection (`aapt2` + pure-Python ProtoXML + pure-Python ELF64 16 KB native `.so` alignment check).
   - Flags `targetSdkVersion`, `debuggable`, `usesCleartextTraffic`, missing `android:exported`, and restricted permissions (`QUERY_ALL_PACKAGES`, `MANAGE_EXTERNAL_STORAGE`, `FOREGROUND_SERVICE_MEDIA_PLAYBACK`, `AD_ID`, etc.).
   - Fingerprints embedded SDKs (`AdMob`, `UMP GDPR`, `Firebase Analytics/Crashlytics/FCM`, `Meta`, `OneSignal`) to auto-generate **`play_console_declarations_checklist.html` & `.txt`** (Data Safety, Ads Declaration, `app-ads.txt`, Families Policy).
   - Audits store copy & headlines (Arabic-normalized + English) for impersonation (`التطبيق الرسمي` / `Official App`) and prohibited downloaders (`تحميل من يوتيوب` / `YouTube Downloader`).
4. **Live ASO Intelligence & Arabic-Smart Policy Guard (`04_aso_research.py` + `05_copy_and_policy_guard.py`):**
   - Runs unmodified `vendors/google-play-scraper` (ESM) injected into unmodified `vendors/aso` (CJS) on Node v24+, plus Python `google-play-scraper` 1–2 star review mining with rate-limiting & graceful degradation.
   - Arabic letter normalization (`أ/إ/آ → ا`, `ى → ي`, `ة → ه`, tashkeel stripping) for keyword stuffing & competitor trademark detection, plus cross-account `security/anti_repetition.py`.
5. **Native Arabic RTL & English LTR Visuals (`06_render_visuals.py`):**
   - Renders Play-compliant `512×512` icons, `1024×500` feature graphics, and `1080×1920` framed screenshots via local headless Chrome/Edge (HarfBuzz Arabic RTL shaping) + Fastlane `metadata/android/<locale>/` layout.
6. **Fail-Closed Account Isolation & Publishing (`security/isolation_guard.py` + `07_isolated_publish.py`):**
   - Defaults to zero-network `manual_bundle` (`READY_TO_UPLOAD/` with `listing_copy.html`, `play_console_declarations_checklist.html`, assets, and checksums).
   - Opt-in `isolated_auto_publish` enforces IPv4-only checks, persistent `/24` IP ledger (`ip_ledger.sqlite3`), `router_reboot` 5s polling, `dedicated_network` (strict Proxy on `requests` + `httplib2` or Windows Wi-Fi SSID lock via `netsh wlan`), and 1-to-1 Service Account key SHA-256 lock.

---

## One-Time Setup on Windows (Team Members)

### 1. Clone & Create Isolated Python 3.12 `.venv`
```powershell
git clone https://github.com/y9090oussefwork/playlaunch-skill.git PlayLaunch_Studio
cd PlayLaunch_Studio

& "C:\Program Files\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Install Unmodified Open-Source Vendors
```powershell
powershell -ExecutionPolicy Bypass -File .\vendors\setup_vendors.ps1
```

### 3. Install as a Global Portable AI Agent Skill
```powershell
powershell -ExecutionPolicy Bypass -File .\install-skill.ps1
```
This links the `playlaunch` skill cleanly into:
- `~\.agents\skills\playlaunch`
- `~\.gemini\antigravity\skills\playlaunch`
- `~\.codex\skills\playlaunch`
- `~\.claude\skills\playlaunch`

Add any custom agent path with `-Target`:
```powershell
powershell -ExecutionPolicy Bypass -File .\install-skill.ps1 -Target "~\.cursor\skills\playlaunch"
```

---

## Using PlayLaunch from Any Android / Flutter Project

Open **any** Android or Flutter project in Antigravity, Gemini CLI, Codex, Claude Code, or Cursor and simply prompt:

- *"Run PlayLaunch full optimization and Google Play compliance audit for this app in Arabic and English."*
- *"Audit `app-release.aab` for Google Play rejection risks and generate the Data Safety checklist."*
- *"Run PlayLaunch ASO competitor research and write Arabic + English Play Store copy for this project."*

The agent automatically follows [SKILL.md](SKILL.md), reads only the relevant guide in `references/`, executes Stages `1 → 6 + 2B`, and pauses cleanly at `awaiting_human_approval` so you can review `play_console_declarations_checklist.html`, `listing_copy.html`, and the rendered PNG screenshots directly in chat before approving Stage 7.

---

## Automated Tests

All tests run cleanly in isolated temporary directories (`tmp_path`) without touching `security/ip_ledger.sqlite3`, `Youssef_Audio_Workspace`, or Google APIs:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
node tests\smoke_node_vendors.cjs
```
