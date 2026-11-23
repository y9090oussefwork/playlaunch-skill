# ASO Research & Copywriting Playbook (`references/aso-and-copy-playbook.md`)

Read this reference when running **Stage 4 (`04_aso_research.py`)** or **Stage 5 (`05_copy_and_policy_guard.py`)**.

## Stage 4: ASO & Competitor Intelligence
- **Runtime Bridge:** `stages/bridges/aso_node_bridge.cjs` injects unmodified `vendors/google-play-scraper` (ESM) into unmodified `vendors/aso` (CJS) on Node v24+, avoiding `aso`'s outdated bundled scraper without editing any vendor code.
- **Resilience:** Built-in rate-limit delay (`--delay-ms 350`), per-call timeouts (`--call-timeout-ms 15000`), and graceful degradation (if one competitor or keyword fails/times out, the run continues cleanly and logs warnings).
- **Review Mining:** Python `google-play-scraper` mines 1-star & 2-star competitor reviews and categorizes complaints (`ads_intrusive`, `offline_playback_issues`, `crashes_bugs`, `audio_quality`, `missing_songs_or_features`, `ui_ux_friction`, `battery_performance`).

## Stage 5: Writing High-Converting, Policy-Clean Copy
1. Run `05_copy_and_policy_guard.py prepare --run-dir "<RUN_DIR>"` to produce `copy_brief.json` and scaffold Fastlane directories (`metadata/android/<locale>/`).
2. Read `copy_brief.json` and follow `assigned_account_style` (`tone` and `structure_signature` from `security/anti_repetition.py`) so separate developer accounts never share copy footprints.
3. Turn top 1–2 star competitor complaints from `aso_insights.competitor_pain_points` into authentic product strengths.
4. Respect hard Google Play limits (`config/play_policies.json`):
   - `title.txt` ≤ **30 chars** (0 emojis; no `FREE`/`TOP`/`BEST`/`!!`).
   - `short_description.txt` ≤ **80 chars** (≤ 1 emoji).
   - `full_description.txt` ≤ **4000 chars** (≤ 8 emojis; content word density ≤ `4.0%`, single word repeats ≤ `10`).
   - `changelogs/1.txt` ≤ **500 chars**.
   - `screenshot_headlines.json`: ≥ 2 slides per locale, each headline ≤ 60 chars.
5. **Arabic Normalization Intelligence:** `05_copy_and_policy_guard.py` normalizes Arabic (`أ/إ/آ/ٱ → ا`, `ى → ي`, `ة → ه`, strips tashkeel & tatweel) before checking keyword stuffing, banned promotional claims (`الأفضل في العالم`, `حمل الآن`, `رقم 1`), and accidental competitor app/brand mentions.
6. Run `05_copy_and_policy_guard.py validate --run-dir "<RUN_DIR>"` and fix every reported error until `passed: true`.
