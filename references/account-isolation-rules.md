# Account Isolation & Fail-Closed Security Rules (`references/account-isolation-rules.md`)

Read this reference before touching developer account profiles or Stage 7 (`07_isolated_publish.py`).

## Default Mode: Zero-Network `manual_bundle`
- `publish_mode: "manual_bundle"` is the mandatory default.
- Zero network requests are ever made to Google Play.
- Outputs a self-contained `<RUN_DIR>\READY_TO_UPLOAD\` folder containing `listing_copy.html`, `listing_copy.txt`, `play_console_declarations_checklist.html`, `play_console_declarations_checklist.txt`, `bundle_manifest.json` (SHA-256), `assets/`, `metadata/android/` (Fastlane-compatible), and `app_bundle/*.aab` (if present).
- Transfer `READY_TO_UPLOAD\` to the isolated client device/network for manual upload.

## Opt-In Mode: `isolated_auto_publish` (Strictly Gated by `security/isolation_guard.py`)
EVERY outbound Google Play API call (Publishing API v3 or Developer Reporting API) MUST acquire an `IsolatedPlaySession` BEFORE importing `googleapiclient`:

1. **Fail-Closed IPv4-Only Discovery:**
   - Queries explicit IPv4 endpoints (`api.ipify.org`, `ipv4.icanhazip.com`, `v4.ident.me`).
   - Rejects IPv6, private IPs, or lookup errors immediately (`NetworkIsolationError`).
2. **Persistent Ledger (`security/ip_ledger.sqlite3`, gitignored):**
   - **HARD BLOCK** if the exact IPv4 OR `/24` subnet (when `strict_subnet_block: true`) was EVER used by a **different** `account_id`.
   - **BLOCK** if `require_fresh_ip_every_run: true` and the IP exists for ANY account.
3. **Dual Isolation Modes:**
   - **`router_reboot`:** Polls every 5s while writing live status to `security/guard_live_status.json` until a fresh clean IPv4 is detected.
   - **`dedicated_network`:**
     - *Proxy:* Enforces HTTP/SOCKS5 proxy (`trust_env=False`, password via Windows Credential Manager `keyring`) on BOTH IPv4 verification and `httplib2.Http` Play API transport with **ZERO direct fallback**.
     - *Wi-Fi/Hotspot:* Runs `netsh wlan show interfaces` on Windows and aborts immediately if connected `SSID != required_wifi_ssid`.
4. **1-to-1 Service Account Key Lock (`key_bindings`):**
   - Hashes key file (`SHA-256`) and reads `client_email`; permanently blocks cross-account reuse of either key hash or service account email.
5. **Draft-Only Default:**
   - Uploads strictly to **`draft`** track by default. Never pass `--promote-production` without explicit user instruction.
