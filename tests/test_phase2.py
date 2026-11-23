"""Automated unit & integration tests for Phase 2:
- Stage 4 ASO Research graceful degradation & complaint categorization
- Stage 5 Copywriting Brief & Policy Guard (Arabic normalization, stuffing, competitor blocking)
- Security Anti-Repetition Guard (cross-account template & copy isolation)
- Stage 6 Visual & Screenshot Renderer (Arabic RTL & English LTR exact Play dimensions)
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from core.config_loader import load_play_policies
from core.run_state import RunStateManager
from security.anti_repetition import AntiRepetitionGuard

stage4 = importlib.import_module("stages.04_aso_research")
stage5 = importlib.import_module("stages.05_copy_and_policy_guard")
stage6 = importlib.import_module("stages.06_render_visuals")


# ---------------------------------------------------------------------------
# 1. Arabic Normalization & Policy Guard Tests (Stage 5)
# ---------------------------------------------------------------------------

def test_arabic_normalization_rules() -> None:
    raw = "أَغَانِي إِسْلَام كَابُونْجَا — الآفْضَلُ عَلَى مُسْتَوَى المَدِينَة"
    norm = stage5.normalize_arabic(raw)
    assert "أ" not in norm and "إ" not in norm and "آ" not in norm
    assert "ى" not in norm and "ة" not in norm
    assert "اغاني اسلام كابونجا" in norm
    assert "علي مستوي المدينه" in norm


def test_policy_guard_catches_arabic_keyword_stuffing_across_variants() -> None:
    policies = load_play_policies()
    # Mix hamza, tashkeel, and taa marbuta variants of "أغنية" / "اغنيه" (> 10 repeats)
    stuffed_variants = [
        "أغنية", "اغنية", "أغنيه", "اغنيه", "أُغْنِيَة", "إغنية",
        "آغنية", "اغـنية", "أغنية", "اغنيه", "أغنية", "اغنية",
    ]
    filler = "استمتع بتجربة استماع رائعة وسريعة وواضحة للموسيقى الشعبية في كل الأوقات بسهولة تامة " * 3
    full_desc = filler + " " + " ".join(stuffed_variants)

    res = stage5.validate_locale_copy(
        locale="ar",
        title="مهرجانات كابونجا",
        short_description="استمع لمقاطعك المفضلة بجودة واضحة بدون إنترنت",
        full_description=full_desc,
        changelogs={"1.txt": "تحسينات في الأداء واستقرار التشغيل."},
        policies=policies,
    )
    codes = {e["code"] for e in res["errors"]}
    assert not res["passed"]
    assert "KEYWORD_STUFFING_REPEATS" in codes or "KEYWORD_STUFFING_DENSITY" in codes


def test_policy_guard_catches_normalized_competitor_and_banned_arabic_claims() -> None:
    policies = load_play_policies()
    aso_mock = {
        "competitor_brands": [
            {"app_id": "com.anghami.app", "title": "أنغامي بلس - موسيقى"},
            {"app_id": "com.spotify.music", "title": "Spotify: Music and Podcasts"},
        ]
    }
    comp_phrases = stage5._extract_competitor_trademark_phrases(
        aso_mock,
        own_names=["اسلام كابونجا", "eslam-kabonga"],
    )

    # Uses normalized variant "انغامي بلس" (without hamza) + banned claim "الأفضل في العالم"
    res = stage5.validate_locale_copy(
        locale="ar",
        title="مهرجانات كابونجا 🔥",
        short_description="حمل الآن الأفضل في العالم للاستماع اليومي",
        full_description="تطبيق خفيف يعمل مثل انغامي بلس ويتيح الاستماع لمقاطعك المفضلة بسهولة.",
        changelogs={"1.txt": "إصدار جديد"},
        policies=policies,
        competitor_phrases=comp_phrases,
    )
    codes = {e["code"] for e in res["errors"]}
    assert not res["passed"]
    assert "TITLE_EMOJI_ABUSE" in codes
    assert "BANNED_PROMO_CLAIM" in codes
    assert "COMPETITOR_TRADEMARK_MENTION" in codes


def test_policy_guard_passes_clean_bilingual_listing() -> None:
    policies = load_play_policies()
    res_ar = stage5.validate_locale_copy(
        locale="ar",
        title="أغاني إسلام كابونجا بدون نت",
        short_description="استمع لمختارات إسلام كابونجا بجودة واضحة وتشغيل مستقر بدون إنترنت.",
        full_description=(
            "اكتشف تجربة استماع مريحة ومباشرة لمختارات إسلام كابونجا في أي وقت ومكان دون الحاجة "
            "إلى اتصال بالشبكة. تم تصميم الواجهة لتمنحك وصولاً فورياً إلى قوائمك المفضلة مع تشغيل "
            "هادئ في الخلفية وتحكم سلس من شاشة القفل.\n\n"
            "أبرز المزايا:\n"
            "• تشغيل فوري للمقاطع الصوتية دون استهلاك بيانات الهاتف.\n"
            "• صوت نقي ومتوازن يناسب السماعات والسيارة.\n"
            "• إمكانية التكرار والتشغيل العشوائي وإدارة قائمة المفضلة.\n"
            "• أداء خفيف يحافظ على مساحة التخزين والبطارية."
        ),
        changelogs={"1.txt": "تحسين استقرار التشغيل في الخلفية وتحديث الواجهة."},
        policies=policies,
    )
    assert res_ar["passed"], f"Unexpected AR policy errors: {res_ar['errors']}"

    res_en = stage5.validate_locale_copy(
        locale="en-US",
        title="Eslam Kabonga Offline Audio",
        short_description="Listen to Eslam Kabonga tracks offline with clean background playback.",
        full_description=(
            "Enjoy a smooth, lightweight offline listening experience built for everyday playback. "
            "Browse curated tracks quickly, play audio reliably in the background, and manage "
            "your listening queue right from the lock screen without relying on mobile data.\n\n"
            "Highlights:\n"
            "- Instant local playback with zero buffering delays.\n"
            "- Balanced audio clarity designed for headphones and car speakers.\n"
            "- Simple favorites list, shuffle, and repeat controls.\n"
            "- Compact footprint optimized for battery efficiency."
        ),
        changelogs={"1.txt": "Improved background playback stability and polished layout."},
        policies=policies,
    )
    assert res_en["passed"], f"Unexpected EN policy errors: {res_en['errors']}"


# ---------------------------------------------------------------------------
# 2. Anti-Repetition Guard Tests
# ---------------------------------------------------------------------------

def test_anti_repetition_assigns_unique_styles_and_flags_cross_account_copy(tmp_path: Path) -> None:
    db = tmp_path / "anti_rep.sqlite3"
    guard = AntiRepetitionGuard(db_path=db)

    style_a = guard.select_style_for_account("account_A")
    guard.record_visual_usage(
        account_id="account_A",
        app_slug="app-one",
        template_id=style_a["visual"]["template_id"],
        font_ar=style_a["visual"]["font_ar"],
        font_en=style_a["visual"]["font_en"],
        layout_signature=style_a["visual"]["layout_signature"],
    )
    sample_copy = (
        "استمع لمختارات الموسيقى الشعبية بدون انترنت وبجودة صوت واضحة جدا "
        "مع تشغيل خلفي مريح وواجهة بسيطة وسريعة تناسب جميع الهواتف الذكية"
    )
    guard.record_copy_usage(
        account_id="account_A",
        app_slug="app-one",
        locale="ar",
        tone=style_a["copy"]["tone"],
        structure_signature=style_a["copy"]["structure_signature"],
        full_description=sample_copy,
    )

    # Separate account_B MUST receive a distinct visual template and copy tone
    style_b = guard.select_style_for_account("account_B")
    assert style_b["visual"]["template_id"] != style_a["visual"]["template_id"]
    assert style_b["copy"]["tone"] != style_a["copy"]["tone"]

    # Cross-account copy reuse MUST be flagged
    cross_check = guard.check_copy_collision(
        account_id="account_B",
        locale="ar",
        full_description=sample_copy,
        tone=style_a["copy"]["tone"],
        structure_signature=style_a["copy"]["structure_signature"],
    )
    assert cross_check["collision_detected"] is True
    assert cross_check["max_similarity"] >= 0.9

    # Same account updating its own app MUST NOT trigger a cross-account collision
    same_check = guard.check_copy_collision(
        account_id="account_A",
        locale="ar",
        full_description=sample_copy,
        tone=style_a["copy"]["tone"],
        structure_signature=style_a["copy"]["structure_signature"],
    )
    assert same_check["collision_detected"] is False


# ---------------------------------------------------------------------------
# 3. Stage 4 Graceful Degradation Test
# ---------------------------------------------------------------------------

def test_stage4_review_mining_graceful_degradation() -> None:
    competitors = [
        {"app_id": "com.broken.competitor", "title": "Broken App"},
        {"app_id": "com.working.competitor", "title": "Working App"},
    ]

    def fake_reviews(app_id, **kwargs):
        if app_id == "com.broken.competitor":
            raise RuntimeError("Simulated 429 rate-limit / timeout")
        return (
            [
                {"content": "الاعلانات كثيرة جدا ومزعجة للغاية", "score": 1, "thumbsUpCount": 12},
                {"content": "التطبيق لا يعمل بدون نت ويطلب انترنت دائما", "score": 1, "thumbsUpCount": 8},
            ],
            None,
        )

    with mock.patch("google_play_scraper.reviews", side_effect=fake_reviews):
        summary, warnings = stage4.fetch_competitor_low_star_reviews(
            competitors=competitors,
            lang="ar",
            country="eg",
            reviews_per_competitor=10,
            delay_seconds=0.0,
        )

    # Must survive competitor 1 failure and still analyze competitor 2
    assert len(summary["degraded_competitors"]) == 2  # 1-star & 2-star attempts on broken app
    assert len(warnings) >= 2
    assert summary["total_low_star_reviews"] == 4
    assert "ads_intrusive" in summary["categories"]
    assert "offline_playback_issues" in summary["categories"]


# ---------------------------------------------------------------------------
# 4. Stage 6 Visual Rendering & Exact Play Dimensions Test (AR RTL + EN LTR)
# ---------------------------------------------------------------------------

def test_stage6_renders_exact_play_dimensions_ar_rtl_and_en_ltr(tmp_path: Path) -> None:
    runs_root = tmp_path / "workspace_runs"
    anti_db = tmp_path / "anti_rep.sqlite3"
    manager, state = RunStateManager.create_new("demo-music", mode="standalone", runs_root=runs_root)

    manifest = {
        "schema_version": 1,
        "mode": "standalone",
        "app": {
            "slug": "demo-music",
            "display_name": "مختارات صوتية",
            "package_name": "com.example.demomusic",
            "primary_category": "MUSIC_AND_AUDIO",
            "target_locales": ["ar", "en-US"],
        },
        "branding": {
            "accent_color": "#00FF00",
            "icon_local_path": None,
            "icon_source_url": None,
        },
        "audio_catalog": {
            "available": True,
            "top_tracks": ["مقطع البداية", "ليالي القاهرة"],
        },
        "account": {"account_id": "dev_visual_test", "publish_mode": "manual_bundle"},
    }
    manager.write_json_artifact("intake_manifest.json", manifest)
    manager.update_stage(state, "01_intake", "completed")

    # Prepare brief & write 2 concise headlines per locale
    stage5.prepare_copy_brief(run_dir=manager.run_dir, runs_root=runs_root, anti_rep_db=anti_db)
    (manager.run_dir / "metadata" / "android" / "ar" / "title.txt").write_text("مختارات صوتية بدون نت", encoding="utf-8")
    (manager.run_dir / "metadata" / "android" / "ar" / "short_description.txt").write_text(
        "استمع لمقاطعك المفضلة بجودة عالية بدون إنترنت.", encoding="utf-8"
    )
    (manager.run_dir / "metadata" / "android" / "en-US" / "title.txt").write_text("Offline Audio Mix", encoding="utf-8")
    (manager.run_dir / "metadata" / "android" / "en-US" / "short_description.txt").write_text(
        "Play your favorite tracks offline with clear audio.", encoding="utf-8"
    )
    manager.write_json_artifact(
        "screenshot_headlines.json",
        {
            "ar": [
                {"index": 1, "headline": "استمع بدون إنترنت في أي وقت", "subtitle": "تشغيل فوري وسلس", "badge": "بدون نت"},
                {"index": 2, "headline": "جودة صوت نقية وتشغيل خلفي", "subtitle": "تحكم من شاشة القفل", "badge": "صوت نقي"},
            ],
            "en-US": [
                {"index": 1, "headline": "Listen Offline Anytime", "subtitle": "Instant local playback", "badge": "Offline"},
                {"index": 2, "headline": "Clear Background Audio", "subtitle": "Full lock-screen controls", "badge": "Crisp Sound"},
            ],
        },
    )

    manifest_out, vmanifest = stage6.run_stage_06(
        run_dir=manager.run_dir,
        runs_root=runs_root,
        anti_rep_db=anti_db,
        engine="auto",
        download_remote_icon=False,
    )

    assert manifest_out.exists()
    # Verify exact Google Play PNG dimensions via Pillow
    icon_path = Path(vmanifest["icon"]["path"])
    with Image.open(icon_path) as im:
        assert im.size == (512, 512)
    assert icon_path.stat().st_size <= 1024 * 1024

    for loc in ("ar", "en-US"):
        loc_data = vmanifest["locales"][loc]
        assert loc_data["is_rtl"] is (loc == "ar")
        fg_path = Path(loc_data["feature_graphic"]["path"])
        with Image.open(fg_path) as fg_im:
            assert fg_im.size == (1024, 500)

        assert len(loc_data["phone_screenshots"]) == 2
        for shot in loc_data["phone_screenshots"]:
            with Image.open(shot["path"]) as s_im:
                assert s_im.size == (1080, 1920)

        # Verify Fastlane image mirror inside workspace_runs/
        fl_dir = Path(loc_data["fastlane_images_dir"])
        assert (fl_dir / "icon.png").exists()
        assert (fl_dir / "featureGraphic.png").exists()
        assert (fl_dir / "phoneScreenshots" / "01.png").exists()

    # Verify Mandatory Human Approval Gate pause
    _, final_state = RunStateManager.load(manager.run_dir, runs_root=runs_root)
    assert final_state.stages["06_render_visuals"].status == "completed"
    assert final_state.stages["07_isolated_publish"].status == "awaiting_human_approval"
    assert final_state.overall_status == "awaiting_human_approval"
