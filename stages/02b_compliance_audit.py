"""Stage 2B — Google Play Compliance & Rejection Prevention Engine (stages/02b_compliance_audit.py).

Performs deep pre-submission compliance auditing on:
  1. The actual signed .aab / .apk binary (even in Mode A `audio_workspace` when Stage 2 build is skipped):
     - Decodes AndroidManifest.xml (plain XML, APK binary via aapt2, or AAB ProtoXML in pure Python).
     - Verifies targetSdkVersion, android:debuggable, android:usesCleartextTraffic, android:exported.
     - Checks ELF64 16 KB page alignment (`p_align >= 16384`) on 64-bit native `.so` libraries.
     - Flags restricted & sensitive permissions (QUERY_ALL_PACKAGES, REQUEST_INSTALL_PACKAGES,
       MANAGE_EXTERNAL_STORAGE, READ_MEDIA_*, FOREGROUND_SERVICE_MEDIA_PLAYBACK, AD_ID, etc.).
     - Fingerprints embedded SDKs (AdMob, UMP, Firebase Analytics/Crashlytics/FCM, Meta, OneSignal)
       to auto-generate exact Google Play Data Safety & Ads/Families declarations.
  2. Store listing copy & screenshot headlines (Arabic-normalized + English):
     - Detects impersonation ("Official" / "التطبيق الرسمي"), prohibited downloaders ("تحميل من يوتيوب"),
       and unrelated third-party brand references with safe wording recommendations.
  3. Optional HTTPS Privacy Policy URL check (HTTP 200 & non-404 verification).

Outputs strictly inside workspace_runs/<app-slug>/<run-timestamp>/:
  - compliance_audit_report.json
  - play_console_declarations_checklist.html
  - play_console_declarations_checklist.txt
"""

from __future__ import annotations

import argparse
import html
import json
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import requests

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.config_loader import CONFIG_DIR, load_studio_config
from core.env_resolver import resolve_environment
from core.logger import get_logger
from core.models import StageRecord
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso
from core.validators import assert_path_within

# Reuse Stage 5's battle-tested Arabic normalization
import importlib

_stage5 = importlib.import_module("stages.05_copy_and_policy_guard")
normalize_arabic = _stage5.normalize_arabic

logger = get_logger("stage.02b_compliance_audit")

