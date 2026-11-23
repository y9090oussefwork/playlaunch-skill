"""Stage 7 — Packaging, Account Isolation & Publishing (stages/07_isolated_publish.py).

Enforces:
  1. Mandatory Human Approval Gate: refuses to run unless `state.human_approved == True`
     or `--approve` is passed (except `--dry-run` simulation).
  2. Default `manual_bundle` mode: ZERO network calls to Google Play. Packages a clean,
     self-contained `READY_TO_UPLOAD/` folder strictly inside `workspace_runs/`.
  3. Optional explicit `--sync-to-audio-workspace`: ONLY copies approved store assets &
     release notes to `Youssef_Audio_Workspace/02_Developer_Accounts/...` when explicitly
     requested (never on default runs or dry-runs).
  4. Opt-in `isolated_auto_publish` mode: MUST acquire a verified `IsolatedPlaySession`
     from `security/isolation_guard.py` BEFORE importing/initializing `googleapiclient`.
     Uploads as `draft` track by default (never `production` without `--promote-production`).
     In proxy mode, binds `google_auth_httplib2.AuthorizedHttp` exclusively to the
     proxy-bound `httplib2.Http` transport (ZERO direct fallback).
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path
from typing import Any

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.config_loader import load_play_policies
from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso
from core.validators import assert_path_within, sha256_file
from security.isolation_guard import (
    DEFAULT_IP_LEDGER_DB,
    DEFAULT_LIVE_STATUS_PATH,
    IsolatedPlaySession,
    IsolationGuard,
)

logger = get_logger("stage.07_isolated_publish")


class HumanApprovalRequiredError(PermissionError):
    """Raised when Stage 7 is invoked without human approval (and not --dry-run)."""


class ComplianceBlockerError(PermissionError):
    """Raised when compliance_audit_report.json has unresolved BLOCKER findings."""


def _load_full_account_profile(
    manifest: dict[str, Any],
    explicit_account_profile: Path | None = None,
    studio_root: Path = STUDIO_ROOT,
) -> dict[str, Any]:
    acc_block = dict(manifest.get("account", {}))
    candidate = explicit_account_profile
    if candidate is None and acc_block.get("profile_path"):
        p = Path(str(acc_block["profile_path"]))
        if p.exists():
            candidate = p
    if candidate is None and acc_block.get("account_id"):
        real_p = studio_root / "profiles" / "accounts" / f"{acc_block['account_id']}.json"
        if real_p.exists():
            candidate = real_p

    if candidate and candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return acc_block


def _resolve_release_aab(run_dir: Path, manifest: dict[str, Any]) -> Path | None:
    # Check Stage 2 report first, then Stage 1 intake manifest
    s2_path = run_dir / "audit_and_build_report.json"
    if s2_path.exists():
        s2 = json.loads(s2_path.read_text(encoding="utf-8"))
        aab_info = s2.get("release_aab") or {}
        if aab_info.get("found") and aab_info.get("path"):
            p = Path(aab_info["path"])
            if p.exists():
                return p
    rel = manifest.get("release_artifact") or {}
    if rel.get("found") and rel.get("path"):
        p = Path(rel["path"])
        if p.exists():
            return p
    return None


def _collect_locale_listings(run_dir: Path, locales: list[str]) -> dict[str, dict[str, Any]]:
    meta_root = run_dir / "metadata" / "android"
    data: dict[str, dict[str, Any]] = {}
    for loc in locales:
        loc_dir = meta_root / loc
        title = (loc_dir / "title.txt").read_text(encoding="utf-8").strip() if (loc_dir / "title.txt").exists() else ""
        short_d = (
            (loc_dir / "short_description.txt").read_text(encoding="utf-8").strip()
            if (loc_dir / "short_description.txt").exists()
            else ""
        )
        full_d = (
            (loc_dir / "full_description.txt").read_text(encoding="utf-8").strip()
            if (loc_dir / "full_description.txt").exists()
            else ""
        )
        changelogs: dict[str, str] = {}
        cl_dir = loc_dir / "changelogs"
        if cl_dir.exists():
            for f in sorted(cl_dir.glob("*.txt")):
                changelogs[f.stem] = f.read_text(encoding="utf-8").strip()
        data[loc] = {
            "title": title,
            "short_description": short_d,
            "full_description": full_d,
            "changelogs": changelogs,
        }
    return data


def _build_listing_copy_txt(app_info: dict[str, Any], listings: dict[str, dict[str, Any]]) -> str:
    lines = [
        "====================================================================",
        f"PlayLaunch Studio — Manual Upload Copy Sheet",
        f"App: {app_info.get('display_name')} ({app_info.get('package_name')})",
        f"Generated: {utc_now_iso()}",
        "====================================================================",
        "",
    ]
    for loc, item in listings.items():
        lines.extend(
            [
                f"-------------------- LOCALE: {loc} --------------------",
                f"[Title] ({len(item['title'])}/30 chars):",
                item["title"],
                "",
                f"[Short Description] ({len(item['short_description'])}/80 chars):",
                item["short_description"],
                "",
                f"[Full Description] ({len(item['full_description'])}/4000 chars):",
                item["full_description"],
                "",
            ]
        )
        for vc, cl in item["changelogs"].items():
            lines.extend([f"[Changelog v{vc}] ({len(cl)}/500 chars):", cl, ""])
    return "\n".join(lines) + "\n"


def _build_listing_copy_html(
    app_info: dict[str, Any],
    listings: dict[str, dict[str, Any]],
    has_icon: bool,
    locale_assets: dict[str, dict[str, Any]],
) -> str:
    sections: list[str] = []
    for loc, item in listings.items():
        rtl = loc.lower().startswith(("ar", "fa", "ur", "he"))
        dir_attr = "rtl" if rtl else "ltr"
        assets = locale_assets.get(loc, {})
        fg_rel = assets.get("feature_graphic_rel")
        shots_rel = assets.get("screenshots_rel", [])

        fg_html = (
            f'<div class="asset-block"><h4>Feature Graphic (1024×500)</h4>'
            f'<img class="fg-preview" src="{html.escape(fg_rel)}" alt="Feature Graphic" /></div>'
            if fg_rel
            else ""
        )
        shots_html = ""
        if shots_rel:
            imgs = "".join(
                f'<img class="shot-preview" src="{html.escape(s)}" alt="Screenshot" />' for s in shots_rel
            )
            shots_html = f'<div class="asset-block"><h4>Phone Screenshots (1080×1920)</h4><div class="shots-grid">{imgs}</div></div>'

        changelogs_html = ""
        for vc, cl in item["changelogs"].items():
            cid = f"cl_{loc}_{vc}".replace("-", "_")
            changelogs_html += f"""
            <div class="field">
              <div class="field-hdr">
                <span>Changelog v{html.escape(vc)} ({len(cl)}/500)</span>
                <button onclick="copyById('{cid}')">Copy / نسخ</button>
              </div>
              <textarea id="{cid}" dir="{dir_attr}" rows="3" readonly>{html.escape(cl)}</textarea>
            </div>
            """

        tid = f"title_{loc}".replace("-", "_")
        sid = f"short_{loc}".replace("-", "_")
        fid = f"full_{loc}".replace("-", "_")
        sections.append(
            f"""
            <section class="locale-card">
              <h2>Locale: {html.escape(loc)}</h2>
              <div class="field">
                <div class="field-hdr"><span>Title ({len(item['title'])}/30)</span><button onclick="copyById('{tid}')">Copy / نسخ</button></div>
                <textarea id="{tid}" dir="{dir_attr}" rows="1" readonly>{html.escape(item['title'])}</textarea>
              </div>
              <div class="field">
                <div class="field-hdr"><span>Short Description ({len(item['short_description'])}/80)</span><button onclick="copyById('{sid}')">Copy / نسخ</button></div>
                <textarea id="{sid}" dir="{dir_attr}" rows="2" readonly>{html.escape(item['short_description'])}</textarea>
              </div>
              <div class="field">
                <div class="field-hdr"><span>Full Description ({len(item['full_description'])}/4000)</span><button onclick="copyById('{fid}')">Copy / نسخ</button></div>
                <textarea id="{fid}" dir="{dir_attr}" rows="10" readonly>{html.escape(item['full_description'])}</textarea>
              </div>
              {changelogs_html}
              {fg_html}
              {shots_html}
            </section>
            """
        )

    icon_tag = (
        '<img class="app-icon" src="assets/icon_512.png" alt="App Icon" />' if has_icon else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>PlayLaunch Studio — Ready To Upload ({html.escape(str(app_info.get('slug', '')))})</title>
  <style>
    body {{ font-family: 'Segoe UI', Tahoma, sans-serif; background: #0B1120; color: #F1F5F9; margin: 0; padding: 28px; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; }}
    .hero {{ display: flex; align-items: center; gap: 20px; padding: 24px; border-radius: 16px; background: #111827; border: 1px solid #1F2937; margin-bottom: 24px; }}
    .app-icon {{ width: 84px; height: 84px; border-radius: 18px; border: 2px solid #22C55E; }}
    .locale-card {{ background: #111827; border: 1px solid #1F2937; border-radius: 16px; padding: 24px; margin-bottom: 24px; }}
    .field {{ margin-bottom: 16px; }}
    .field-hdr {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; font-size: 14px; color: #94A3B8; }}
    button {{ background: #22C55E; color: #052E16; border: none; border-radius: 8px; padding: 6px 14px; font-weight: 700; cursor: pointer; }}
    textarea {{ width: 100%; box-sizing: border-box; background: #0F172A; color: #F8FAFC; border: 1px solid #334155; border-radius: 10px; padding: 12px; font-size: 15px; font-family: inherit; }}
    .fg-preview {{ max-width: 520px; width: 100%; border-radius: 10px; border: 1px solid #334155; }}
    .shots-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 14px; }}
    .shot-preview {{ width: 100%; border-radius: 10px; border: 1px solid #334155; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      {icon_tag}
      <div>
        <h1 style="margin:0 0 6px 0;">{html.escape(str(app_info.get('display_name', '')))}</h1>
        <div style="color:#94A3B8;">Package: <code>{html.escape(str(app_info.get('package_name', '')))}</code> • Mode: Manual Isolated Transfer Bundle</div>
      </div>
    </div>
    {"".join(sections)}
  </div>
  <script>
    function copyById(id) {{
      const el = document.getElementById(id);
      if (!el) return;
      el.select();
      navigator.clipboard ? navigator.clipboard.writeText(el.value) : document.execCommand('copy');
    }}
  </script>
</body>
</html>
"""


