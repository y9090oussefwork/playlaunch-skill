"""Stage 5 — Copywriting Brief, Localization & Policy Guard (stages/05_copy_and_policy_guard.py).

Two explicit subcommands so any AI coding agent writes high-converting localized copy directly
using Stage 4 intelligence (zero external LLM API keys required):
  - `prepare`: Builds `copy_brief.json` and scaffolds Fastlane-standard `metadata/android/<locale>/`.
  - `validate`: Enforces Google Play Store policy rules (`config/play_policies.json`),
                Arabic-normalized keyword stuffing & competitor trademark detection,
                and cross-account anti-repetition checks (`security/anti_repetition.py`).

All outputs are written strictly inside workspace_runs/<app-slug>/<run-timestamp>/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.config_loader import load_play_policies
from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso
from core.validators import assert_path_within
from security.anti_repetition import DEFAULT_ANTI_REPETITION_DB, AntiRepetitionGuard

logger = get_logger("stage.05_copy_policy")

# Arabic normalization regexes: tashkeel (harakat), tatweel, alef variants, yaa, taa marbuta
_ARABIC_TASHKEEL_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9\u0621-\u064A]+", re.UNICODE)

# Broad emoji matcher covering emoticons, dingbats, symbols, flags, and variation selectors
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]",
    re.UNICODE,
)

# Function stopwords excluded from keyword stuffing density (all pre-normalized)
STOPWORDS_NORMALIZED: frozenset[str] = frozenset(
    {
        # English
        "the", "and", "for", "with", "your", "you", "our", "from", "that", "this",
        "are", "can", "all", "any", "not", "out", "use", "app", "new", "more", "into",
        "have", "has", "will", "without", "one", "per", "also", "just", "now", "when",
        # Arabic (normalized: أ/إ/آ -> ا, ى -> ي, ة -> ه)
        "في", "من", "علي", "الي", "عن", "مع", "هذا", "هذه", "التي", "الذي", "التي",
        "التي", "التي", "كل", "او", "ان", "ما", "لا", "لم", "لن", "قد", "كما", "حتي",
        "بين", "عند", "بعد", "قبل", "حول", "خلال", "عبر", "دون", "بدون", "لك", "بكل",
        "تطبيق", "بشكل", "سهل", "اي", "اذا", "وهو", "وهي", "حيث", "فقط", "جدا", "اكثر",
    }
)

# Generic store words that must not be treated as unique competitor trademarks
GENERIC_CATEGORY_WORDS_NORMALIZED: frozenset[str] = frozenset(
    {
        "music", "audio", "player", "offline", "songs", "song", "mp3", "sound", "play",
        "free", "pro", "plus", "lite", "app", "hd", "new", "best", "top", "2024", "2025", "2026", "2027",
        "اغاني", "مهرجانات", "شعبي", "شعبيه", "بدون", "نت", "انترنت", "تطبيق", "مشغل",
        "موسيقي", "صوت", "كلمات", "جميع", "كل", "جديد", "جديده", "حديثه", "كامله", "اوفلاين",
        "ترانيم", "اناشيد", "مدائح", "الحان", "كتاب", "صوتي", "راديو", "فيديو",
    }
)


def normalize_arabic(text: str) -> str:
    """Normalize Arabic text for accurate policy & keyword analysis.

    - Strips diacritics (tashkeel) and tatweel (ـ).
    - Unifies أ / إ / آ / ٱ -> ا.
    - Unifies ى -> ي.
    - Unifies ة -> ه.
    - Lowercases Latin text and collapses repeated whitespace.
    """
    if not text:
        return ""
    cleaned = _ARABIC_TASHKEEL_RE.sub("", text)
    cleaned = (
        cleaned.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ٱ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
    )
    cleaned = cleaned.lower()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def tokenize_normalized(text: str) -> list[str]:
    norm = normalize_arabic(text)
    return _TOKEN_RE.findall(norm)


def count_emojis(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


def _extract_competitor_trademark_phrases(
    aso_research: dict[str, Any] | None,
    own_names: list[str],
) -> list[dict[str, str]]:
    """Build a list of distinct competitor brand/app phrases to block from accidental mention."""
    if not aso_research:
        return []

    own_tokens: set[str] = set()
    for name in own_names:
        for tok in tokenize_normalized(name):
            if len(tok) >= 3:
                own_tokens.add(tok)

    candidates: list[dict[str, str]] = []
    seen_norm: set[str] = set()

    for comp in aso_research.get("competitor_brands", []) or aso_research.get("competitors", []) or []:
        title = str(comp.get("title") or "").strip()
        if not title:
            continue
        # Extract primary brand segment before separators like " - ", " : ", " | "
        primary_segment = re.split(r"\s*[-:|—–]\s*", title)[0].strip()
        for raw_phrase in (title, primary_segment):
            norm_phrase = normalize_arabic(raw_phrase)
            tokens = [t for t in _TOKEN_RE.findall(norm_phrase) if len(t) >= 3]
            if not tokens:
                continue
            # Filter out phrases composed entirely of generic category words or the app's own name/artist
            distinctive_tokens = [
                t for t in tokens
                if t not in GENERIC_CATEGORY_WORDS_NORMALIZED
                and t not in STOPWORDS_NORMALIZED
                and t not in own_tokens
            ]
            if not distinctive_tokens:
                continue
            # Check full distinctive phrase (must be >= 4 chars)
            key = " ".join(tokens)
            if len(key) >= 4 and key not in seen_norm:
                seen_norm.add(key)
                candidates.append(
                    {
                        "raw": raw_phrase,
                        "normalized": key,
                        "app_id": str(comp.get("app_id") or ""),
                    }
                )
    return candidates


def validate_locale_copy(
    locale: str,
    title: str,
    short_description: str,
    full_description: str,
    changelogs: dict[str, str],
    policies: dict[str, Any],
    competitor_phrases: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate a single locale's Play Store listing against all policy rules."""
    limits = policies.get("limits", {})
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def add_error(code: str, field: str, message: str, detail: Any = None) -> None:
        errors.append({"code": code, "field": field, "message": message, "detail": detail})

    def add_warning(code: str, field: str, message: str, detail: Any = None) -> None:
        warnings.append({"code": code, "field": field, "message": message, "detail": detail})

    title_s = title.strip()
    short_s = short_description.strip()
    full_s = full_description.strip()

    # 1. Presence & hard character limits
    max_title = int(limits.get("title_max_chars", 30))
    max_short = int(limits.get("short_description_max_chars", 80))
    max_full = int(limits.get("full_description_max_chars", 4000))
    max_changelog = int(limits.get("changelog_max_chars", 500))

    if not title_s:
        add_error("EMPTY_TITLE", "title", "Title cannot be empty.")
    elif len(title_s) > max_title:
        add_error("TITLE_TOO_LONG", "title", f"Title length {len(title_s)} exceeds max {max_title} chars.", len(title_s))

    if not short_s:
        add_error("EMPTY_SHORT_DESC", "short_description", "Short description cannot be empty.")
    elif len(short_s) > max_short:
        add_error(
            "SHORT_DESC_TOO_LONG",
            "short_description",
            f"Short description length {len(short_s)} exceeds max {max_short} chars.",
            len(short_s),
        )

    if not full_s:
        add_error("EMPTY_FULL_DESC", "full_description", "Full description cannot be empty.")
    elif len(full_s) > max_full:
        add_error(
            "FULL_DESC_TOO_LONG",
            "full_description",
            f"Full description length {len(full_s)} exceeds max {max_full} chars.",
            len(full_s),
        )

    for cl_name, cl_text in changelogs.items():
        cl_s = cl_text.strip()
        if len(cl_s) > max_changelog:
            add_error(
                "CHANGELOG_TOO_LONG",
                f"changelogs/{cl_name}",
                f"Changelog length {len(cl_s)} exceeds max {max_changelog} chars.",
                len(cl_s),
            )

    # 2. Emoji limits & disallowed title patterns
    title_emojis = count_emojis(title_s)
    short_emojis = count_emojis(short_s)
    full_emojis = count_emojis(full_s)

    if title_emojis > int(limits.get("max_emojis_in_title", 0)):
        add_error("TITLE_EMOJI_ABUSE", "title", f"Title contains {title_emojis} emoji(s); max allowed is {limits.get('max_emojis_in_title', 0)}.")
    if short_emojis > int(limits.get("max_emojis_in_short_description", 1)):
        add_error("SHORT_EMOJI_ABUSE", "short_description", f"Short description contains {short_emojis} emoji(s); max allowed is {limits.get('max_emojis_in_short_description', 1)}.")
    if full_emojis > int(limits.get("max_emojis_in_full_description", 8)):
        add_error("FULL_EMOJI_ABUSE", "full_description", f"Full description contains {full_emojis} emoji(s); max allowed is {limits.get('max_emojis_in_full_description', 8)}.")

    for pattern in policies.get("disallowed_title_patterns", []):
        if re.search(pattern, title_s):
            add_error("DISALLOWED_TITLE_PATTERN", "title", f"Title matched disallowed Play policy pattern '{pattern}'.", pattern)

    # 3. Banned promotional claims (normalized Arabic + English)
    banned_map = policies.get("banned_promotional_phrases", {})
    all_banned_raw = list(banned_map.get("en", [])) + list(banned_map.get("ar", []))
    fields_to_scan = {
        "title": title_s,
        "short_description": short_s,
        "full_description": full_s,
    }
    for field_name, raw_text in fields_to_scan.items():
        norm_text = f" {normalize_arabic(raw_text)} "
        for phrase in all_banned_raw:
            norm_phrase = normalize_arabic(phrase)
            if not norm_phrase:
                continue
            # Use boundary-aware match for alphanumeric phrases
            pattern = r"(?<![A-Za-z0-9\u0621-\u064A])" + re.escape(norm_phrase) + r"(?![A-Za-z0-9\u0621-\u064A])"
            if re.search(pattern, norm_text):
                add_error(
                    "BANNED_PROMO_CLAIM",
                    field_name,
                    f"Banned promotional claim detected in {field_name}: '{phrase}'",
                    phrase,
                )

    # 4. Keyword stuffing check (Arabic-normalized tokens!)
    max_density = float(limits.get("max_keyword_density_percent", 4.0))
    max_repeats = int(limits.get("max_single_word_repeats_in_full_description", 10))
    full_tokens = tokenize_normalized(full_s)
    total_tokens = len(full_tokens)
    content_tokens = [t for t in full_tokens if len(t) >= 3 and t not in STOPWORDS_NORMALIZED]
    token_counts = Counter(content_tokens)

    top_keywords_stats: list[dict[str, Any]] = []
    for word, count in token_counts.most_common(10):
        density_pct = round((count / total_tokens) * 100.0, 2) if total_tokens else 0.0
        top_keywords_stats.append({"word_normalized": word, "count": count, "density_percent": density_pct})
        if count > max_repeats:
            add_error(
                "KEYWORD_STUFFING_REPEATS",
                "full_description",
                f"Keyword '{word}' (normalized) repeated {count} times (max {max_repeats}).",
                {"word": word, "count": count, "max": max_repeats},
            )
        elif total_tokens >= 30 and count >= 4 and density_pct > max_density:
            add_error(
                "KEYWORD_STUFFING_DENSITY",
                "full_description",
                f"Keyword '{word}' (normalized) density is {density_pct}% (max {max_density}%).",
                {"word": word, "count": count, "density_percent": density_pct, "max": max_density},
            )

    # 5. Competitor trademark / app name mention check (Arabic-normalized + EN)
    if competitor_phrases:
        for field_name, raw_text in fields_to_scan.items():
            norm_text = " ".join(tokenize_normalized(raw_text))
            if not norm_text:
                continue
            for comp in competitor_phrases:
                norm_comp = comp["normalized"]
                pattern = r"(?<![A-Za-z0-9\u0621-\u064A])" + re.escape(norm_comp) + r"(?![A-Za-z0-9\u0621-\u064A])"
                if re.search(pattern, norm_text):
                    add_error(
                        "COMPETITOR_TRADEMARK_MENTION",
                        field_name,
                        f"Accidental competitor app/brand mention in {field_name}: '{comp['raw']}'",
                        comp,
                    )

    return {
        "locale": locale,
        "passed": len(errors) == 0,
        "char_counts": {
            "title": len(title_s),
            "title_max": max_title,
            "short_description": len(short_s),
            "short_description_max": max_short,
            "full_description": len(full_s),
            "full_description_max": max_full,
            "total_words": total_tokens,
        },
        "emoji_counts": {
            "title": title_emojis,
            "short_description": short_emojis,
            "full_description": full_emojis,
        },
        "top_normalized_keywords": top_keywords_stats[:8],
        "errors": errors,
        "warnings": warnings,
    }


