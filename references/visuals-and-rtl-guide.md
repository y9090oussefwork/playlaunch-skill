# Store Visuals & Arabic RTL Rendering Guide (`references/visuals-and-rtl-guide.md`)

Read this reference when running **Stage 3 (`03_capture_screens.py`)** or **Stage 6 (`06_render_visuals.py`)**.

## Required Google Play Dimensions (Auto-Verified via Pillow)
- **High-Res App Icon:** `512 × 512` PNG (≤ 1024 KB) → `<RUN_DIR>\visuals\icon_512.png`.
- **Feature Graphic:** `1024 × 500` PNG per locale → `<RUN_DIR>\visuals\feature_graphic\<locale>\feature_graphic.png`.
- **Phone Screenshots:** `1080 × 1920` PNG (2–8 slides per locale) → `<RUN_DIR>\visuals\screenshots\<locale>\01.png` ...
- Automatically mirrored into Fastlane-standard `<RUN_DIR>\metadata\android\<locale>\images\{icon.png, featureGraphic.png, phoneScreenshots\*.png}`.

## Arabic RTL Renderer & Anti-Repetition
- `vendors/open-screenshot-generator` is kept unmodified; because it lacks explicit CSS `dir="rtl"` handling in `src/`, Stage 6 routes Arabic/RTL (and unbuilt OSG CLI setups) cleanly to `stages/renderers/rtl_html_renderer.py`.
- `rtl_html_renderer.py` drives local Windows Chrome/Edge (`--headless=new`) with native Chromium HarfBuzz Arabic shaping, `<html dir="rtl" lang="ar">`, and Windows local `Dubai` / `Segoe UI` typography (works 100% offline with zero broken/disconnected Arabic glyphs).
- `security/anti_repetition.py` assigns a distinct visual template (`editorial_spotlight`, `aurora_mesh_card`, `minimal_clean_split`, `neon_studio_wave`, `geometric_showcase`) and font pair per developer `account_id` to prevent cross-account visual footprint repetition.

## Raw Screenshots Fallback (Stage 3)
- If an Android emulator/device is attached via ADB, Stage 3 captures raw screens into `<RUN_DIR>\raw_screenshots\`.
- If no ADB device is attached, Stage 3 uses any pre-captured PNG/JPG dropped in `<RUN_DIR>\raw_screenshots\` (or skips cleanly so Stage 6 renders realistic branded phone UI mockups automatically).