def build_manual_upload_bundle(
    run_dir: Path,
    manifest: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Package READY_TO_UPLOAD/ strictly inside run_dir with zero Play Console network calls."""
    bundle_dir = run_dir / "READY_TO_UPLOAD"
    assert_path_within(bundle_dir, run_dir)

    app_info = manifest.get("app", {})
    locales = app_info.get("target_locales") or ["ar", "en-US"]
    listings = _collect_locale_listings(run_dir, locales)
    aab_path = _resolve_release_aab(run_dir, manifest)

    visuals_manifest_path = run_dir / "visuals_manifest.json"
    visuals_manifest = (
        json.loads(visuals_manifest_path.read_text(encoding="utf-8"))
        if visuals_manifest_path.exists()
        else {}
    )

    if dry_run:
        return {
            "dry_run": True,
            "bundle_dir": str(bundle_dir),
            "locales": locales,
            "aab_included": aab_path is not None,
            "aab_source": str(aab_path) if aab_path else None,
            "network_calls": 0,
        }

    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy Fastlane metadata directory
    src_meta = run_dir / "metadata" / "android"
    if src_meta.exists():
        shutil.copytree(src_meta, bundle_dir / "metadata" / "android", dirs_exist_ok=True)

    # 2. Copy visual assets cleanly into assets/
    assets_dir = bundle_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    has_icon = False
    icon_src = run_dir / "visuals" / "icon_512.png"
    if icon_src.exists():
        shutil.copy2(icon_src, assets_dir / "icon_512.png")
        has_icon = True

    locale_assets: dict[str, dict[str, Any]] = {}
    for loc in locales:
        loc_assets_dir = assets_dir / loc
        loc_shots_dir = loc_assets_dir / "screenshots"
        loc_shots_dir.mkdir(parents=True, exist_ok=True)
        fg_rel = None
        shots_rel: list[str] = []

        fg_src = run_dir / "visuals" / "feature_graphic" / loc / "feature_graphic.png"
        if fg_src.exists():
            shutil.copy2(fg_src, loc_assets_dir / "feature_graphic.png")
            fg_rel = f"assets/{loc}/feature_graphic.png"

        shots_src_dir = run_dir / "visuals" / "screenshots" / loc
        if shots_src_dir.exists():
            for shot in sorted(shots_src_dir.glob("*.png")):
                shutil.copy2(shot, loc_shots_dir / shot.name)
                shots_rel.append(f"assets/{loc}/screenshots/{shot.name}")

        locale_assets[loc] = {"feature_graphic_rel": fg_rel, "screenshots_rel": shots_rel}

    # 3. Copy release .aab if available
    aab_copied_rel = None
    if aab_path and aab_path.exists():
        aab_out_dir = bundle_dir / "app_bundle"
        aab_out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(aab_path, aab_out_dir / aab_path.name)
        aab_copied_rel = f"app_bundle/{aab_path.name}"

    # 4. Generate copy-paste sheets (TXT + HTML), checklist, and include Compliance Audit outputs if present
    for compliance_file in (
        "compliance_audit_report.json",
        "play_console_declarations_checklist.html",
        "play_console_declarations_checklist.txt",
    ):
        src_c = run_dir / compliance_file
        if src_c.exists():
            shutil.copy2(src_c, bundle_dir / compliance_file)

    (bundle_dir / "listing_copy.txt").write_text(
        _build_listing_copy_txt(app_info, listings), encoding="utf-8"
    )
    (bundle_dir / "listing_copy.html").write_text(
        _build_listing_copy_html(app_info, listings, has_icon=has_icon, locale_assets=locale_assets),
        encoding="utf-8",
    )
    (bundle_dir / "README_UPLOAD_STEPS.txt").write_text(
        (
            "PlayLaunch Studio — Isolated Manual Upload Checklist\n"
            "====================================================\n"
            "1. Transfer this READY_TO_UPLOAD folder to the isolated client device/network for this developer account.\n"
            "2. Open listing_copy.html in a local browser for one-click Copy buttons (or use listing_copy.txt).\n"
            "3. Upload assets/icon_512.png (512x512), assets/<locale>/feature_graphic.png (1024x500), and screenshots (1080x1920).\n"
            "4. Upload the signed release .aab from app_bundle/ into a Draft/Internal/Production release.\n"
            "5. Fastlane-compatible metadata is also preserved under metadata/android/<locale>/.\n"
        ),
        encoding="utf-8",
    )

    # 5. Checksum manifest of every file inside READY_TO_UPLOAD/
    files_manifest: list[dict[str, Any]] = []
    for p in sorted(bundle_dir.rglob("*")):
        if p.is_file():
            files_manifest.append(
                {
                    "relative_path": str(p.relative_to(bundle_dir)).replace("\\", "/"),
                    "size_bytes": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
            )
    manifest_payload = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "app_slug": app_info.get("slug"),
        "package_name": app_info.get("package_name"),
        "locales": locales,
        "aab_included": aab_copied_rel is not None,
        "aab_relative_path": aab_copied_rel,
        "visual_template": (visuals_manifest.get("visual_style") or {}).get("template_id"),
        "files_count": len(files_manifest),
        "files": files_manifest,
    }
    (bundle_dir / "bundle_manifest.json").write_text(
        json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return {
        "dry_run": False,
        "bundle_dir": str(bundle_dir),
        "listing_copy_txt": str(bundle_dir / "listing_copy.txt"),
        "listing_copy_html": str(bundle_dir / "listing_copy.html"),
        "bundle_manifest": str(bundle_dir / "bundle_manifest.json"),
        "aab_included": aab_copied_rel is not None,
        "files_count": len(files_manifest),
        "network_calls": 0,
    }


def sync_approved_assets_to_audio_workspace(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Explicit opt-in sync to Youssef_Audio_Workspace/02_Developer_Accounts/ (NEVER called by default)."""
    if manifest.get("mode") != "audio_workspace":
        return {"synced": False, "reason": "Not an audio_workspace run."}
    targets = (manifest.get("audio_workspace_refs") or {}).get("planned_export_targets") or {}
    store_assets_dir = Path(targets["store_assets_dir"])
    release_notes_dir = Path(targets["release_notes_dir"])

    store_assets_dir.mkdir(parents=True, exist_ok=True)
    release_notes_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    ready_dir = run_dir / "READY_TO_UPLOAD"
    if (ready_dir / "assets").exists():
        shutil.copytree(ready_dir / "assets", store_assets_dir / "assets", dirs_exist_ok=True)
        copied.append(str(store_assets_dir / "assets"))
    for copy_sheet in ("listing_copy.txt", "listing_copy.html"):
        src = ready_dir / copy_sheet
        if src.exists():
            shutil.copy2(src, store_assets_dir / copy_sheet)
            copied.append(str(store_assets_dir / copy_sheet))

    locales = (manifest.get("app") or {}).get("target_locales") or []
    for loc in locales:
        cl_dir = run_dir / "metadata" / "android" / loc / "changelogs"
        if cl_dir.exists():
            dst_cl = release_notes_dir / loc
            dst_cl.mkdir(parents=True, exist_ok=True)
            for f in cl_dir.glob("*.txt"):
                shutil.copy2(f, dst_cl / f.name)
                copied.append(str(dst_cl / f.name))

    return {
        "synced": True,
        "store_assets_dir": str(store_assets_dir),
        "release_notes_dir": str(release_notes_dir),
        "copied_paths": copied,
    }


def _build_authorized_play_http(session: IsolatedPlaySession, scopes: list[str]) -> Any:
    """Create `google_auth_httplib2.AuthorizedHttp` strictly routed through `session.http_transport`."""
    import google_auth_httplib2
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        session.key_file_path, scopes=scopes
    )
    # In dedicated_network proxy mode, session.http_transport is strictly proxy-bound (ZERO direct fallback)
    return google_auth_httplib2.AuthorizedHttp(credentials, http=session.http_transport)