def prepare_copy_brief(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    anti_rep_db: Path = DEFAULT_ANTI_REPETITION_DB,
    version_code: int = 1,
) -> tuple[Path, dict[str, Any]]:
    """Build copy_brief.json + scaffold Fastlane metadata folders strictly inside run_dir."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    manifest_path = manager.run_dir / "intake_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"intake_manifest.json missing in {manager.run_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    aso_path = manager.run_dir / "aso_research.json"
    aso_research = json.loads(aso_path.read_text(encoding="utf-8")) if aso_path.exists() else None

    policies = load_play_policies()
    account_id = str(manifest.get("account", {}).get("account_id") or "unassigned")
    guard = AntiRepetitionGuard(db_path=anti_rep_db)
    assigned_style = guard.select_style_for_account(account_id=account_id)

    locales = manifest.get("app", {}).get("target_locales") or ["ar", "en-US"]
    metadata_root = manager.run_dir / "metadata" / "android"
    assert_path_within(metadata_root, manager.run_dir)

    scaffolded_paths: dict[str, dict[str, str]] = {}
    for loc in locales:
        loc_dir = metadata_root / loc
        cl_dir = loc_dir / "changelogs"
        cl_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "title": loc_dir / "title.txt",
            "short_description": loc_dir / "short_description.txt",
            "full_description": loc_dir / "full_description.txt",
            "changelog": cl_dir / f"{version_code}.txt",
        }
        for p in files.values():
            if not p.exists():
                p.write_text("", encoding="utf-8")
        scaffolded_paths[loc] = {k: str(v) for k, v in files.items()}

    headlines_path = manager.run_dir / "screenshot_headlines.json"
    if not headlines_path.exists():
        skeleton_headlines = {
            loc: [
                {"index": 1, "headline": "", "subtitle": "", "badge": ""},
                {"index": 2, "headline": "", "subtitle": "", "badge": ""},
                {"index": 3, "headline": "", "subtitle": "", "badge": ""},
                {"index": 4, "headline": "", "subtitle": "", "badge": ""},
            ]
            for loc in locales
        }
        manager.write_json_artifact("screenshot_headlines.json", skeleton_headlines)

    top_keywords = []
    top_complaints = {}
    competitor_titles = []
    if aso_research:
        top_keywords = aso_research.get("keyword_opportunity_matrix", [])[:12]
        top_complaints = aso_research.get("competitor_complaints", {}).get("categories", {})
        competitor_titles = [c.get("title", "") for c in aso_research.get("competitors", [])[:10] if c.get("title")]

    brief: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "run_dir": str(manager.run_dir),
        "app": manifest.get("app", {}),
        "audio_catalog": manifest.get("audio_catalog"),
        "store_seed": manifest.get("store_seed"),
        "target_locales": locales,
        "assigned_account_style": assigned_style,
        "policy_limits": policies.get("limits", {}),
        "banned_promotional_phrases": policies.get("banned_promotional_phrases", {}),
        "aso_insights": {
            "top_keyword_opportunities": top_keywords,
            "competitor_pain_points": top_complaints,
            "competitor_titles_do_not_mention": competitor_titles,
        },
        "fastlane_metadata_paths": scaffolded_paths,
        "screenshot_headlines_path": str(headlines_path),
        "agent_instructions": (
            "Write natural, high-converting localized Play Store copy directly into the Fastlane files "
            "listed under `fastlane_metadata_paths` and populate `screenshot_headlines.json`. "
            "Follow `assigned_account_style` tone/structure, stay strictly within `policy_limits`, "
            "avoid all `banned_promotional_phrases` and competitor brand names, then run: "
            f"`python stages/05_copy_and_policy_guard.py validate --run-dir \"{manager.run_dir}\"`."
        ),
    }

    brief_path = manager.write_json_artifact("copy_brief.json", brief)
    manager.update_stage(
        state,
        "05_copy_and_policy_guard",
        "running",
        message=f"Prepared copy_brief.json and scaffolded Fastlane metadata for locales: {', '.join(locales)}.",
        artifacts={
            "copy_brief": str(brief_path),
            "screenshot_headlines": str(headlines_path),
            "fastlane_metadata_dir": str(metadata_root),
        },
    )
    logger.info("Stage 5 prepare complete -> %s", brief_path)
    return brief_path, brief


def validate_copy_run(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    anti_rep_db: Path = DEFAULT_ANTI_REPETITION_DB,
    strict_anti_repetition: bool = False,
    record_anti_repetition: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Validate all localized Fastlane metadata files + screenshot_headlines.json."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    manifest = json.loads((manager.run_dir / "intake_manifest.json").read_text(encoding="utf-8"))
    aso_path = manager.run_dir / "aso_research.json"
    aso_research = json.loads(aso_path.read_text(encoding="utf-8")) if aso_path.exists() else None
    brief_path = manager.run_dir / "copy_brief.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8")) if brief_path.exists() else {}

    policies = load_play_policies()
    app = manifest.get("app", {})
    own_names = [
        str(app.get("display_name") or ""),
        str(app.get("runtime_display_name") or ""),
        str(app.get("artist_name") or ""),
        str(app.get("slug") or ""),
    ]
    competitor_phrases = _extract_competitor_trademark_phrases(aso_research, own_names=own_names)

    metadata_root = manager.run_dir / "metadata" / "android"
    locales = app.get("target_locales") or ["ar", "en-US"]
    account_id = str(manifest.get("account", {}).get("account_id") or "unassigned")
    assigned_copy_style = brief.get("assigned_account_style", {}).get("copy", {})
    tone = str(assigned_copy_style.get("tone") or "default")
    structure_sig = str(assigned_copy_style.get("structure_signature") or "default")

    guard = AntiRepetitionGuard(db_path=anti_rep_db)
    locale_reports: dict[str, Any] = {}
    anti_rep_reports: dict[str, Any] = {}
    all_passed = True
    stage_warnings: list[str] = []

    for loc in locales:
        loc_dir = metadata_root / loc
        title = (loc_dir / "title.txt").read_text(encoding="utf-8") if (loc_dir / "title.txt").exists() else ""
        short_d = (
            (loc_dir / "short_description.txt").read_text(encoding="utf-8")
            if (loc_dir / "short_description.txt").exists()
            else ""
        )
        full_d = (
            (loc_dir / "full_description.txt").read_text(encoding="utf-8")
            if (loc_dir / "full_description.txt").exists()
            else ""
        )
        changelogs: dict[str, str] = {}
        cl_dir = loc_dir / "changelogs"
        if cl_dir.exists():
            for cl_file in sorted(cl_dir.glob("*.txt")):
                changelogs[cl_file.name] = cl_file.read_text(encoding="utf-8")

        loc_res = validate_locale_copy(
            locale=loc,
            title=title,
            short_description=short_d,
            full_description=full_d,
            changelogs=changelogs,
            policies=policies,
            competitor_phrases=competitor_phrases,
        )

        # Cross-account anti-repetition check
        anti_res = guard.check_copy_collision(
            account_id=account_id,
            locale=loc,
            full_description=full_d,
            tone=tone,
            structure_signature=structure_sig,
        )
        anti_rep_reports[loc] = anti_res
        if anti_res["collision_detected"]:
            msg = (
                f"Anti-repetition collision on locale '{loc}' against other account(s): "
                f"{[m['other_account_id'] for m in anti_res['matches']]} (max_sim={anti_res['max_similarity']})"
            )
            if strict_anti_repetition:
                loc_res["errors"].append(
                    {"code": "ANTI_REPETITION_COLLISION", "field": "full_description", "message": msg, "detail": anti_res}
                )
                loc_res["passed"] = False
            else:
                loc_res["warnings"].append(
                    {"code": "ANTI_REPETITION_WARNING", "field": "full_description", "message": msg, "detail": anti_res}
                )
                stage_warnings.append(msg)

        locale_reports[loc] = loc_res
        if not loc_res["passed"]:
            all_passed = False

    # Validate screenshot_headlines.json
    headlines_path = manager.run_dir / "screenshot_headlines.json"
    headlines_errors: list[str] = []
    if not headlines_path.exists():
        headlines_errors.append("screenshot_headlines.json is missing.")
    else:
        raw_hl = json.loads(headlines_path.read_text(encoding="utf-8"))
        for loc in locales:
            items = raw_hl.get(loc)
            if not isinstance(items, list) or len(items) < 2:
                headlines_errors.append(f"Locale '{loc}' needs at least 2 screenshot headlines.")
                continue
            for idx, item in enumerate(items, start=1):
                hl = str(item.get("headline") or "").strip() if isinstance(item, dict) else ""
                if not hl:
                    headlines_errors.append(f"Locale '{loc}' screenshot #{idx} has an empty headline.")
                elif len(hl) > 60:
                    headlines_errors.append(f"Locale '{loc}' screenshot #{idx} headline exceeds 60 chars ({len(hl)}).")

    if headlines_errors:
        all_passed = False

    # Record clean copy fingerprints into anti-repetition DB when passed
    if all_passed and record_anti_repetition:
        for loc in locales:
            full_d = (metadata_root / loc / "full_description.txt").read_text(encoding="utf-8").strip()
            if full_d:
                guard.record_copy_usage(
                    account_id=account_id,
                    app_slug=str(app.get("slug", "")),
                    locale=loc,
                    tone=tone,
                    structure_signature=structure_sig,
                    full_description=full_d,
                )

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "app_slug": app.get("slug"),
        "account_id": account_id,
        "passed": all_passed,
        "strict_anti_repetition": strict_anti_repetition,
        "locales": locale_reports,
        "anti_repetition": anti_rep_reports,
        "screenshot_headlines_validation": {
            "passed": len(headlines_errors) == 0,
            "errors": headlines_errors,
        },
    }

    report_path = manager.write_json_artifact("policy_validation_report.json", report)
    manager.update_stage(
        state,
        "05_copy_and_policy_guard",
        "completed" if all_passed else "failed",
        message=(
            "Policy Guard passed for all locales."
            if all_passed
            else "Policy Guard found violations — review policy_validation_report.json and fix copy."
        ),
        artifacts={"policy_validation_report": str(report_path)},
        warnings=stage_warnings,
    )
    logger.info("Stage 5 validate -> passed=%s report=%s", all_passed, report_path)
    return report_path, report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 5 Copywriting & Policy Guard")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prep = sub.add_parser("prepare", help="Generate copy_brief.json & scaffold Fastlane metadata files")
    p_prep.add_argument("--run-dir", type=Path, required=True)
    p_prep.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    p_prep.add_argument("--version-code", type=int, default=1)

    p_val = sub.add_parser("validate", help="Validate localized Fastlane copy against Google Play policies")
    p_val.add_argument("--run-dir", type=Path, required=True)
    p_val.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    p_val.add_argument("--strict-anti-repetition", action="store_true")
    p_val.add_argument("--no-record", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "prepare":
        brief_path, brief = prepare_copy_brief(
            run_dir=args.run_dir,
            runs_root=args.runs_root,
            version_code=args.version_code,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "command": "prepare",
                    "copy_brief": str(brief_path),
                    "target_locales": brief["target_locales"],
                    "assigned_visual_template": brief["assigned_account_style"]["visual"]["template_id"],
                    "assigned_copy_tone": brief["assigned_account_style"]["copy"]["tone"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    report_path, report = validate_copy_run(
        run_dir=args.run_dir,
        runs_root=args.runs_root,
        strict_anti_repetition=args.strict_anti_repetition,
        record_anti_repetition=not args.no_record,
    )
    print(
        json.dumps(
            {
                "status": "ok" if report["passed"] else "policy_failed",
                "command": "validate",
                "passed": report["passed"],
                "policy_validation_report": str(report_path),
                "locales_summary": {
                    loc: {
                        "passed": rep["passed"],
                        "errors": len(rep["errors"]),
                        "warnings": len(rep["warnings"]),
                        "chars": rep["char_counts"],
                    }
                    for loc, rep in report["locales"].items()
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