COMPLIANCE_RULES_PATH = CONFIG_DIR / "google_play_compliance_rules.json"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def load_compliance_rules(rules_path: Path = COMPLIANCE_RULES_PATH) -> dict[str, Any]:
    if not rules_path.exists():
        raise FileNotFoundError(f"Compliance rules file not found: {rules_path}")
    return json.loads(rules_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Pure-Python AAB ProtoXML (Android aapt.pb.XmlNode) Minimal Wire Decoder
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 64:
            raise ValueError("Invalid protobuf varint")
    raise ValueError("Truncated protobuf varint")


def _iter_pb_fields(buf: bytes):
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = _read_varint(buf, pos)
            yield field_num, wire_type, val
        elif wire_type == 1:
            val = buf[pos : pos + 8]
            pos += 8
            yield field_num, wire_type, val
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            val = buf[pos : pos + length]
            pos += length
            yield field_num, wire_type, val
        elif wire_type == 5:
            val = buf[pos : pos + 4]
            pos += 4
            yield field_num, wire_type, val
        else:
            break


def _decode_pb_compiled_item_value(item_bytes: bytes) -> str | None:
    """Decode aapt.pb.Item primitive/string value if present."""
    for fnum, wtype, val in _iter_pb_fields(item_bytes):
        if fnum == 4 and wtype == 2:  # String { value = 1 }
            for sf, sw, sv in _iter_pb_fields(val):
                if sf == 1 and sw == 2:
                    return sv.decode("utf-8", errors="ignore")
        elif fnum == 7 and wtype == 2:  # Primitive { int_decimal_value=14, boolean_value=17, ... }
            for pf, pw, pv in _iter_pb_fields(val):
                if pf in (14, 15, 16) and pw == 0:
                    return str(pv)
                if pf == 17 and pw == 0:
                    return "true" if pv else "false"
    return None


def _decode_pb_xml_attribute(attr_bytes: bytes) -> tuple[str, str]:
    name = ""
    raw_val = ""
    compiled_val = None
    for fnum, wtype, val in _iter_pb_fields(attr_bytes):
        if fnum == 2 and wtype == 2:
            name = val.decode("utf-8", errors="ignore")
        elif fnum == 3 and wtype == 2:
            raw_val = val.decode("utf-8", errors="ignore")
        elif fnum == 6 and wtype == 2:
            compiled_val = _decode_pb_compiled_item_value(val)
    return name, (raw_val if raw_val != "" else (compiled_val or ""))


def _decode_pb_xml_element(elem_bytes: bytes) -> dict[str, Any]:
    tag = ""
    attrs: dict[str, str] = {}
    children: list[dict[str, Any]] = []
    for fnum, wtype, val in _iter_pb_fields(elem_bytes):
        if fnum == 3 and wtype == 2:
            tag = val.decode("utf-8", errors="ignore")
        elif fnum == 4 and wtype == 2:
            k, v = _decode_pb_xml_attribute(val)
            if k:
                attrs[k] = v
        elif fnum == 5 and wtype == 2:
            child = _decode_pb_xml_node(val)
            if child:
                children.append(child)
    return {"tag": tag, "attrs": attrs, "children": children}


def _decode_pb_xml_node(node_bytes: bytes) -> dict[str, Any] | None:
    for fnum, wtype, val in _iter_pb_fields(node_bytes):
        if fnum == 1 and wtype == 2:
            return _decode_pb_xml_element(val)
    return None


def _elementtree_to_dict(elem: ET.Element) -> dict[str, Any]:
    tag = elem.tag.split("}")[-1]
    attrs = {k.split("}")[-1]: v for k, v in elem.attrib.items()}
    children = [_elementtree_to_dict(c) for c in list(elem)]
    return {"tag": tag, "attrs": attrs, "children": children}


def _parse_aapt2_xmltree_output(output: str) -> dict[str, Any] | None:
    """Parse `aapt2 dump xmltree` indented tree into normalized manifest node dict."""
    stack: list[tuple[int, dict[str, Any]]] = []
    root: dict[str, Any] | None = None
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if line.startswith("E:"):
            m = re.match(r"E:\s+([^\s(]+)", line)
            if not m:
                continue
            node = {"tag": m.group(1), "attrs": {}, "children": []}
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if stack:
                stack[-1][1]["children"].append(node)
            else:
                root = node
            stack.append((indent, node))
        elif line.startswith("A:") and stack:
            # Example: A: http://schemas.android.com/apk/res/android:targetSdkVersion(0x01010270)=37
            m = re.match(r"A:\s+(?:[^\s=(]*:)?([A-Za-z0-9_]+)(?:\([^)]*\))?=(.+)$", line)
            if m:
                attr_name = m.group(1)
                raw_v = m.group(2).strip()
                qm = re.match(r'^"([^"]*)"', raw_v)
                if qm:
                    val = qm.group(1)
                elif "(type 0x12)0xffffffff" in raw_v or raw_v.lower() == "true":
                    val = "true"
                elif "(type 0x12)0x0" in raw_v or raw_v.lower() == "false":
                    val = "false"
                else:
                    hex_m = re.search(r"0x([0-9a-fA-F]+)$", raw_v)
                    val = str(int(hex_m.group(1), 16)) if hex_m else raw_v
                stack[-1][1]["attrs"][attr_name] = val
    return root


def extract_manifest_tree(artifact_path: Path, aapt2_bin: str | None = None) -> tuple[dict[str, Any] | None, str]:
    """Extract normalized manifest tree from .aab or .apk (plain XML, ProtoXML, or aapt2)."""
    with zipfile.ZipFile(artifact_path, "r") as zf:
        names = set(zf.namelist())
        manifest_member = (
            "base/manifest/AndroidManifest.xml"
            if "base/manifest/AndroidManifest.xml" in names
            else ("AndroidManifest.xml" if "AndroidManifest.xml" in names else None)
        )
        if not manifest_member:
            return None, "manifest_not_found"
        raw_bytes = zf.read(manifest_member)

    # 1. Plain UTF-8 XML (useful in unpacked builds and synthetic test archives)
    stripped = raw_bytes.lstrip()
    if stripped.startswith(b"<"):
        try:
            root_el = ET.fromstring(raw_bytes.decode("utf-8", errors="ignore"))
            return _elementtree_to_dict(root_el), "plain_xml"
        except ET.ParseError:
            pass

    # 2. Binary AXML inside APK (magic 0x00080003) -> use aapt2 dump xmltree
    if raw_bytes[:4] == b"\x03\x00\x08\x00" and aapt2_bin:
        try:
            proc = subprocess.run(
                [aapt2_bin, "dump", "xmltree", str(artifact_path), "--file", manifest_member],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                parsed = _parse_aapt2_xmltree_output(proc.stdout)
                if parsed:
                    return parsed, "aapt2_xmltree"
        except (OSError, subprocess.SubprocessError):
            pass

    # 3. AAB ProtoXML (aapt.pb.XmlNode) pure-Python decode
    try:
        parsed_pb = _decode_pb_xml_node(raw_bytes)
        if parsed_pb and parsed_pb.get("tag"):
            return parsed_pb, "aab_proto_xml"
    except Exception:
        pass

    return None, "decode_failed"


def _walk_manifest_nodes(node: dict[str, Any]):
    yield node
    for child in node.get("children", []):
        yield from _walk_manifest_nodes(child)


def analyze_manifest_tree(tree: dict[str, Any] | None) -> dict[str, Any]:
    """Extract targetSdk, minSdk, debuggable, cleartext, permissions, and exported gaps."""
    summary: dict[str, Any] = {
        "parsed": tree is not None,
        "package": None,
        "target_sdk": None,
        "min_sdk": None,
        "debuggable": False,
        "uses_cleartext_traffic": False,
        "permissions": [],
        "permission_max_sdk": {},
        "components_missing_exported": [],
        "meta_data_names": [],
    }
    if not tree:
        return summary

    summary["package"] = tree.get("attrs", {}).get("package")
    perms: list[str] = []
    perm_max_sdk: dict[str, int] = {}
    missing_exported: list[dict[str, str]] = []
    meta_names: list[str] = []

    for node in _walk_manifest_nodes(tree):
        tag = node.get("tag", "")
        attrs = node.get("attrs", {})
        if tag == "uses-sdk":
            if "targetSdkVersion" in attrs:
                try:
                    summary["target_sdk"] = int(str(attrs["targetSdkVersion"]), 0)
                except ValueError:
                    pass
            if "minSdkVersion" in attrs:
                try:
                    summary["min_sdk"] = int(str(attrs["minSdkVersion"]), 0)
                except ValueError:
                    pass
        elif tag == "application":
            if str(attrs.get("debuggable", "")).lower() == "true":
                summary["debuggable"] = True
            if str(attrs.get("usesCleartextTraffic", "")).lower() == "true":
                summary["uses_cleartext_traffic"] = True
        elif tag in ("uses-permission", "uses-permission-sdk-23"):
            p_name = attrs.get("name")
            if p_name:
                perms.append(p_name)
                if "maxSdkVersion" in attrs:
                    try:
                        perm_max_sdk[p_name] = int(str(attrs["maxSdkVersion"]), 0)
                    except ValueError:
                        pass
        elif tag == "meta-data":
            m_name = attrs.get("name")
            if m_name:
                meta_names.append(m_name)
        elif tag in ("activity", "activity-alias", "service", "receiver"):
            has_intent_filter = any(c.get("tag") == "intent-filter" for c in node.get("children", []))
            if has_intent_filter and "exported" not in attrs:
                missing_exported.append({"tag": tag, "name": attrs.get("name", "<unnamed>")})

    summary["permissions"] = sorted(set(perms))
    summary["permission_max_sdk"] = perm_max_sdk
    summary["components_missing_exported"] = missing_exported
    summary["meta_data_names"] = sorted(set(meta_names))
    return summary


# ---------------------------------------------------------------------------
# Pure-Python ELF64 16 KB Native Page Alignment Check
# ---------------------------------------------------------------------------

def check_elf64_bytes_alignment(elf_bytes: bytes, required_align: int = 16384) -> dict[str, Any]:
    """Inspect ELF64 Program Headers (`PT_LOAD`) for >= 16 KB (`0x4000`) alignment."""
    if len(elf_bytes) < 64 or elf_bytes[:4] != b"\x7fELF":
        return {"is_elf64": False, "aligned_16kb": None, "min_pt_load_align": None}
    ei_class = elf_bytes[4]
    ei_data = elf_bytes[5]
    if ei_class != 2 or ei_data != 1:  # Only 64-bit little-endian (arm64-v8a / x86_64)
        return {"is_elf64": False, "aligned_16kb": None, "min_pt_load_align": None}

    e_phoff = struct.unpack_from("<Q", elf_bytes, 32)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", elf_bytes, 54)

    load_aligns: list[int] = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 56 > len(elf_bytes):
            break
        p_type = struct.unpack_from("<I", elf_bytes, off)[0]
        p_align = struct.unpack_from("<Q", elf_bytes, off + 48)[0]
        if p_type == 1:  # PT_LOAD
            load_aligns.append(p_align)

    if not load_aligns:
        return {"is_elf64": True, "aligned_16kb": True, "min_pt_load_align": None}

    min_align = min(load_aligns)
    return {
        "is_elf64": True,
        "aligned_16kb": min_align >= required_align,
        "min_pt_load_align": min_align,
    }


def inspect_artifact_archive(
    artifact_path: Path,
    rules: dict[str, Any],
    aapt2_bin: str | None = None,
) -> dict[str, Any]:
    """Perform full manifest, SDK fingerprint, and 16 KB ELF64 native alignment audit on .aab/.apk."""
    tree, decode_method = extract_manifest_tree(artifact_path, aapt2_bin=aapt2_bin)
    manifest_summary = analyze_manifest_tree(tree)

    required_align = int(rules.get("technical_manifest_rules", {}).get("native_page_alignment_bytes", 16384))
    sdk_specs = rules.get("sdk_fingerprints", {})

    detected_sdks: dict[str, dict[str, Any]] = {}
    native_libs_checked: list[dict[str, Any]] = []
    unaligned_libs: list[dict[str, Any]] = []

    with zipfile.ZipFile(artifact_path, "r") as zf:
        all_names = zf.namelist()
        # Collect searchable haystack: file names + manifest meta-data + small property/version files + dex snippets
        text_blobs: list[str] = ["\n".join(all_names), "\n".join(manifest_summary.get("meta_data_names", []))]

        for member in all_names:
            lower = member.lower()
            # 16 KB alignment check on 64-bit native libraries
            if lower.endswith(".so") and ("arm64-v8a/" in lower or "x86_64/" in lower):
                try:
                    so_bytes = zf.read(member)
                    res = check_elf64_bytes_alignment(so_bytes, required_align=required_align)
                    if res["is_elf64"]:
                        entry = {
                            "library": member,
                            "min_pt_load_align": res["min_pt_load_align"],
                            "aligned_16kb": res["aligned_16kb"],
                        }
                        native_libs_checked.append(entry)
                        if res["aligned_16kb"] is False:
                            unaligned_libs.append(entry)
                except Exception:
                    pass

            # Read metadata / properties / dex markers for SDK detection (bounded size for speed)
            if lower.endswith((".version", ".properties")) or (lower.endswith(".dex") and zf.getinfo(member).file_size <= 25 * 1024 * 1024):
                try:
                    raw = zf.read(member)
                    text_blobs.append(raw.decode("latin-1", errors="ignore"))
                except Exception:
                    pass

        combined_haystack = "\n".join(text_blobs)
        for sdk_id, spec in sdk_specs.items():
            matched_markers = [m for m in spec.get("markers", []) if m in combined_haystack]
            if matched_markers:
                detected_sdks[sdk_id] = {
                    "sdk_id": sdk_id,
                    "display_name": spec["display_name"],
                    "matched_markers": matched_markers[:4],
                    "requires_ads_declaration": bool(spec.get("requires_ads_declaration", False)),
                    "families_incompatible_without_config": bool(spec.get("families_incompatible_without_config", False)),
                    "data_safety_disclosures": spec.get("data_safety_disclosures", []),
                }

    return {
        "artifact_path": str(artifact_path),
        "decode_method": decode_method,
        "manifest": manifest_summary,
        "detected_sdks": detected_sdks,
        "native_16kb_check": {
            "checked_64bit_libs_count": len(native_libs_checked),
            "unaligned_libs": unaligned_libs,
            "all_aligned": len(unaligned_libs) == 0,
        },
    }


def check_privacy_policy_url(url: str | None, perform_http_check: bool = True) -> dict[str, Any]:
    if not url or not str(url).strip():
        return {"provided": False, "url": None, "valid_https": False, "http_status": None, "ok": False}

    cleaned = str(url).strip()
    is_https = cleaned.lower().startswith("https://")
    if not is_https:
        return {"provided": True, "url": cleaned, "valid_https": False, "http_status": None, "ok": False, "error": "Privacy Policy URL must use HTTPS."}

    if not perform_http_check:
        return {"provided": True, "url": cleaned, "valid_https": True, "http_status": None, "ok": True, "skipped_http": True}

    try:
        resp = requests.get(cleaned, timeout=8, allow_redirects=True)
        body_low = (resp.text or "").lower()[:2000]
        looks_404 = resp.status_code == 404 or ("page not found" in body_low and len(body_low) < 1200)
        ok = resp.status_code == 200 and not looks_404 and len(resp.text.strip()) >= 40
        return {
            "provided": True,
            "url": cleaned,
            "valid_https": True,
            "http_status": resp.status_code,
            "ok": ok,
            "error": None if ok else f"Privacy Policy returned HTTP {resp.status_code} or empty/404 content.",
        }
    except Exception as exc:
        return {
            "provided": True,
            "url": cleaned,
            "valid_https": True,
            "http_status": None,
            "ok": False,
            "error": f"Failed to reach Privacy Policy URL: {exc}",
        }


def _scan_text_for_ip_and_impersonation(
    texts_by_source: dict[str, str],
    rules: dict[str, Any],
    authorized_official: bool = False,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    ip_rules = rules.get("ip_and_impersonation_rules", {})

    def _matches_phrase(norm_text: str, norm_phrase: str) -> bool:
        if not norm_phrase:
            return False
        pat = r"(?<![A-Za-z0-9\u0621-\u064A])" + re.escape(norm_phrase) + r"(?![A-Za-z0-9\u0621-\u064A])"
        return bool(re.search(pat, norm_text))

    # 1. Official impersonation claims
    off_spec = ip_rules.get("official_impersonation_phrases", {})
    if not authorized_official:
        off_phrases = list(off_spec.get("en", [])) + list(off_spec.get("ar", []))
        for src_name, raw_text in texts_by_source.items():
            norm_t = normalize_arabic(raw_text)
            for phrase in off_phrases:
                if _matches_phrase(norm_t, normalize_arabic(phrase)):
                    findings.append(
                        {
                            "rule_id": "IMPERSONATION_OFFICIAL_CLAIM",
                            "severity": off_spec.get("severity", "BLOCKER"),
                            "category": "ip_and_impersonation",
                            "policy": off_spec.get("policy", "Impersonation Policy"),
                            "location": src_name,
                            "message": f"Deceptive/official impersonation phrase detected ('{phrase}') in {src_name}.",
                            "remediation": off_spec.get("remediation", ""),
                        }
                    )
                    break

    # 2. Prohibited third-party downloader claims (YouTube/TikTok/Spotify)
    dl_spec = ip_rules.get("prohibited_downloader_phrases", {})
    dl_phrases = list(dl_spec.get("en", [])) + list(dl_spec.get("ar", []))
    for src_name, raw_text in texts_by_source.items():
        norm_t = normalize_arabic(raw_text)
        for phrase in dl_phrases:
            if _matches_phrase(norm_t, normalize_arabic(phrase)):
                findings.append(
                    {
                        "rule_id": "PROHIBITED_DOWNLOADER_FUNCTIONALITY",
                        "severity": dl_spec.get("severity", "BLOCKER"),
                        "category": "ip_and_impersonation",
                        "policy": dl_spec.get("policy", "Device & Network Abuse"),
                        "location": src_name,
                        "message": f"Prohibited third-party downloader claim detected ('{phrase}') in {src_name}.",
                        "remediation": dl_spec.get("remediation", ""),
                    }
                )
                break

    # 3. Third-party brand mentions (WARNING)
    brand_spec = ip_rules.get("third_party_brand_warning_terms", {})
    for src_name, raw_text in texts_by_source.items():
        norm_t = normalize_arabic(raw_text)
        for term in brand_spec.get("terms", []):
            if _matches_phrase(norm_t, normalize_arabic(term)):
                findings.append(
                    {
                        "rule_id": "THIRD_PARTY_BRAND_REFERENCE",
                        "severity": brand_spec.get("severity", "WARNING"),
                        "category": "ip_and_impersonation",
                        "policy": brand_spec.get("policy", "Intellectual Property Policy"),
                        "location": src_name,
                        "message": f"Third-party brand '{term}' referenced in {src_name}.",
                        "remediation": brand_spec.get("remediation", ""),
                    }
                )
                break

    return findings


def _build_declarations_checklist_txt(report: dict[str, Any]) -> str:
    lines = [
        "==========================================================================",
        "PlayLaunch Studio — Google Play Console Declarations & Pre-Submit Checklist",
        f"App: {report['app_slug']} ({report['package_name']})",
        f"Readiness Score: {report['readiness_score']}/100 | BLOCKERS: {report['blocker_count']} | WARNINGS: {report['warning_count']}",
        f"Generated: {report['generated_at']}",
        "==========================================================================",
        "",
        "1. ADS DECLARATION (App Content -> Ads):",
        f"   - Contains Ads?: {'YES' if report['declarations']['contains_ads'] else 'NO (no Ad SDK detected)'}",
        f"   - UMP GDPR Consent SDK detected?: {'YES' if report['declarations']['ump_consent_detected'] else 'NO'}",
        f"   - app-ads.txt reminder: {'REQUIRED on developer website root' if report['declarations']['contains_ads'] else 'Not required unless ads are added'}",
        "",
        "2. DATA SAFETY FORM (App Content -> Data Safety):",
        f"   - Collects or shares user data?: {'YES' if report['declarations']['data_safety_rows'] else 'NO (verify any custom backend APIs)'}",
    ]
    for row in report["declarations"]["data_safety_rows"]:
        lines.append(
            f"   * [{row['category']}] {row['data_type']} | Collected={row['collected']} | Shared={row['shared']} | Purposes={', '.join(row['purposes'])}"
        )

    lines.extend(["", "3. SENSITIVE & FOREGROUND SERVICE DECLARATIONS:"])
    if report["declarations"]["permission_declarations"]:
        for item in report["declarations"]["permission_declarations"]:
            lines.append(f"   * {item['permission']} -> {item['declaration_required']}")
            lines.append(f"     Action: {item['remediation']}")
    else:
        lines.append("   * None detected (no restricted permissions found).")

    lines.extend(["", "4. FINDINGS SUMMARY (BLOCKERS / WARNINGS / INFO):"])
    for f in report["findings"]:
        lines.append(f"   [{f['severity']}] ({f['rule_id']}) {f['message']}")
        if f.get("remediation"):
            lines.append(f"       Fix: {f['remediation']}")
    return "\n".join(lines) + "\n"


def _build_declarations_checklist_html(report: dict[str, Any]) -> str:
    decl = report["declarations"]
    findings_rows = "".join(
        f"""
        <tr class="sev-{html.escape(f['severity'].lower())}">
          <td><span class="pill {html.escape(f['severity'].lower())}">{html.escape(f['severity'])}</span></td>
          <td><code>{html.escape(f['rule_id'])}</code></td>
          <td>{html.escape(f['policy'])}</td>
          <td><strong>{html.escape(f['message'])}</strong><br/><small>{html.escape(f.get('remediation', ''))}</small></td>
        </tr>
        """
        for f in report["findings"]
    ) or "<tr><td colspan='4'>No policy issues detected!</td></tr>"

    ds_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(r['category'])}</td>
          <td>{html.escape(r['data_type'])}</td>
          <td>{'YES' if r['collected'] else 'NO'}</td>
          <td>{'YES' if r['shared'] else 'NO'}</td>
          <td>{html.escape(', '.join(r['purposes']))}</td>
        </tr>
        """
        for r in decl["data_safety_rows"]
    ) or "<tr><td colspan='5'>No third-party data-collecting SDKs detected in archive.</td></tr>"

    perm_rows = "".join(
        f"""
        <li><strong><code>{html.escape(p['permission'])}</code></strong> — {html.escape(p['declaration_required'])}<br/>
        <small>{html.escape(p['remediation'])}</small></li>
        """
        for p in decl["permission_declarations"]
    ) or "<li>No restricted or foreground-service declarations required from binary scan.</li>"

    score_cls = "ok" if report["blocker_count"] == 0 and report["readiness_score"] >= 80 else ("err" if report["blocker_count"] > 0 else "warn")
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <title>Play Console Compliance & Declarations Checklist — {html.escape(report['app_slug'])}</title>
  <style>
    body {{ font-family: 'Dubai', 'Segoe UI', Tahoma, sans-serif; background: #070B14; color: #F1F5F9; margin: 0; padding: 28px; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; }}
    .card {{ background: #0F172A; border: 1px solid #1E293B; border-radius: 14px; padding: 20px; margin-bottom: 18px; }}
    .row {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .pill {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 800; font-size: 12px; }}
    .pill.blocker, .pill.err {{ background: rgba(239,68,68,0.18); color: #F87171; border: 1px solid rgba(239,68,68,0.45); }}
    .pill.warning, .pill.warn {{ background: rgba(245,158,11,0.18); color: #FBBF24; border: 1px solid rgba(245,158,11,0.45); }}
    .pill.info {{ background: rgba(56,189,248,0.16); color: #7DD3FC; border: 1px solid rgba(56,189,248,0.4); }}
    .pill.ok {{ background: rgba(34,197,94,0.18); color: #4ADE80; border: 1px solid rgba(34,197,94,0.45); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 10px; }}
    th, td {{ border-bottom: 1px solid #1E293B; padding: 10px; text-align: start; vertical-align: top; }}
    th {{ color: #94A3B8; }}
    small {{ color: #94A3B8; }}
    code {{ background: #1E293B; padding: 2px 6px; border-radius: 6px; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="row">
        <div>
          <h1 style="margin:0 0 6px 0;">ورقة إجابات وامتثال Google Play Console</h1>
          <div style="color:#94A3B8;">App: <strong>{html.escape(report['app_slug'])}</strong> (<code>{html.escape(report['package_name'])}</code>)</div>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <span class="pill {score_cls}">Readiness: {report['readiness_score']}/100</span>
          <span class="pill blocker">BLOCKERS: {report['blocker_count']}</span>
          <span class="pill warning">WARNINGS: {report['warning_count']}</span>
          <span class="pill info">INFO: {report['info_count']}</span>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>1. إقرار الإعلانات والخصوصية (Ads & Privacy Policy)</h2>
      <ul>
        <li><strong>هل يحتوي التطبيق على إعلانات (Contains Ads)؟</strong> {'نعم — YES (تم رصد SDK إعلانات)' if decl['contains_ads'] else 'لا — NO (لم يُرصد SDK إعلانات في الفحص الحالي)'}</li>
        <li><strong>حالة موافقة GDPR (Google UMP SDK):</strong> {'متوفر (UMP Detected)' if decl['ump_consent_detected'] else 'غير مرصود'}</li>
        <li><strong>ملف <code>app-ads.txt</code>:</strong> {'مطلوب نشره على جذر موقع المطور' if decl['contains_ads'] else 'غير مطلوب بدون إعلانات'}</li>
        <li><strong>رابط سياسة الخصوصية (Privacy Policy):</strong> {html.escape(str(report['privacy_policy_check'].get('url') or 'غير محدد بعد — يجب إضافته في Play Console'))}</li>
      </ul>
    </div>

    <div class="card">
      <h2>2. إجابات نموذج أمان البيانات (Data Safety Form)</h2>
      <table>
        <thead><tr><th>Category</th><th>Data Type</th><th>Collected</th><th>Shared</th><th>Purposes</th></tr></thead>
        <tbody>{ds_rows}</tbody>
      </table>
    </div>

    <div class="card">
      <h2>3. إقرارات الصلاحيات والخدمات الخلفية (Permissions & Foreground Service)</h2>
      <ul>{perm_rows}</ul>
    </div>

    <div class="card">
      <h2>4. سجل نتائج التدقيق وخطة الإصلاح (Audit Findings)</h2>
      <table>
        <thead><tr><th>Severity</th><th>Rule</th><th>Policy</th><th>Details & Fix</th></tr></thead>
        <tbody>{findings_rows}</tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def run_stage_02b(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    artifact_override: Path | None = None,
    privacy_policy_url: str | None = None,
    targets_children: bool = False,
    authorized_official: bool = False,
    skip_network_checks: bool = False,
    rules_path: Path = COMPLIANCE_RULES_PATH,
) -> tuple[Path, dict[str, Any]]:
    """Execute Stage 2B Google Play Compliance & Rejection Prevention Audit."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    state.stages.setdefault("02b_compliance_audit", StageRecord())
    manager.update_stage(state, "02b_compliance_audit", "running", message="Running Google Play Compliance & Rejection Prevention audit")

    manifest = json.loads((manager.run_dir / "intake_manifest.json").read_text(encoding="utf-8"))
    rules = load_compliance_rules(rules_path=rules_path)
    config = load_studio_config()
    env_info = resolve_environment(config=config)

    app_info = manifest.get("app", {})
    app_slug = str(app_info.get("slug") or state.app_slug)
    package_name = str(app_info.get("package_name") or "")
    locales = app_info.get("target_locales") or ["ar", "en-US"]

    # Locate artifact (.aab or .apk)
    artifact_path: Path | None = artifact_override
    if artifact_path is None:
        s2_path = manager.run_dir / "audit_and_build_report.json"
        if s2_path.exists():
            s2 = json.loads(s2_path.read_text(encoding="utf-8"))
            aab_info = s2.get("release_aab") or {}
            if aab_info.get("found") and aab_info.get("path"):
                cand = Path(aab_info["path"])
                if cand.exists():
                    artifact_path = cand
    if artifact_path is None:
        rel = manifest.get("release_artifact") or {}
        if rel.get("found") and rel.get("path"):
            cand = Path(rel["path"])
            if cand.exists():
                artifact_path = cand

    findings: list[dict[str, Any]] = []
    tech_rules = rules.get("technical_manifest_rules", {})
    perm_rules = rules.get("restricted_permissions", {})

    def add_finding(
        rule_id: str,
        severity: str,
        category: str,
        policy: str,
        message: str,
        remediation: str,
        location: str = "artifact",
    ) -> None:
        findings.append(
            {
                "rule_id": rule_id,
                "severity": severity,
                "category": category,
                "policy": policy,
                "location": location,
                "message": message,
                "remediation": remediation,
            }
        )

    binary_audit: dict[str, Any] = {
        "artifact_audited": False,
        "artifact_path": str(artifact_path) if artifact_path else None,
        "manifest": {},
        "detected_sdks": {},
        "native_16kb_check": {"checked_64bit_libs_count": 0, "unaligned_libs": [], "all_aligned": True},
    }

    permission_declarations: list[dict[str, str]] = []

    if artifact_path and artifact_path.exists():
        binary_audit = inspect_artifact_archive(
            artifact_path=artifact_path,
            rules=rules,
            aapt2_bin=env_info.aapt2_bin,
        )
        binary_audit["artifact_audited"] = True
        m_info = binary_audit["manifest"]

        if not m_info.get("parsed"):
            add_finding(
                "MANIFEST_UNREADABLE",
                "WARNING",
                "technical_manifest",
                "APK/AAB Integrity",
                f"Could not decode AndroidManifest.xml from {artifact_path.name} (method={binary_audit['decode_method']}).",
                "Verify the .aab/.apk is a valid Android build archive.",
            )
        else:
            # 1. targetSdkVersion
            target_sdk = m_info.get("target_sdk")
            min_blocker = int(tech_rules.get("min_target_sdk_blocker", 34))
            rec_sdk = int(tech_rules.get("recommended_target_sdk", 35))
            if target_sdk is not None:
                if target_sdk < min_blocker:
                    add_finding(
                        "TARGET_SDK_TOO_LOW",
                        "BLOCKER",
                        "technical_manifest",
                        "Target API Level Requirement",
                        f"targetSdkVersion={target_sdk} is below Google Play minimum ({min_blocker}). Upload will be rejected.",
                        f"Upgrade targetSdk to {rec_sdk} in build.gradle(.kts) and test behavior changes.",
                    )
                elif target_sdk < rec_sdk:
                    add_finding(
                        "TARGET_SDK_UPGRADE_RECOMMENDED",
                        "WARNING",
                        "technical_manifest",
                        "Target API Level Requirement",
                        f"targetSdkVersion={target_sdk} is below the latest recommended Google Play API level ({rec_sdk}).",
                        f"Plan upgrade to targetSdk={rec_sdk} (Android 15) ahead of the annual deadline.",
                    )

            # 2. debuggable=true
            if tech_rules.get("disallow_debuggable", True) and m_info.get("debuggable"):
                add_finding(
                    "DEBUGGABLE_ENABLED",
                    "BLOCKER",
                    "technical_manifest",
                    "Release Build Integrity",
                    "android:debuggable='true' detected in release artifact. Google Play strictly rejects debuggable builds.",
                    "Remove android:debuggable='true' and build with release signing config.",
                )

            # 3. usesCleartextTraffic=true
            if tech_rules.get("disallow_cleartext_traffic", True) and m_info.get("uses_cleartext_traffic"):
                add_finding(
                    "CLEARTEXT_TRAFFIC_ENABLED",
                    "BLOCKER",
                    "technical_manifest",
                    "Network Security Policy",
                    "android:usesCleartextTraffic='true' detected in AndroidManifest.xml.",
                    "Set android:usesCleartextTraffic='false' and enforce HTTPS across all endpoints / Network Security Config.",
                )

            # 4. Missing android:exported on intent-filter components
            if tech_rules.get("require_exported_with_intent_filter", True):
                for comp in m_info.get("components_missing_exported", []):
                    add_finding(
                        "MISSING_ANDROID_EXPORTED",
                        "BLOCKER",
                        "technical_manifest",
                        "Android 12+ Component Security",
                        f"<{comp['tag']} android:name='{comp['name']}'> has an <intent-filter> without explicit android:exported.",
                        "Add explicit android:exported='true' (only for launcher/public entry points) or 'false'.",
                    )

            # 5. Restricted & Sensitive Permissions
            perm_max_sdk = m_info.get("permission_max_sdk", {})
            for perm in m_info.get("permissions", []):
                if perm in perm_rules:
                    p_spec = perm_rules[perm]
                    sev = p_spec["severity"]
                    # Downgrade legacy storage permissions to INFO when properly scoped via maxSdkVersion
                    if perm == "android.permission.WRITE_EXTERNAL_STORAGE" and perm_max_sdk.get(perm, 99) <= 29:
                        sev = "INFO"
                    elif perm == "android.permission.READ_EXTERNAL_STORAGE" and perm_max_sdk.get(perm, 99) <= 32:
                        sev = "INFO"
                    add_finding(
                        f"PERMISSION_{perm.split('.')[-1]}",
                        sev,
                        "permissions",
                        p_spec["policy"],
                        f"Permission {perm} detected: {p_spec['reason']}",
                        p_spec["remediation"],
                        location="AndroidManifest.xml",
                    )
                    permission_declarations.append(
                        {
                            "permission": perm,
                            "severity": sev,
                            "declaration_required": p_spec["declaration_required"],
                            "remediation": p_spec["remediation"],
                        }
                    )

        # 6. Native 16 KB page alignment check
        unaligned = binary_audit["native_16kb_check"]["unaligned_libs"]
        if unaligned:
            names_preview = ", ".join(u["library"] for u in unaligned[:4])
            add_finding(
                "NATIVE_16KB_ALIGNMENT_WARNING",
                "WARNING",
                "technical_manifest",
                "Android 15+ 16 KB Memory Page Size Requirement",
                f"{len(unaligned)} 64-bit native library(ies) have PT_LOAD alignment < 16 KB (4 KB): {names_preview}.",
                "Upgrade NDK to r27+ / AGP 8.5.1+ and rebuild native dependencies with 16 KB ELF alignment.",
            )

        # 7. Ads, UMP GDPR & Families Policy cross-checks
        sdks = binary_audit["detected_sdks"]
        has_admob = "google_admob" in sdks
        has_ump = "google_ump" in sdks
        has_any_ads = any(s.get("requires_ads_declaration") for s in sdks.values())
        has_ad_id_perm = "com.google.android.gms.permission.AD_ID" in m_info.get("permissions", [])

        if has_admob and not has_ump:
            add_finding(
                "ADMOB_MISSING_UMP_CONSENT",
                "WARNING",
                "ads_and_privacy",
                "EU User Consent Policy (GDPR / UMP)",
                "Google AdMob SDK detected without Google UMP (User Messaging Platform) consent SDK.",
                "Integrate Google UMP SDK if distributing to EEA/UK/Switzerland, and publish app-ads.txt on your developer website.",
            )

        if has_any_ads:
            add_finding(
                "ADS_DECLARATION_AND_APP_ADS_TXT",
                "INFO",
                "ads_and_privacy",
                "Google Play Ads Policy",
                "Advertising SDK detected: you MUST declare 'Yes, my app contains ads' in Play Console and configure app-ads.txt.",
                "Verify Play Console -> App Content -> Ads = Yes and host app-ads.txt at your developer website root.",
            )

        if targets_children:
            if any(s.get("families_incompatible_without_config") for s in sdks.values()):
                add_finding(
                    "FAMILIES_POLICY_ADS_CONFLICT",
                    "BLOCKER",
                    "ads_and_families",
                    "Google Play Families Policy",
                    "App targets children while bundling AdMob/Meta Ads SDK without verified Families-compliant configuration.",
                    "Use only Families Self-Certified Ad SDKs, enforce tagForChildDirectedTreatment(true), and remove AD_ID permission.",
                )
            if has_ad_id_perm:
                add_finding(
                    "FAMILIES_POLICY_AD_ID_FORBIDDEN",
                    "BLOCKER",
                    "ads_and_families",
                    "Google Play Families Policy (Advertising ID)",
                    "Apps solely targeting children MUST NOT transmit or declare com.google.android.gms.permission.AD_ID.",
                    "Strip AD_ID permission using <uses-permission android:name='com.google.android.gms.permission.AD_ID' tools:node='remove'/>.",
                )
    else:
        add_finding(
            "ARTIFACT_NOT_PRESENT_FOR_BINARY_SCAN",
            "WARNING",
            "technical_manifest",
            "Pre-Release Binary Audit",
            "No signed .aab or .apk was found in this run; binary Manifest/SDK/16KB inspection was skipped (metadata & policy checks still ran).",
            "Re-run 02b_compliance_audit.py --artifact <path-to-release.aab> once the release .aab is built before final submission.",
        )

    # 8. Privacy Policy URL check
    resolved_pp_url = (
        privacy_policy_url
        or (manifest.get("store_seed") or {}).get("privacy_policy_url")
        or (manifest.get("app") or {}).get("privacy_policy_url")
    )
    pp_check = check_privacy_policy_url(resolved_pp_url, perform_http_check=not skip_network_checks)
    if not pp_check["provided"]:
        add_finding(
            "PRIVACY_POLICY_URL_MISSING",
            "WARNING",
            "data_safety_privacy",
            "Google Play Privacy Policy Requirement",
            "No Privacy Policy URL configured for automated verification.",
            "Ensure a valid, public HTTPS Privacy Policy URL is entered in Play Console -> App Content -> Privacy Policy (pass --privacy-policy-url to verify HTTP 200).",
            location="store_config",
        )
    elif not pp_check["ok"]:
        add_finding(
            "PRIVACY_POLICY_URL_INVALID",
            "BLOCKER",
            "data_safety_privacy",
            "Google Play Privacy Policy Requirement",
            f"Privacy Policy URL check failed ({pp_check.get('url')}): {pp_check.get('error')}",
            "Host your Privacy Policy over valid HTTPS returning HTTP 200 (never 404 or broken redirect).",
            location="store_config",
        )

    # 9. IP, Impersonation & Prohibited Functionality scan across app name, store copy & headlines
    texts_to_scan: dict[str, str] = {
        "intake.display_name": str(app_info.get("display_name") or ""),
    }
    meta_root = manager.run_dir / "metadata" / "android"
    for loc in locales:
        ldir = meta_root / loc
        for fname in ("title.txt", "short_description.txt", "full_description.txt"):
            fpath = ldir / fname
            if fpath.exists():
                texts_to_scan[f"{loc}/{fname}"] = fpath.read_text(encoding="utf-8")
    hl_path = manager.run_dir / "screenshot_headlines.json"
    if hl_path.exists():
        try:
            hl_raw = json.loads(hl_path.read_text(encoding="utf-8"))
            for loc, items in hl_raw.items():
                if isinstance(items, list):
                    joined = " ".join(
                        f"{i.get('headline', '')} {i.get('subtitle', '')} {i.get('badge', '')}"
                        for i in items
                        if isinstance(i, dict)
                    )
                    texts_to_scan[f"screenshot_headlines/{loc}"] = joined
        except (OSError, json.JSONDecodeError):
            pass

    findings.extend(
        _scan_text_for_ip_and_impersonation(
            texts_by_source=texts_to_scan,
            rules=rules,
            authorized_official=authorized_official,
        )
    )

    # Deduplicate Data Safety rows across detected SDKs
    ds_rows: list[dict[str, Any]] = []
    seen_ds: set[tuple[str, str]] = set()
    for sdk in binary_audit.get("detected_sdks", {}).values():
        for row in sdk.get("data_safety_disclosures", []):
            key = (row["category"], row["data_type"])
            if key not in seen_ds:
                seen_ds.add(key)
                ds_rows.append(row)

    blocker_count = sum(1 for f in findings if f["severity"] == "BLOCKER")
    warning_count = sum(1 for f in findings if f["severity"] == "WARNING")
    info_count = sum(1 for f in findings if f["severity"] == "INFO")

    # Readiness Score (0..100): each BLOCKER -35, each WARNING -8, each INFO -0
    readiness_score = max(0, 100 - (blocker_count * 35) - (warning_count * 8))

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "app_slug": app_slug,
        "package_name": package_name,
        "readiness_score": readiness_score,
        "passed": blocker_count == 0,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "targets_children": targets_children,
        "binary_audit": binary_audit,
        "privacy_policy_check": pp_check,
        "declarations": {
            "contains_ads": any(s.get("requires_ads_declaration") for s in binary_audit.get("detected_sdks", {}).values()),
            "ump_consent_detected": "google_ump" in binary_audit.get("detected_sdks", {}),
            "detected_sdks": [s["display_name"] for s in binary_audit.get("detected_sdks", {}).values()],
            "data_safety_rows": ds_rows,
            "permission_declarations": permission_declarations,
        },
        "findings": findings,
    }

    report_path = manager.write_json_artifact("compliance_audit_report.json", report)
    checklist_txt_path = assert_path_within(manager.run_dir / "play_console_declarations_checklist.txt", manager.run_dir)
    checklist_html_path = assert_path_within(manager.run_dir / "play_console_declarations_checklist.html", manager.run_dir)
    checklist_txt_path.write_text(_build_declarations_checklist_txt(report), encoding="utf-8")
    checklist_html_path.write_text(_build_declarations_checklist_html(report), encoding="utf-8")

    stage_status = "completed" if blocker_count == 0 else "failed"
    manager.update_stage(
        state,
        "02b_compliance_audit",
        stage_status,
        message=(
            f"Compliance audit complete: Readiness {readiness_score}/100 "
            f"(BLOCKERS={blocker_count}, WARNINGS={warning_count}, INFO={info_count})."
        ),
        artifacts={
            "compliance_audit_report": str(report_path),
            "play_console_checklist_html": str(checklist_html_path),
            "play_console_checklist_txt": str(checklist_txt_path),
        },
        warnings=[f["message"] for f in findings if f["severity"] in ("BLOCKER", "WARNING")][:10],
    )
    logger.info(
        "Stage 2B Compliance Audit -> score=%d/100 blockers=%d warnings=%d",
        readiness_score,
        blocker_count,
        warning_count,
    )
    return report_path, report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 2B Google Play Compliance & Rejection Prevention Engine")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--artifact", type=Path, help="Explicit path to signed .aab or .apk to audit")
    parser.add_argument("--privacy-policy-url", help="HTTPS Privacy Policy URL to verify (HTTP 200)")
    parser.add_argument("--targets-children", action="store_true", help="Enforce strict Google Play Families Policy rules")
    parser.add_argument("--authorized-official", action="store_true", help="Allow 'Official' wording if publisher is officially authorized")
    parser.add_argument("--skip-network-checks", action="store_true", help="Skip live HTTP check on privacy policy URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report_path, report = run_stage_02b(
        run_dir=args.run_dir,
        runs_root=args.runs_root,
        artifact_override=args.artifact,
        privacy_policy_url=args.privacy_policy_url,
        targets_children=args.targets_children,
        authorized_official=args.authorized_official,
        skip_network_checks=args.skip_network_checks,
    )
    print(
        json.dumps(
            {
                "status": "ok" if report["passed"] else "blockers_found",
                "readiness_score": report["readiness_score"],
                "blocker_count": report["blocker_count"],
                "warning_count": report["warning_count"],
                "info_count": report["info_count"],
                "compliance_audit_report": str(report_path),
                "checklist_html": str(args.run_dir / "play_console_declarations_checklist.html"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
