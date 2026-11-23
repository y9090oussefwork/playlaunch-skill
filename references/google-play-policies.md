# Google Play Compliance & Rejection Prevention (`references/google-play-policies.md`)

Read this reference when running **Stage 2B (`stages/02b_compliance_audit.py`)** or resolving `BLOCKER`/`WARNING` findings.

## What `02b_compliance_audit.py` Checks (`config/google_play_compliance_rules.json`)

1. **Technical & Manifest Integrity (`.aab` / `.apk` binary inspection):**
   - Decodes `AndroidManifest.xml` via plain XML, `aapt2 dump xmltree` (APK), or pure-Python ProtoXML decoder (AAB).
   - `targetSdkVersion`: `< 34` = **BLOCKER**; `< 35` = **WARNING** (Android 15 recommended).
   - `android:debuggable="true"` = **BLOCKER**.
   - `android:usesCleartextTraffic="true"` = **BLOCKER** (enforce HTTPS in release builds).
   - Missing `android:exported` on any component with `<intent-filter>` = **BLOCKER**.
   - **16 KB Native Page Alignment:** Inspects ELF64 `PT_LOAD` `p_align >= 16384` on all 64-bit (`arm64-v8a`, `x86_64`) `.so` libraries; `< 16 KB` triggers a **WARNING**.

2. **Restricted & Sensitive Permissions:**
   - **BLOCKER:** `QUERY_ALL_PACKAGES`, `REQUEST_INSTALL_PACKAGES`, `MANAGE_EXTERNAL_STORAGE`.
   - **WARNING:** `READ_MEDIA_IMAGES`, `READ_MEDIA_VIDEO`, `READ_MEDIA_AUDIO`, unscoped `READ/WRITE_EXTERNAL_STORAGE` (downgraded to `INFO` when properly scoped via `maxSdkVersion`), `FOREGROUND_SERVICE_MEDIA_PLAYBACK` (requires Play Console FGS declaration + 15–30s demo video), `FOREGROUND_SERVICE_DATA_SYNC`, `FOREGROUND_SERVICE_SPECIAL_USE`, `SCHEDULE_EXACT_ALARM`.
   - **INFO:** `com.google.android.gms.permission.AD_ID` (**BLOCKER** if `--targets-children` is set!).

3. **SDK Fingerprinting → Data Safety, Ads & Families Policy:**
   - Automatically detects `Google AdMob`, `Google UMP (GDPR)`, `Firebase Analytics`, `Firebase Crashlytics`, `Firebase Cloud Messaging`, `Meta/Facebook SDK`, and `OneSignal` from archive metadata & `.dex` markers.
   - Generates exact Data Safety rows in `play_console_declarations_checklist.html` & `.txt`.
   - Warns if AdMob is bundled without Google UMP (GDPR/EEA consent), reminds about `Contains Ads: YES` and `app-ads.txt`.
   - **BLOCKER** under Google Play Families Policy if `--targets-children` is active alongside AdMob/Meta Ads or `AD_ID`.

4. **Privacy Policy URL & IP/Impersonation Guard:**
   - Optional `--privacy-policy-url "https://..."` verifies HTTPS + HTTP 200 (not 404).
   - Scans app name, localized copy, and screenshot headlines (Arabic-normalized + English) for official impersonation (`التطبيق الرسمي` / `Official App` → **BLOCKER**) and prohibited third-party downloaders (`تحميل من يوتيوب` / `YouTube Downloader` / `TikTok Downloader` → **BLOCKER**).

## Stage 7 Blocker Gate
If `compliance_audit_report.json` has `blocker_count > 0`, Stage 7 refuses execution (`exit code 3`). Fix every `BLOCKER` and re-run Stage 2B. **AI Agents must NEVER pass `--acknowledge-compliance-risks` without explicit user permission.**
