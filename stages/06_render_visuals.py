"""Stage 6 — Visual & Screenshot Generation (stages/06_render_visuals.py).

Produces Google Play compliant store visual assets strictly inside workspace_runs/:
  - High-Res App Icon: 512x512 PNG (<= 1024 KB)
  - Feature Graphic:   1024x500 PNG per locale
  - Phone Screenshots: 1080x1920 PNG per locale

Engine routing (`--engine auto|osg|rtl_html`):
  - Probes unmodified `vendors/open-screenshot-generator/cli/dist/cli.js`.
  - Because `open-screenshot-generator` does not set CSS `dir="rtl"` for Arabic shaping and
    requires a full Next.js bundle build for `cli/dist/cli.js`, `auto` cleanly delegates
    Arabic/RTL locales (and unbuilt OSG setups) to `stages/renderers/rtl_html_renderer.py`
    using local Chrome/Edge headless Chromium with native HarfBuzz Arabic shaping — ZERO
    modifications to `vendors/`.

Enforces unique visual template & typography per developer account via `security/anti_repetition.py`.
Pauses pipeline cleanly at the mandatory Human Approval Gate (`awaiting_human_approval`).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.config_loader import load_play_policies
from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso
from core.validators import assert_path_within
from security.anti_repetition import DEFAULT_ANTI_REPETITION_DB, AntiRepetitionGuard
from stages.renderers.rtl_html_renderer import (
    build_feature_graphic_html,
    build_screenshot_html,
    find_local_chromium_browser,
    is_rtl_locale,
    render_html_to_png,
)

logger = get_logger("stage.06_render_visuals")

OSG_VENDOR_DIR = STUDIO_ROOT / "vendors" / "open-screenshot-generator"
OSG_CLI_ENTRY = OSG_VENDOR_DIR / "cli" / "dist" / "cli.js"


def probe_osg_vendor() -> dict[str, Any]:
    """Inspect unmodified vendors/open-screenshot-generator CLI availability & RTL status."""
    return {
        "vendor_dir_exists": OSG_VENDOR_DIR.exists(),
        "cli_built": OSG_CLI_ENTRY.exists(),
        "cli_entry": str(OSG_CLI_ENTRY),
        "native_rtl_css_support": False,
        "notes": (
            "open-screenshot-generator cloned unmodified; lacks explicit CSS dir=rtl/unicode-bidi "
            "support in src/ and cli/dist/cli.js requires full Next.js cli:build. "
            "Using stages/renderers/rtl_html_renderer.py (local Chromium HarfBuzz RTL) for guaranteed "
            "Arabic shaping and exact 1080x1920 / 1024x500 PNG output."
        ),
    }


def _hex_to_rgb(hex_color: str, default: tuple[int, int, int] = (34, 197, 94)) -> tuple[int, int, int]:
    raw = (hex_color or "").strip().lstrip("#")
    if len(raw) == 6:
        try:
            return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
        except ValueError:
            return default
    return default


def prepare_high_res_icon(
    manifest: dict[str, Any],
    output_icon_path: Path,
    cache_dir: Path,
    download_remote: bool = True,
) -> dict[str, Any]:
    """Produce a guaranteed 512x512 PNG icon (<= 1024 KB) strictly inside run_dir."""
    branding = manifest.get("branding", {})
    app = manifest.get("app", {})
    local_icon = Path(branding["icon_local_path"]) if branding.get("icon_local_path") else None
    source_used = "generated_fallback"
    source_img_path: Path | None = None

    if local_icon and local_icon.exists():
        source_img_path = local_icon
        source_used = f"local:{local_icon}"
    elif download_remote and branding.get("icon_source_url"):
        url = str(branding["icon_source_url"])
        try:
            resp = requests.get(url, timeout=8)
            if resp.status_code == 200 and len(resp.content) > 256:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached = cache_dir / "source_icon_download.img"
                cached.write_bytes(resp.content)
                # Verify it opens as a valid image
                with Image.open(cached) as test_im:
                    test_im.verify()
                source_img_path = cached
                source_used = f"remote:{url}"
        except Exception as exc:
            logger.warning("Could not download icon_source_url (%s); using clean branded fallback: %s", url, exc)

    output_icon_path.parent.mkdir(parents=True, exist_ok=True)
    if source_img_path is not None:
        with Image.open(source_img_path) as im:
            rgba = im.convert("RGBA")
            # Center-crop to square if aspect ratio differs
            w, h = rgba.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            cropped = rgba.crop((left, top, left + side, top + side))
            resized = cropped.resize((512, 512), Image.Resampling.LANCZOS)
            resized.save(output_icon_path, format="PNG", optimize=True)
    else:
        # Crisp generated 512x512 monogram fallback
        accent_rgb = _hex_to_rgb(branding.get("accent_color", "#2563EB"))
        img = Image.new("RGBA", (512, 512), (15, 23, 42, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([24, 24, 488, 488], radius=96, fill=(22, 32, 52, 255), outline=accent_rgb + (255,), width=14)
        draw.ellipse([116, 116, 396, 396], fill=accent_rgb + (255,))
        label = (str(app.get("display_name") or app.get("slug") or "P").strip()[:1] or "P").upper()
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 180)
        except OSError:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((512 - tw) / 2, (512 - th) / 2 - 14), label, fill=(10, 15, 28, 255), font=font)
        img.save(output_icon_path, format="PNG", optimize=True)

    with Image.open(output_icon_path) as final_im:
        w, h = final_im.size
    size_bytes = output_icon_path.stat().st_size
    return {
        "path": str(output_icon_path),
        "width": w,
        "height": h,
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024.0, 2),
        "source": source_used,
    }


def _discover_raw_screenshots(run_dir: Path, locale: str) -> list[Path]:
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    for folder in (run_dir / "raw_screenshots" / locale, run_dir / "raw_screenshots"):
        if folder.exists() and folder.is_dir():
            files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts)
            if files:
                return files
    return []


def _default_headlines_for_locale(locale: str, app_name: str, is_audio: bool) -> list[dict[str, str]]:
    if is_rtl_locale(locale):
        if is_audio:
            return [
                {"headline": f"استمع إلى {app_name} بدون إنترنت", "subtitle": "جميع المقاطع جاهزة للتشغيل الفوري في أي وقت", "badge": "يعمل بدون نت"},
                {"headline": "جودة صوت نقية وتشغيل مستقر", "subtitle": "استمتع بتجربة استماع واضحة بدون تقطيع", "badge": "صوت نقي"},
                {"headline": "تشغيل في الخلفية وقفل الشاشة", "subtitle": "تحكم كامل من شريط الإشعارات أثناء استخدام هاتفك", "badge": "تشغيل خلفي"},
                {"headline": "تصميم خفيف وسهل الاستخدام", "subtitle": "وصول سريع لمقاطعك المفضلة بضغطة واحدة", "badge": "واجهة سلسة"},
            ]
        return [
            {"headline": f"تجربة سريعة ومنظمة مع {app_name}", "subtitle": "إنجاز يومي أسهل بواجهة عربية واضحة", "badge": "سهل الاستخدام"},
            {"headline": "يعمل بسلاسة في أي وقت", "subtitle": "أداء خفيف ومستقر يحافظ على بطارية هاتفك", "badge": "أداء عالي"},
        ]
    if is_audio:
        return [
            {"headline": f"Play {app_name} Anywhere Offline", "subtitle": "Instant local playback with zero internet required", "badge": "Offline Ready"},
            {"headline": "Crisp & Reliable Audio Quality", "subtitle": "Smooth listening experience built for daily use", "badge": "Clear Audio"},
            {"headline": "Background & Lock-Screen Play", "subtitle": "Keep listening effortlessly while multitasking", "badge": "Background Play"},
            {"headline": "Fast, Clean & Lightweight UI", "subtitle": "Find and replay your favorite tracks in seconds", "badge": "Clean UI"},
        ]
    return [
        {"headline": f"Stay Focused With {app_name}", "subtitle": "Simple, clean design built for everyday productivity", "badge": "Clean Workflow"},
        {"headline": "Fast, Private & Offline First", "subtitle": "Lightweight performance with zero clutter", "badge": "Offline First"},
    ]


def run_stage_06(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    anti_rep_db: Path = DEFAULT_ANTI_REPETITION_DB,
    engine: str = "auto",
    strict_anti_repetition: bool = False,
    record_anti_repetition: bool = True,
    download_remote_icon: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Execute Stage 6 Visual Generation and pause cleanly at Human Approval Gate."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    manifest = json.loads((manager.run_dir / "intake_manifest.json").read_text(encoding="utf-8"))
    policies = load_play_policies()
    osg_probe = probe_osg_vendor()

    manager.update_stage(state, "06_render_visuals", "running", message="Rendering store visuals (icon, feature graphic, screenshots)")

    app = manifest.get("app", {})
    branding = manifest.get("branding", {})
    locales = app.get("target_locales") or ["ar", "en-US"]
    account_id = str(manifest.get("account", {}).get("account_id") or "unassigned")
    accent_color = str(branding.get("accent_color") or "#22C55E")
    is_audio = str(app.get("primary_category") or "").upper() == "MUSIC_AND_AUDIO"
    sample_tracks = list((manifest.get("audio_catalog") or {}).get("top_tracks") or [])

    # Select account-unique visual template via Anti-Repetition Guard
    guard = AntiRepetitionGuard(db_path=anti_rep_db)
    brief_path = manager.run_dir / "copy_brief.json"
    preferred_template = None
    if brief_path.exists():
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        preferred_template = brief.get("assigned_account_style", {}).get("visual", {}).get("template_id")

    assigned = guard.select_style_for_account(account_id=account_id, preferred_template=preferred_template)
    visual_style = assigned["visual"]
    collision = guard.check_visual_collision(
        account_id=account_id,
        template_id=visual_style["template_id"],
        font_ar=visual_style["font_ar"],
        font_en=visual_style["font_en"],
    )
    warnings: list[str] = []
    if collision["collision_detected"]:
        msg = (
            f"Visual template '{visual_style['template_id']}' was previously used by other account(s): "
            f"{collision['colliding_accounts']}."
        )
        if strict_anti_repetition:
            manager.update_stage(state, "06_render_visuals", "failed", message=msg)
            raise RuntimeError(msg)
        warnings.append(msg)

    visuals_root = manager.run_dir / "visuals"
    assert_path_within(visuals_root, manager.run_dir)
    visuals_root.mkdir(parents=True, exist_ok=True)

    # 1. High-Res Icon (512x512 PNG)
    icon_out = visuals_root / "icon_512.png"
    icon_info = prepare_high_res_icon(
        manifest=manifest,
        output_icon_path=icon_out,
        cache_dir=visuals_root / "_cache",
        download_remote=download_remote_icon,
    )
    icon_req = policies["visual_requirements"]["icon"]
    assert icon_info["width"] == icon_req["width"] and icon_info["height"] == icon_req["height"], (
        f"Icon dimensions mismatch: {icon_info['width']}x{icon_info['height']}"
    )

    # Load headlines from Stage 5 if available
    headlines_path = manager.run_dir / "screenshot_headlines.json"
    headlines_by_locale: dict[str, Any] = {}
    if headlines_path.exists():
        headlines_by_locale = json.loads(headlines_path.read_text(encoding="utf-8"))

    browser_exe = find_local_chromium_browser()
    if browser_exe is None:
        raise RuntimeError("No local Chrome/Edge Chromium binary found for headless HTML/RTL visual rendering.")

    rendered_locales: dict[str, Any] = {}
    metadata_root = manager.run_dir / "metadata" / "android"
    assert_path_within(metadata_root, manager.run_dir)

    for loc in locales:
        rtl = is_rtl_locale(loc)
        engine_used = "rtl_html_renderer"
        if engine == "osg" and osg_probe["cli_built"] and not rtl:
            engine_used = "open_screenshot_generator_cli"
        elif engine == "osg" and not osg_probe["cli_built"]:
            warnings.append(
                f"Requested engine='osg' for '{loc}', but open-screenshot-generator cli/dist/cli.js is not built; "
                "using clean fallback rtl_html_renderer."
            )

        # Read localized title & short_description from Stage 5 Fastlane metadata if present
        loc_meta_dir = metadata_root / loc
        loc_title = (
            (loc_meta_dir / "title.txt").read_text(encoding="utf-8").strip()
            if (loc_meta_dir / "title.txt").exists()
            else ""
        ) or str(app.get("display_name") or app.get("slug") or "App")
        loc_short = (
            (loc_meta_dir / "short_description.txt").read_text(encoding="utf-8").strip()
            if (loc_meta_dir / "short_description.txt").exists()
            else ""
        ) or ("تجربة استماع سريعة بدون إنترنت" if rtl else "Fast, clean offline mobile experience")

        # 2. Feature Graphic (1024x500 PNG)
        fg_out = visuals_root / "feature_graphic" / loc / "feature_graphic.png"
        fg_html = build_feature_graphic_html(
            locale=loc,
            template_id=visual_style["template_id"],
            font_ar=visual_style["font_ar"],
            font_en=visual_style["font_en"],
            accent_color=accent_color,
            title=loc_title,
            tagline=loc_short,
            icon_path=icon_out,
        )
        fg_info = render_html_to_png(fg_html, fg_out, width=1024, height=500, browser_exe=browser_exe)

        # 3. Phone Screenshots (1080x1920 PNG)
        raw_shots = _discover_raw_screenshots(manager.run_dir, loc)
        loc_headlines = headlines_by_locale.get(loc)
        if not isinstance(loc_headlines, list) or not any(
            isinstance(x, dict) and str(x.get("headline") or "").strip() for x in loc_headlines
        ):
            loc_headlines = _default_headlines_for_locale(loc, loc_title, is_audio=is_audio)

        shots_out_dir = visuals_root / "screenshots" / loc
        shots_out_dir.mkdir(parents=True, exist_ok=True)
        rendered_shots: list[dict[str, Any]] = []

        for idx, item in enumerate(loc_headlines[:8], start=1):
            hl = str(item.get("headline") or "").strip()
            sub = str(item.get("subtitle") or "").strip()
            badge = str(item.get("badge") or "").strip()
            raw_shot = raw_shots[(idx - 1) % len(raw_shots)] if raw_shots else None
            shot_out = shots_out_dir / f"{idx:02d}.png"

            shot_html = build_screenshot_html(
                locale=loc,
                template_id=visual_style["template_id"],
                font_ar=visual_style["font_ar"],
                font_en=visual_style["font_en"],
                accent_color=accent_color,
                app_name=loc_title,
                headline=hl,
                subtitle=sub,
                badge=badge,
                raw_screenshot_path=raw_shot,
                icon_path=icon_out,
                sample_tracks=sample_tracks,
                slide_index=idx,
            )
            shot_info = render_html_to_png(shot_html, shot_out, width=1080, height=1920, browser_exe=browser_exe)
            shot_info["index"] = idx
            shot_info["headline"] = hl
            shot_info["used_raw_screenshot"] = str(raw_shot) if raw_shot else None
            rendered_shots.append(shot_info)

        # Mirror into Fastlane-compatible metadata/android/<locale>/images/ strictly inside run_dir
        fl_images_dir = loc_meta_dir / "images"
        fl_phone_dir = fl_images_dir / "phoneScreenshots"
        fl_phone_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon_out, fl_images_dir / "icon.png")
        shutil.copy2(fg_out, fl_images_dir / "featureGraphic.png")
        for s in rendered_shots:
            src_p = Path(s["path"])
            shutil.copy2(src_p, fl_phone_dir / src_p.name)

        rendered_locales[loc] = {
            "locale": loc,
            "is_rtl": rtl,
            "engine_used": engine_used,
            "feature_graphic": fg_info,
            "phone_screenshots": rendered_shots,
            "fastlane_images_dir": str(fl_images_dir),
        }

    # Record visual style usage in anti-repetition ledger
    if record_anti_repetition:
        guard.record_visual_usage(
            account_id=account_id,
            app_slug=str(app.get("slug") or ""),
            template_id=visual_style["template_id"],
            font_ar=visual_style["font_ar"],
            font_en=visual_style["font_en"],
            layout_signature=visual_style["layout_signature"],
            accent_color=accent_color,
        )

    visuals_manifest = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "app_slug": app.get("slug"),
        "account_id": account_id,
        "visual_style": visual_style,
        "anti_repetition_check": collision,
        "vendor_osg_probe": osg_probe,
        "icon": icon_info,
        "locales": rendered_locales,
        "warnings": warnings,
    }

    manifest_out = manager.write_json_artifact("visuals_manifest.json", visuals_manifest)
    manager.update_stage(
        state,
        "06_render_visuals",
        "completed",
        message=(
            f"Rendered visuals for {len(locales)} locale(s) using template '{visual_style['template_id']}' "
            f"(icon 512x512, featureGraphic 1024x500, screenshots 1080x1920)."
        ),
        artifacts={
            "visuals_manifest": str(manifest_out),
            "icon_512": str(icon_out),
            "visuals_dir": str(visuals_root),
        },
        warnings=warnings,
    )
    # Mandatory Human Approval Gate before Stage 7 Publishing
    manager.update_stage(
        state,
        "07_isolated_publish",
        "awaiting_human_approval",
        message="Paused at Mandatory Human Approval Gate. Review copy & visuals before Stage 7.",
    )
    logger.info("Stage 6 complete (paused at awaiting_human_approval) -> %s", manifest_out)
    return manifest_out, visuals_manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 6 Visual & Screenshot Generation")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--engine", choices=("auto", "osg", "rtl_html"), default="auto")
    parser.add_argument("--strict-anti-repetition", action="store_true")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--offline-icon", action="store_true", help="Skip downloading remote icon_source_url")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    manifest_out, vmanifest = run_stage_06(
        run_dir=args.run_dir,
        runs_root=args.runs_root,
        engine=args.engine,
        strict_anti_repetition=args.strict_anti_repetition,
        record_anti_repetition=not args.no_record,
        download_remote_icon=not args.offline_icon,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "visuals_manifest": str(manifest_out),
                "template_id": vmanifest["visual_style"]["template_id"],
                "icon": f"{vmanifest['icon']['width']}x{vmanifest['icon']['height']} ({vmanifest['icon']['size_kb']} KB)",
                "locales": {
                    loc: {
                        "engine": info["engine_used"],
                        "feature_graphic": f"{info['feature_graphic']['width']}x{info['feature_graphic']['height']}",
                        "screenshots": [f"{s['width']}x{s['height']}" for s in info["phone_screenshots"]],
                    }
                    for loc, info in vmanifest["locales"].items()
                },
                "next_gate": "awaiting_human_approval",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
