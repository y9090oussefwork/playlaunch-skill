# Audio Workspace Read-Only Bridge (`references/audio-workspace-bridge.md`)

Read this reference ONLY when working with apps inside `D:\Projects\Youssef_Audio_Workspace` (Mode A: `audio_workspace`).

## Strict Zero-Touch Policy
- `D:\Projects\Youssef_Audio_Workspace` (especially `01_Platform_Source/`) is **100% READ-ONLY** throughout Stages `1 → 6 + 2B`.
- Every run artifact is written exclusively inside `<SKILL>\workspace_runs\<app-slug>\<run-timestamp>\`.
- On teammate machines where `Youssef_Audio_Workspace` does not exist, set `"audio_workspace": { "enabled": false }` in `config/studio.config.json` and use Mode B (`standalone` with `--project-dir "."`).

## Read-Only Data Sources (`stages/01_intake_adapter.py`)
1. `workspace.json`
2. `03_App_Configurations\<app-slug>\app.json`, `branding\branding.json`, `store\listing.json`
3. `01_Platform_Source\apps\android\app\src\<flavor>\assets\offline\music_data.json` (artists, categories, top tracks for authentic ASO)
4. `01_Platform_Source\apps\android\app\src\<flavor>\res\mipmap-xxxhdpi\ic_launcher.png`
5. Release `.aab` discovery (strict read-only priority):
   - Local API `http://127.0.0.1:5000/api/agent/jobs` (if running)
   - SQLite read-only URI (`?mode=ro`) on `%LOCALAPPDATA%\YoussefAudioPlatform\app-manager\agent_jobs.sqlite3`
   - Filesystem scan under `05_Releases\agent-builds\*\<flavor>\release\` and `05_Releases\android\production\<app-slug>\`
6. When a verified signed `.aab` exists, Stage 2 build auto-skips, while **Stage 2B (`02b_compliance_audit.py`) STILL audits the final `.aab` binary** before submission.

## Explicit Opt-In Sync Only
Copying approved assets/release notes into `02_Developer_Accounts\<account_id>\store-assets\<app-slug>\` and `release-notes\<app-slug>\` ONLY happens when the user explicitly requests `--sync-to-audio-workspace` alongside `--approve` in Stage 7.
