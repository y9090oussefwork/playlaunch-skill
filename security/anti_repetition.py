"""Anti-Repetition & Anti-Footprint Guard (security/anti_repetition.py).

Tracks visual templates, font families, color palettes, and copywriting tones/structures
per developer `account_id` in security/anti_repetition.sqlite3 (gitignored).

Prevents cross-account visual or textual footprint overlap across separate Google Play
developer accounts while allowing a single account to maintain consistent branding across
its own updates.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import STUDIO_ROOT

DEFAULT_ANTI_REPETITION_DB = STUDIO_ROOT / "security" / "anti_repetition.sqlite3"

# Curated catalog of distinct visual templates & typography pairs
VISUAL_STYLE_CATALOG: list[dict[str, str]] = [
    {
        "template_id": "editorial_spotlight",
        "font_ar": "Dubai, Cairo, Segoe UI, Tahoma, sans-serif",
        "font_en": "Segoe UI, Inter, Arial, sans-serif",
        "layout_signature": "top_headline_angled_phone_dark",
    },
    {
        "template_id": "aurora_mesh_card",
        "font_ar": "Cairo, Dubai, Tahoma, sans-serif",
        "font_en": "Trebuchet MS, Poppins, Segoe UI, sans-serif",
        "layout_signature": "bottom_callout_centered_phone_mesh",
    },
    {
        "template_id": "minimal_clean_split",
        "font_ar": "Tajawal, Dubai, Segoe UI, sans-serif",
        "font_en": "Calibri, Plus Jakarta Sans, Arial, sans-serif",
        "layout_signature": "top_left_light_editorial_stripe",
    },
    {
        "template_id": "neon_studio_wave",
        "font_ar": "Almarai, Dubai, Tahoma, sans-serif",
        "font_en": "Verdana, Space Grotesk, Segoe UI, sans-serif",
        "layout_signature": "stacked_glow_studio_dark",
    },
    {
        "template_id": "geometric_showcase",
        "font_ar": "IBM Plex Sans Arabic, Dubai, Tahoma, sans-serif",
        "font_en": "Georgia, DM Sans, Segoe UI, sans-serif",
        "layout_signature": "grid_offset_device_header_block",
    },
]

COPY_STYLE_CATALOG: list[dict[str, Any]] = [
    {
        "tone": "warm_conversational",
        "structure_signature": "hook_bullets_story_cta",
        "section_order": ["value_hook", "key_benefits_bullets", "offline_experience_story", "soft_closing"],
    },
    {
        "tone": "crisp_feature_focused",
        "structure_signature": "summary_numbered_specs_faq",
        "section_order": ["direct_summary", "numbered_highlights", "audio_quality_notes", "quick_faq"],
    },
    {
        "tone": "editorial_curated",
        "structure_signature": "mood_intro_grouped_features_privacy",
        "section_order": ["listening_mood_intro", "grouped_feature_blocks", "lightweight_privacy_note"],
    },
    {
        "tone": "problem_solution_driven",
        "structure_signature": "pain_points_solutions_highlights",
        "section_order": ["user_pain_relief", "feature_breakdown", "usage_scenarios"],
    },
    {
        "tone": "minimal_modern_utility",
        "structure_signature": "one_liner_compact_pillars_checklist",
        "section_order": ["punchy_intro", "three_core_pillars", "compact_checklist"],
    },
]

_ARABIC_DIACRITICS_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_NON_WORD_RE = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_for_shingles(text: str) -> list[str]:
    cleaned = _ARABIC_DIACRITICS_RE.sub("", text.lower())
    cleaned = cleaned.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا")
    cleaned = cleaned.replace("ى", "ي").replace("ة", "ه")
    cleaned = _NON_WORD_RE.sub(" ", cleaned).strip()
    return [tok for tok in cleaned.split() if len(tok) >= 2]


def compute_shingles(text: str, k: int = 3) -> set[str]:
    tokens = _normalize_for_shingles(text)
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return round(inter / union, 4) if union else 0.0


class AntiRepetitionGuard:
    """Persistent tracker enforcing unique visual & copy footprints per developer account."""

    def __init__(self, db_path: Path = DEFAULT_ANTI_REPETITION_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS visual_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    app_slug TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    font_ar TEXT NOT NULL,
                    font_en TEXT NOT NULL,
                    accent_color TEXT DEFAULT '',
                    layout_signature TEXT NOT NULL,
                    used_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copy_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    app_slug TEXT NOT NULL,
                    locale TEXT NOT NULL,
                    tone TEXT NOT NULL,
                    structure_signature TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    shingles_json TEXT NOT NULL,
                    used_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def select_style_for_account(
        self,
        account_id: str,
        preferred_template: str | None = None,
        preferred_tone: str | None = None,
    ) -> dict[str, Any]:
        """Select a visual template + copywriting style unique to this account_id."""
        with self._connect() as conn:
            own_visual = conn.execute(
                "SELECT template_id, font_ar, font_en, layout_signature FROM visual_usage "
                "WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            other_templates = {
                row["template_id"]
                for row in conn.execute(
                    "SELECT DISTINCT template_id FROM visual_usage WHERE account_id != ?",
                    (account_id,),
                ).fetchall()
            }

            own_copy = conn.execute(
                "SELECT tone, structure_signature FROM copy_usage "
                "WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            other_tones = {
                row["tone"]
                for row in conn.execute(
                    "SELECT DISTINCT tone FROM copy_usage WHERE account_id != ?",
                    (account_id,),
                ).fetchall()
            }

        # Pick visual template
        chosen_visual: dict[str, str] | None = None
        if preferred_template and preferred_template not in other_templates:
            chosen_visual = next((v for v in VISUAL_STYLE_CATALOG if v["template_id"] == preferred_template), None)
        if chosen_visual is None and own_visual is not None:
            chosen_visual = {
                "template_id": own_visual["template_id"],
                "font_ar": own_visual["font_ar"],
                "font_en": own_visual["font_en"],
                "layout_signature": own_visual["layout_signature"],
            }
        if chosen_visual is None:
            available = [v for v in VISUAL_STYLE_CATALOG if v["template_id"] not in other_templates]
            pool = available if available else VISUAL_STYLE_CATALOG
            idx = int(hashlib.sha256(account_id.encode("utf-8")).hexdigest(), 16) % len(pool)
            chosen_visual = dict(pool[idx])

        # Pick copy style
        chosen_copy: dict[str, Any] | None = None
        if preferred_tone and preferred_tone not in other_tones:
            chosen_copy = next((c for c in COPY_STYLE_CATALOG if c["tone"] == preferred_tone), None)
        if chosen_copy is None and own_copy is not None:
            chosen_copy = next(
                (c for c in COPY_STYLE_CATALOG if c["tone"] == own_copy["tone"]),
                {
                    "tone": own_copy["tone"],
                    "structure_signature": own_copy["structure_signature"],
                    "section_order": [],
                },
            )
        if chosen_copy is None:
            available_copy = [c for c in COPY_STYLE_CATALOG if c["tone"] not in other_tones]
            pool_c = available_copy if available_copy else COPY_STYLE_CATALOG
            idx_c = int(hashlib.sha256((account_id + ":copy").encode("utf-8")).hexdigest(), 16) % len(pool_c)
            chosen_copy = dict(pool_c[idx_c])

        return {
            "account_id": account_id,
            "visual": chosen_visual,
            "copy": chosen_copy,
            "templates_used_by_other_accounts": sorted(other_templates),
            "tones_used_by_other_accounts": sorted(other_tones),
        }

    def check_visual_collision(
        self,
        account_id: str,
        template_id: str,
        font_ar: str,
        font_en: str,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT account_id, app_slug, template_id, font_ar, font_en "
                "FROM visual_usage WHERE account_id != ? AND template_id = ?",
                (account_id, template_id),
            ).fetchall()

        collisions = [dict(r) for r in rows]
        exact = [
            c for c in collisions if c["font_ar"] == font_ar and c["font_en"] == font_en
        ]
        return {
            "collision_detected": len(collisions) > 0,
            "exact_signature_collision": len(exact) > 0,
            "colliding_accounts": sorted({c["account_id"] for c in collisions}),
            "matches": collisions,
        }

    def record_visual_usage(
        self,
        account_id: str,
        app_slug: str,
        template_id: str,
        font_ar: str,
        font_en: str,
        layout_signature: str,
        accent_color: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO visual_usage (
                    account_id, app_slug, template_id, font_ar, font_en,
                    accent_color, layout_signature, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (account_id, app_slug, template_id, font_ar, font_en, accent_color, layout_signature, _utc_now()),
            )
            conn.commit()

    def check_copy_collision(
        self,
        account_id: str,
        locale: str,
        full_description: str,
        tone: str = "",
        structure_signature: str = "",
        similarity_threshold: float = 0.45,
    ) -> dict[str, Any]:
        """Detect copy overlap or shared structure against OTHER developer accounts."""
        new_shingles = compute_shingles(full_description)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account_id, app_slug, locale, tone, structure_signature, shingles_json "
                "FROM copy_usage WHERE account_id != ? AND locale = ?",
                (account_id, locale),
            ).fetchall()

        matches: list[dict[str, Any]] = []
        max_sim = 0.0
        for row in rows:
            other_shingles = set(json.loads(row["shingles_json"] or "[]"))
            sim = jaccard_similarity(new_shingles, other_shingles)
            if sim > max_sim:
                max_sim = sim
            same_structure = bool(
                structure_signature and row["structure_signature"] == structure_signature
            )
            if sim >= similarity_threshold or same_structure:
                matches.append(
                    {
                        "other_account_id": row["account_id"],
                        "other_app_slug": row["app_slug"],
                        "similarity": sim,
                        "same_structure": same_structure,
                        "other_tone": row["tone"],
                    }
                )

        return {
            "collision_detected": len(matches) > 0,
            "max_similarity": max_sim,
            "threshold": similarity_threshold,
            "matches": matches,
        }

    def record_copy_usage(
        self,
        account_id: str,
        app_slug: str,
        locale: str,
        tone: str,
        structure_signature: str,
        full_description: str,
    ) -> None:
        shingles = sorted(compute_shingles(full_description))
        text_hash = hashlib.sha256(full_description.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copy_usage (
                    account_id, app_slug, locale, tone, structure_signature,
                    text_sha256, shingles_json, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    app_slug,
                    locale,
                    tone,
                    structure_signature,
                    text_hash,
                    json.dumps(shingles, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            conn.commit()