def execute_isolated_auto_publish(
    run_dir: Path,
    manifest: dict[str, Any],
    account_profile: dict[str, Any],
    guard: IsolationGuard,
    dry_run: bool = False,
    promote_production: bool = False,
    check_vitals: bool = False,
    wait_for_router_reboot: bool = True,
) -> dict[str, Any]:
    """Execute Strictly-Isolated Google Play API publish (draft by default) or --dry-run simulation."""
    app_info = manifest.get("app", {})
    package_name = str(app_info["package_name"])
    locales = app_info.get("target_locales") or ["ar", "en-US"]
    listings = _collect_locale_listings(run_dir, locales)
    aab_path = _resolve_release_aab(run_dir, manifest)

    target_track = "production" if promote_production else str(account_profile.get("default_track") or "draft")
    if not promote_production and target_track == "production":
        # Safety invariant: never promote live production without explicit --promote-production flag
        target_track = "draft"

    # HARD SECURITY GATE: MUST acquire verified session BEFORE importing googleapiclient!
    action_type = "play_api_publish_and_vitals" if check_vitals else "play_api_publish"
    session = guard.acquire_verified_session(
        account_profile=account_profile,
        action_type=action_type,
        dry_run=dry_run,
        wait_for_router_reboot=wait_for_router_reboot,
    )

    api_plan = {
        "package_name": package_name,
        "target_track": target_track,
        "release_status": "completed" if promote_production else "draft",
        "aab_to_upload": str(aab_path) if aab_path else None,
        "locales_to_update": list(listings.keys()),
        "check_vitals": check_vitals,
        "proxy_enforced": session.proxy_url_redacted is not None,
    }

    if dry_run:
        return {
            "dry_run": True,
            "isolation_session": session.to_safe_dict(),
            "api_plan": api_plan,
            "play_api_calls_executed": 0,
            "message": "Dry-run passed Isolation Guard and built Google Play upload plan with zero Play API calls.",
        }

    # Only import googleapiclient AFTER IsolationGuard succeeded
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    publisher_http = _build_authorized_play_http(
        session, scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    service = build("androidpublisher", "v3", http=publisher_http, cache_discovery=False)

    edit = service.edits().insert(body={}, packageName=package_name).execute()
    edit_id = edit["id"]
    uploaded_version_code = None

    if aab_path and aab_path.exists():
        media = MediaFileUpload(str(aab_path), mimetype="application/octet-stream", resumable=True)
        bundle_res = service.edits().bundles().upload(
            packageName=package_name, editId=edit_id, media_body=media
        ).execute()
        uploaded_version_code = bundle_res.get("versionCode")

        release_notes = []
        for loc, item in listings.items():
            if item["changelogs"]:
                latest_note = list(item["changelogs"].values())[-1]
                release_notes.append({"language": loc, "text": latest_note[:500]})

        track_body = {
            "track": "production" if promote_production else "internal",
            "releases": [
                {
                    "versionCodes": [str(uploaded_version_code)],
                    "status": "completed" if promote_production else "draft",
                    "releaseNotes": release_notes,
                }
            ],
        }
        service.edits().tracks().update(
            packageName=package_name,
            editId=edit_id,
            track=track_body["track"],
            body=track_body,
        ).execute()

    for loc, item in listings.items():
        service.edits().listings().update(
            packageName=package_name,
            editId=edit_id,
            language=loc,
            body={
                "language": loc,
                "title": item["title"],
                "shortDescription": item["short_description"],
                "fullDescription": item["full_description"],
            },
        ).execute()

    commit_res = service.edits().commit(packageName=package_name, editId=edit_id).execute()

    vitals_result = None
    if check_vitals:
        policies = load_play_policies()
        reporting_http = _build_authorized_play_http(
            session, scopes=["https://www.googleapis.com/auth/playdeveloperreporting"]
        )
        rep_service = build("playdeveloperreporting", "v1beta1", http=reporting_http, cache_discovery=False)
        crash_res = rep_service.vitals().crashrate().query(
            name=f"apps/{package_name}/crashRateMetricSet",
            body={"metrics": ["crashRate"], "pageSize": 7},
        ).execute()
        anr_res = rep_service.vitals().anrrate().query(
            name=f"apps/{package_name}/anrRateMetricSet",
            body={"metrics": ["anrRate"], "pageSize": 7},
        ).execute()
        vitals_result = {
            "thresholds": policies.get("vitals_thresholds", {}),
            "crash_rate_response": crash_res,
            "anr_rate_response": anr_res,
        }

    return {
        "dry_run": False,
        "isolation_session": session.to_safe_dict(),
        "edit_id": edit_id,
        "uploaded_version_code": uploaded_version_code,
        "commit_result": commit_res,
        "vitals_result": vitals_result,
        "api_plan": api_plan,
    }


def run_stage_07(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    approve: bool = False,
    dry_run: bool = False,
    publish_mode_override: str | None = None,
    promote_production: bool = False,
    check_vitals: bool = False,
    sync_to_audio_workspace: bool = False,
    acknowledge_compliance_risks: str | None = None,
    account_profile_path: Path | None = None,
    ledger_db_path: Path = DEFAULT_IP_LEDGER_DB,
    live_status_path: Path = DEFAULT_LIVE_STATUS_PATH,
    guard_override: IsolationGuard | None = None,
    wait_for_router_reboot: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Execute Stage 7 Packaging / Strictly-Isolated Publishing."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    manifest = json.loads((manager.run_dir / "intake_manifest.json").read_text(encoding="utf-8"))

    # Mandatory Human Approval Gate
    if not dry_run:
        if approve:
            state.human_approved = True
            manager.save(state)
        if not state.human_approved:
            raise HumanApprovalRequiredError(
                "Mandatory Human Approval Gate: Review generated preview files "
                "(listing_copy.html, play_console_declarations_checklist.html, and visuals/) "
                "with the user first, then pass --approve ONLY with explicit user confirmation. "
                "Use --dry-run to simulate without approval."
            )

    # Mandatory Google Play Compliance Blocker Gate (Stage 2B)
    compliance_ack_record = None
    compliance_path = manager.run_dir / "compliance_audit_report.json"
    if compliance_path.exists():
        comp_data = json.loads(compliance_path.read_text(encoding="utf-8"))
        blockers = [f for f in comp_data.get("findings", []) if f.get("severity") == "BLOCKER"]
        if blockers and not dry_run:
            ack_reason = (acknowledge_compliance_risks or "").strip()
            if not ack_reason:
                rule_summary = ", ".join(b.get("rule_id", "BLOCKER") for b in blockers[:6])
                raise ComplianceBlockerError(
                    f"Compliance Blocker Gate: {len(blockers)} unresolved Google Play BLOCKER(s) detected "
                    f"[{rule_summary}]. Fix them and re-run 02b_compliance_audit.py, or pass explicit "
                    "--acknowledge-compliance-risks \"<reason>\" to override consciously."
                )
            compliance_ack_record = {
                "acknowledged_at": utc_now_iso(),
                "reason": ack_reason,
                "blocker_rule_ids": [b.get("rule_id") for b in blockers],
            }

    account_profile = _load_full_account_profile(
        manifest=manifest,
        explicit_account_profile=account_profile_path,
    )
    publish_mode = (
        publish_mode_override
        or str(account_profile.get("publish_mode") or "manual_bundle").strip().lower()
    )
    if publish_mode not in ("manual_bundle", "isolated_auto_publish"):
        raise ValueError(f"Invalid publish_mode '{publish_mode}'.")

    if not dry_run:
        manager.update_stage(
            state,
            "07_isolated_publish",
            "running",
            message=f"Executing Stage 7 ({publish_mode})",
        )

    sync_result = None
    if publish_mode == "manual_bundle":
        # Always ZERO network calls to Google Play
        result_body = build_manual_upload_bundle(
            run_dir=manager.run_dir,
            manifest=manifest,
            dry_run=dry_run,
        )
        if sync_to_audio_workspace and not dry_run:
            sync_result = sync_approved_assets_to_audio_workspace(manager.run_dir, manifest)
    else:
        guard = guard_override or IsolationGuard(
            ledger_db_path=ledger_db_path,
            live_status_path=live_status_path,
        )
        result_body = execute_isolated_auto_publish(
            run_dir=manager.run_dir,
            manifest=manifest,
            account_profile=account_profile,
            guard=guard,
            dry_run=dry_run,
            promote_production=promote_production,
            check_vitals=check_vitals,
            wait_for_router_reboot=wait_for_router_reboot,
        )
        if sync_to_audio_workspace and not dry_run:
            # Also package manual assets locally if syncing to audio workspace
            build_manual_upload_bundle(run_dir=manager.run_dir, manifest=manifest, dry_run=False)
            sync_result = sync_approved_assets_to_audio_workspace(manager.run_dir, manifest)

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "app_slug": manifest["app"]["slug"],
        "package_name": manifest["app"]["package_name"],
        "publish_mode": publish_mode,
        "dry_run": dry_run,
        "human_approved": state.human_approved,
        "compliance_risk_acknowledgement": compliance_ack_record,
        "sync_to_audio_workspace": sync_result,
        "result": result_body,
    }

    report_filename = "publish_dry_run_report.json" if dry_run else "publish_report.json"
    report_path = manager.write_json_artifact(report_filename, report)

    if not dry_run:
        artifacts = {"publish_report": str(report_path)}
        if publish_mode == "manual_bundle" and result_body.get("bundle_dir"):
            artifacts["ready_to_upload_dir"] = str(result_body["bundle_dir"])
        manager.update_stage(
            state,
            "07_isolated_publish",
            "completed",
            message=f"Stage 7 completed ({publish_mode}).",
            artifacts=artifacts,
        )

    return report_path, report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 7 Packaging & Strictly-Isolated Publishing")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--approve", action="store_true", help="Explicit human approval to unlock Stage 7")
    parser.add_argument("--dry-run", action="store_true", help="Simulate checks & payload without contacting Google Play")
    parser.add_argument("--publish-mode", choices=("manual_bundle", "isolated_auto_publish"))
    parser.add_argument("--promote-production", action="store_true", help="Promote live production (default is draft)")
    parser.add_argument("--check-vitals", action="store_true", help="Also query Android Vitals via Isolation Guard")
    parser.add_argument(
        "--sync-to-audio-workspace",
        action="store_true",
        help="Explicit opt-in: mirror approved store assets to Youssef_Audio_Workspace/02_Developer_Accounts/",
    )
    parser.add_argument(
        "--acknowledge-compliance-risks",
        default="",
        help="Explicit human override reason if proceeding despite compliance BLOCKER findings",
    )
    parser.add_argument("--account-profile", type=Path, help="Optional explicit developer account JSON path")
    parser.add_argument("--no-wait-reboot", action="store_true", help="Fail immediately instead of polling router reboot")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        report_path, report = run_stage_07(
            run_dir=args.run_dir,
            runs_root=args.runs_root,
            approve=args.approve,
            dry_run=args.dry_run,
            publish_mode_override=args.publish_mode,
            promote_production=args.promote_production,
            check_vitals=args.check_vitals,
            sync_to_audio_workspace=args.sync_to_audio_workspace,
            acknowledge_compliance_risks=args.acknowledge_compliance_risks or None,
            account_profile_path=args.account_profile,
            wait_for_router_reboot=not args.no_wait_reboot,
        )
    except HumanApprovalRequiredError as exc:
        print(json.dumps({"status": "awaiting_human_approval", "error": str(exc)}, indent=2, ensure_ascii=False))
        return 2
    except ComplianceBlockerError as exc:
        print(json.dumps({"status": "compliance_blocked", "error": str(exc)}, indent=2, ensure_ascii=False))
        return 3
    except Exception as exc:
        print(json.dumps({"status": "blocked_or_failed", "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "publish_mode": report["publish_mode"],
                "dry_run": report["dry_run"],
                "report": str(report_path),
                "ready_to_upload_dir": report["result"].get("bundle_dir"),
                "sync_to_audio_workspace": bool(report.get("sync_to_audio_workspace")),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
