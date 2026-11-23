"""Stage 4 — ASO & Competitor Intelligence (stages/04_aso_research.py).

Orchestrates:
  1. Node.js bridge (`stages/bridges/aso_node_bridge.cjs`) driving unmodified
     `vendors/google-play-scraper` (ESM) + `vendors/aso` (CJS via modern store injection).
  2. Python `google-play-scraper` (`JoMingyu/google-play-scraper`) to mine 1-star & 2-star
     competitor reviews and categorize real user complaints.

Zero Google Play Console credentials are used (public store intelligence only).
Implements gentle rate-limit delays, explicit timeouts, and graceful degradation when any
individual competitor or review fetch fails.

All outputs are written strictly inside workspace_runs/<app-slug>/<run-timestamp>/.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.config_loader import load_studio_config
from core.env_resolver import resolve_environment
from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso

logger = get_logger("stage.04_aso_research")

BRIDGE_SCRIPT = STUDIO_ROOT / "stages" / "bridges" / "aso_node_bridge.cjs"

# Multilingual (AR + EN) complaint categorization patterns for 1-star & 2-star reviews
COMPLAINT_PATTERNS: dict[str, tuple[str, ...]] = {
    "ads_intrusive": (
        "اعلان", "اعلانات", "إعلان", "إعلانات", "دعايه", "دعاية",
        "ads", "advert", "popup", "pop-up", "too many ads", "commercial",
    ),
    "offline_playback_issues": (
        "نت", "انترنت", "إنترنت", "اوفلاين", "أوفلاين", "بدون نت", "يحتاج نت", "لا يعمل بدون",
        "offline", "internet", "wifi", "wi-fi", "no connection", "network", "download",
    ),
    "crashes_bugs": (
        "يخرج", "يفصل", "يتوقف", "تهنيج", "يعلق", "مشكله", "مشكلة", "لا يعمل", "ما يشتغل", "مايشتغل", "عطل", "خطا",
        "crash", "freeze", "bug", "error", "stuck", "stopped", "not working", "won't open", "broken",
    ),
    "audio_quality": (
        "صوت", "الصوت", "تقطيع", "يقطع", "ضعيف", "منخفض", "تشويش", "خلفية", "الخلفية", "شاشة",
        "sound", "audio", "volume", "quality", "background", "lock screen", "stops playing", "cut",
    ),
    "missing_songs_or_features": (
        "ناقص", "غير موجود", "قليل", "قديم", "جديد", "تحديث", "اغاني", "أغاني", "مهرجانات", "كلمات",
        "missing", "few songs", "old songs", "update", "playlist", "search", "lyrics", "equalizer",
    ),
    "ui_ux_friction": (
        "بطيء", "بطئ", "ثقيل", "معقد", "واجهة", "تصميم", "صعب",
        "slow", "lag", "heavy", "ui", "interface", "confusing", "hard to use",
    ),
    "battery_performance": (
        "بطارية", "البطارية", "حرارة", "يسخن", "مساحة", "ذاكرة",
        "battery", "drain", "storage", "memory", "ram", "heat",
    ),
}


def derive_seed_keywords(manifest: dict[str, Any], explicit_seeds: list[str] | None = None) -> list[str]:
    """Derive clean, authentic seed keywords from Stage 1 intake_manifest.json."""
    if explicit_seeds:
        return list(dict.fromkeys(s.strip() for s in explicit_seeds if s.strip()))

    seeds: list[str] = []
    app = manifest.get("app", {})
    catalog = manifest.get("audio_catalog", {})
    store_seed = manifest.get("store_seed", {})

    # Mode B explicit seeds
    for kw in store_seed.get("seed_keywords", []) or []:
        if str(kw).strip():
            seeds.append(str(kw).strip())

    # Mode A artist / display name / categories
    artist = str(app.get("artist_name") or "").strip()
    display = str(app.get("display_name") or "").strip()
    # Strip parenthetical suffixes like "(نعمه)" for cleaner Play Store search seeds
    clean_artist = re.sub(r"\s*\([^)]*\)\s*", " ", artist).strip()
    clean_display = re.sub(r"\s*\([^)]*\)\s*", " ", display).strip()

    if clean_artist:
        seeds.append(f"اغاني {clean_artist}")
        seeds.append(f"{clean_artist} بدون نت")
    elif clean_display:
        seeds.append(clean_display)

    for a_name in (catalog.get("artist_names") or [])[:2]:
        cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", str(a_name)).strip()
        if cleaned and cleaned != clean_artist:
            seeds.append(f"اغاني {cleaned}")

    for cat in (catalog.get("category_names") or [])[:2]:
        if str(cat).strip():
            seeds.append(f"{str(cat).strip()} بدون نت")

    primary_cat = str(app.get("primary_category") or "").upper()
    if primary_cat == "MUSIC_AND_AUDIO":
        seeds.extend(["مهرجانات بدون نت", "اغاني بدون نت", "offline music player"])
    elif not seeds:
        seeds.extend([clean_display or app.get("slug", "android app")])

    # Deduplicate preserving order
    deduped: list[str] = []
    seen: set[str] = set()
    for s in seeds:
        key = s.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(s.strip())
    return deduped[:8]


def categorize_complaint(text: str) -> str:
    lowered = text.lower()
    for category, keywords in COMPLAINT_PATTERNS.items():
        if any(kw in lowered for kw in keywords):
            return category
    return "other"


def fetch_competitor_low_star_reviews(
    competitors: list[dict[str, Any]],
    lang: str = "ar",
    country: str = "eg",
    reviews_per_competitor: int = 20,
    delay_seconds: float = 0.35,
    max_competitors_for_reviews: int = 6,
) -> tuple[dict[str, Any], list[str]]:
    """Mine 1-star & 2-star reviews using JoMingyu/google-play-scraper with graceful degradation."""
    warnings: list[str] = []
    try:
        from google_play_scraper import Sort, reviews as gplay_reviews
    except ImportError as exc:
        warnings.append(f"Python google-play-scraper import failed: {exc}")
        return {"total_low_star_reviews": 0, "categories": {}, "degraded_competitors": []}, warnings

    category_counts: Counter[str] = Counter()
    category_samples: dict[str, list[dict[str, Any]]] = {}
    degraded_competitors: list[dict[str, str]] = []
    total_collected = 0

    for comp in competitors[:max_competitors_for_reviews]:
        app_id = comp.get("app_id")
        if not app_id:
            continue
        for star_score in (1, 2):
            try:
                result_list, _ = gplay_reviews(
                    app_id,
                    lang=lang,
                    country=country,
                    sort=Sort.MOST_RELEVANT,
                    count=max(5, reviews_per_competitor // 2),
                    filter_score_with=star_score,
                )
                for rev in result_list or []:
                    content = str(rev.get("content") or "").strip()
                    if len(content) < 4:
                        continue
                    cat = categorize_complaint(content)
                    category_counts[cat] += 1
                    total_collected += 1
                    samples = category_samples.setdefault(cat, [])
                    if len(samples) < 5:
                        samples.append(
                            {
                                "app_id": app_id,
                                "app_title": comp.get("title", ""),
                                "score": rev.get("score", star_score),
                                "thumbs_up": rev.get("thumbsUpCount", 0),
                                "content": content[:320],
                            }
                        )
            except Exception as exc:
                msg = f"Review mining degraded for {app_id} ({star_score}-star): {exc}"
                logger.warning(msg)
                warnings.append(msg)
                degraded_competitors.append({"app_id": app_id, "star": str(star_score), "error": str(exc)})
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    ranked_categories: dict[str, Any] = {}
    for cat, count in category_counts.most_common():
        ranked_categories[cat] = {
            "count": count,
            "share_percent": round((count / total_collected) * 100.0, 1) if total_collected else 0.0,
            "samples": category_samples.get(cat, []),
        }

    return (
        {
            "total_low_star_reviews": total_collected,
            "categories": ranked_categories,
            "degraded_competitors": degraded_competitors,
        },
        warnings,
    )


def run_node_aso_bridge(
    run_dir: Path,
    request_payload: dict[str, Any],
    node_bin: str,
    subprocess_timeout_seconds: int = 180,
) -> tuple[dict[str, Any], list[str]]:
    """Invoke stages/bridges/aso_node_bridge.cjs with timeout and graceful degradation."""
    warnings: list[str] = []
    bridge_in = run_dir / "_aso_bridge_request.json"
    bridge_out = run_dir / "_aso_bridge_response.json"
    bridge_in.write_text(json.dumps(request_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        proc = subprocess.run(
            [node_bin, str(BRIDGE_SCRIPT), str(bridge_in), str(bridge_out)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=subprocess_timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            err_tail = (proc.stderr or proc.stdout or "").strip()[-600:]
            warnings.append(f"Node ASO bridge exited with code {proc.returncode}: {err_tail}")
        if bridge_out.exists():
            data = json.loads(bridge_out.read_text(encoding="utf-8"))
            warnings.extend(data.get("warnings", []))
            return data, warnings
    except subprocess.TimeoutExpired:
        warnings.append(f"Node ASO bridge timed out after {subprocess_timeout_seconds}s; continuing with degraded empty bridge data.")
    except Exception as exc:
        warnings.append(f"Node ASO bridge execution error: {exc}")
    finally:
        # Clean up temporary bridge scratch files inside run_dir
        for tmp in (bridge_in, bridge_out):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    return (
        {
            "node_version": None,
            "country": request_payload.get("country"),
            "lang": request_payload.get("lang"),
            "seed_keywords": request_payload.get("seed_keywords", []),
            "autocomplete_suggestions": {},
            "competitors": [],
            "keyword_opportunity_matrix": [],
            "warnings": warnings,
        },
        warnings,
    )


def run_stage_04(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    seed_keywords: list[str] | None = None,
    lang: str | None = None,
    country: str | None = None,
    max_competitors: int = 10,
    max_scored_keywords: int = 6,
    reviews_per_competitor: int = 16,
    delay_ms: int = 350,
    call_timeout_ms: int = 15000,
    skip_reviews: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Execute Stage 4 ASO & Competitor Intelligence and write aso_research.json."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    manifest_path = manager.run_dir / "intake_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"intake_manifest.json not found in {manager.run_dir}. Run Stage 1 first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manager.update_stage(state, "04_aso_research", "running", message="Starting Stage 4 ASO & competitor research")

    config = load_studio_config()
    env_info = resolve_environment(config=config)
    node_bin = env_info.node_bin or "node"

    # Choose default lang/country from target_locales
    locales = manifest.get("app", {}).get("target_locales", ["ar", "en-US"])
    primary_locale = locales[0] if locales else "ar"
    resolved_lang = lang or ("ar" if primary_locale.lower().startswith("ar") else "en")
    resolved_country = country or ("eg" if resolved_lang == "ar" else "us")

    seeds = derive_seed_keywords(manifest, explicit_seeds=seed_keywords)
    logger.info("Stage 4 ASO seeds (%s-%s): %s", resolved_lang, resolved_country, seeds)

    bridge_req = {
        "country": resolved_country,
        "lang": resolved_lang,
        "seed_keywords": seeds,
        "own_package_name": manifest.get("app", {}).get("package_name", ""),
        "max_competitors": max_competitors,
        "max_scored_keywords": max_scored_keywords,
        "delay_ms": delay_ms,
        "call_timeout_ms": call_timeout_ms,
    }
    bridge_data, bridge_warnings = run_node_aso_bridge(
        run_dir=manager.run_dir,
        request_payload=bridge_req,
        node_bin=node_bin,
    )

    competitors = bridge_data.get("competitors", [])
    review_warnings: list[str] = []
    if skip_reviews or not competitors:
        complaints_summary = {
            "total_low_star_reviews": 0,
            "categories": {},
            "degraded_competitors": [],
        }
    else:
        complaints_summary, review_warnings = fetch_competitor_low_star_reviews(
            competitors=competitors,
            lang=resolved_lang,
            country=resolved_country,
            reviews_per_competitor=reviews_per_competitor,
            delay_seconds=max(0.1, delay_ms / 1000.0),
        )

    all_warnings = list(dict.fromkeys(bridge_warnings + review_warnings))

    # Extract competitor brand/title list for Stage 5 Policy Guard trademark checks
    competitor_brands = []
    for c in competitors:
        if c.get("title"):
            competitor_brands.append(
                {
                    "app_id": c.get("app_id", ""),
                    "title": c["title"],
                    "developer": c.get("developer", ""),
                }
            )

    aso_report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "app_slug": manifest["app"]["slug"],
        "package_name": manifest["app"]["package_name"],
        "market": {"lang": resolved_lang, "country": resolved_country},
        "vendor_runtime": {
            "node_version": bridge_data.get("node_version"),
            "google_play_scraper_vendor": "vendors/google-play-scraper (unmodified ESM)",
            "aso_vendor": "vendors/aso (unmodified CJS with injected modern gplay store)",
            "python_review_miner": "google-play-scraper (JoMingyu)",
        },
        "seed_keywords": seeds,
        "autocomplete_suggestions": bridge_data.get("autocomplete_suggestions", {}),
        "competitors": competitors,
        "competitor_brands": competitor_brands,
        "keyword_opportunity_matrix": bridge_data.get("keyword_opportunity_matrix", []),
        "competitor_complaints": complaints_summary,
        "warnings": all_warnings,
    }

    output_path = manager.write_json_artifact("aso_research.json", aso_report)
    manager.update_stage(
        state,
        "04_aso_research",
        "completed",
        message=(
            f"ASO research complete: {len(competitors)} competitors, "
            f"{len(aso_report['keyword_opportunity_matrix'])} scored keywords, "
            f"{complaints_summary['total_low_star_reviews']} 1-2 star reviews analyzed."
        ),
        artifacts={"aso_research": str(output_path)},
        warnings=all_warnings,
    )
    logger.info("Stage 4 saved -> %s", output_path)
    return output_path, aso_report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 4 ASO & Competitor Intelligence")
    parser.add_argument("--run-dir", type=Path, required=True, help="Run directory created by Stage 1")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--seed-keywords", help="Comma-separated custom seed keywords")
    parser.add_argument("--lang", help="Play Store language code (e.g. ar, en)")
    parser.add_argument("--country", help="Play Store country code (e.g. eg, sa, us)")
    parser.add_argument("--max-competitors", type=int, default=10)
    parser.add_argument("--max-scored-keywords", type=int, default=6)
    parser.add_argument("--reviews-per-competitor", type=int, default=16)
    parser.add_argument("--delay-ms", type=int, default=350, help="Rate-limit delay between store requests (ms)")
    parser.add_argument("--call-timeout-ms", type=int, default=15000, help="Per-call timeout (ms)")
    parser.add_argument("--skip-reviews", action="store_true", help="Skip 1-2 star review mining")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    seeds = [s.strip() for s in args.seed_keywords.split(",") if s.strip()] if args.seed_keywords else None

    out_path, report = run_stage_04(
        run_dir=args.run_dir,
        runs_root=args.runs_root,
        seed_keywords=seeds,
        lang=args.lang,
        country=args.country,
        max_competitors=args.max_competitors,
        max_scored_keywords=args.max_scored_keywords,
        reviews_per_competitor=args.reviews_per_competitor,
        delay_ms=args.delay_ms,
        call_timeout_ms=args.call_timeout_ms,
        skip_reviews=args.skip_reviews,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "aso_research": str(out_path),
                "competitors_count": len(report["competitors"]),
                "scored_keywords_count": len(report["keyword_opportunity_matrix"]),
                "low_star_reviews_count": report["competitor_complaints"]["total_low_star_reviews"],
                "warnings_count": len(report["warnings"]),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
