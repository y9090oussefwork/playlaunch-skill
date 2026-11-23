"""Arabic RTL & English LTR HTML/CSS Headless Browser Renderer (stages/renderers/rtl_html_renderer.py).

Clean fallback renderer used when rendering Arabic RTL store visuals or when
vendors/open-screenshot-generator CLI is not prebuilt. Uses local Windows Chrome/Edge
headless Chromium (HarfBuzz native Arabic shaping) with zero modifications to vendors/.
"""

from __future__ import annotations

import base64
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

RTL_PREFIXES = ("ar", "fa", "ur", "he")

BROWSER_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)

# Distinct visual palettes & CSS layout modifiers per anti-repetition template_id
TEMPLATE_THEMES: dict[str, dict[str, str]] = {
    "editorial_spotlight": {
        "bg": "linear-gradient(155deg, #090D16 0%, #111827 52%, #0F291E 100%)",
        "fg": "#F8FAFC",
        "sub_fg": "#CBD5E1",
        "card_bg": "rgba(15, 23, 42, 0.78)",
        "phone_transform": "perspective(1200px) rotateY(-6deg) rotateZ(1.5deg)",
        "badge_bg": "rgba(255,255,255,0.12)",
        "layout_mode": "top_headline",
    },
    "aurora_mesh_card": {
        "bg": "radial-gradient(circle at 20% 18%, #1E1B4B 0%, #0F172A 48%, #042F2E 100%)",
        "fg": "#FFFFFF",
        "sub_fg": "#D1D5DB",
        "card_bg": "rgba(30, 41, 59, 0.72)",
        "phone_transform": "none",
        "badge_bg": "rgba(255,255,255,0.14)",
        "layout_mode": "bottom_headline",
    },
    "minimal_clean_split": {
        "bg": "linear-gradient(160deg, #F8FAFC 0%, #EFF6FF 55%, #E2E8F0 100%)",
        "fg": "#0F172A",
        "sub_fg": "#475569",
        "card_bg": "rgba(255, 255, 255, 0.90)",
        "phone_transform": "none",
        "badge_bg": "rgba(15, 23, 42, 0.08)",
        "layout_mode": "top_headline",
    },
    "neon_studio_wave": {
        "bg": "linear-gradient(145deg, #05050A 0%, #160A2C 50%, #091B2A 100%)",
        "fg": "#F9FAFB",
        "sub_fg": "#D8B4FE",
        "card_bg": "rgba(17, 12, 34, 0.85)",
        "phone_transform": "perspective(1200px) rotateY(5deg) rotateZ(-1deg)",
        "badge_bg": "rgba(255,255,255,0.12)",
        "layout_mode": "top_headline",
    },
    "geometric_showcase": {
        "bg": "linear-gradient(135deg, #111318 0%, #1E2430 58%, #17202A 100%)",
        "fg": "#F1F5F9",
        "sub_fg": "#94A3B8",
        "card_bg": "rgba(30, 36, 48, 0.88)",
        "phone_transform": "translateY(16px)",
        "badge_bg": "rgba(255,255,255,0.10)",
        "layout_mode": "top_headline",
    },
}


def is_rtl_locale(locale: str) -> bool:
    code = (locale or "").strip().lower().split("-")[0].split("_")[0]
    return code in RTL_PREFIXES


def find_local_chromium_browser() -> Path | None:
    for cand in BROWSER_CANDIDATES:
        if cand.exists() and cand.is_file():
            return cand
    for name in ("chrome", "msedge", "chromium"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _file_to_data_uri(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else ("image/webp" if suffix == ".webp" else "image/jpeg")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _sanitize_hex_color(color: str | None, default: str = "#22C55E") -> str:
    raw = (color or "").strip()
    if len(raw) in (4, 7) and raw.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in raw[1:]):
        return raw
    return default


def build_screenshot_html(
    locale: str,
    template_id: str,
    font_ar: str,
    font_en: str,
    accent_color: str,
    app_name: str,
    headline: str,
    subtitle: str,
    badge: str,
    raw_screenshot_path: Path | None = None,
    icon_path: Path | None = None,
    sample_tracks: list[str] | None = None,
    slide_index: int = 1,
) -> str:
    rtl = is_rtl_locale(locale)
    direction = "rtl" if rtl else "ltr"
    theme = TEMPLATE_THEMES.get(template_id, TEMPLATE_THEMES["editorial_spotlight"])
    accent = _sanitize_hex_color(accent_color)
    font_family = font_ar if rtl else font_en
    raw_uri = _file_to_data_uri(raw_screenshot_path)
    icon_uri = _file_to_data_uri(icon_path)

    safe_app = html.escape(app_name or "PlayLaunch App")
    safe_headline = html.escape(headline or app_name)
    safe_subtitle = html.escape(subtitle or "")
    safe_badge = html.escape(badge or ("يعمل بدون إنترنت" if rtl else "Offline Ready"))

    if raw_uri:
        phone_screen_html = f'<img class="raw-shot" src="{raw_uri}" alt="App Screenshot" />'
    else:
        # Clean, realistic dark audio/app UI mockup inside the device frame when Stage 3 raw shots are absent
        tracks = sample_tracks or (
            ["قائمة التشغيل المميزة", "تشغيل فوري بدون إنترنت", "جودة صوت نقية", "تشغيل في الخلفية"]
            if rtl
            else ["Featured Offline Mix", "Instant Local Playback", "Studio Audio Clarity", "Background Controls"]
        )
        rows_html = "\n".join(
            f"""
            <div class="mock-row {'active' if i == (slide_index - 1) % max(len(tracks[:4]), 1) else ''}">
              <div class="mock-dot"></div>
              <div class="mock-meta">
                <div class="mock-title">{html.escape(t[:36])}</div>
                <div class="mock-sub">{safe_app}</div>
              </div>
              <div class="mock-time">03:{15 + i * 11:02d}</div>
            </div>
            """
            for i, t in enumerate(tracks[:4])
        )
        icon_block = (
            f'<img class="mock-cover" src="{icon_uri}" alt="icon" />'
            if icon_uri
            else f'<div class="mock-cover mock-cover-fallback">{html.escape(app_name[:1].upper() or "A")}</div>'
        )
        phone_screen_html = f"""
        <div class="mock-ui">
          <div class="mock-statusbar">
            <span>9:41</span>
            <span>5G • 100%</span>
          </div>
          <div class="mock-hero">
            {icon_block}
            <div class="mock-app-title">{safe_app}</div>
            <div class="mock-pill">{'متاح بدون نت • جودة عالية' if rtl else 'Offline Ready • Crisp Audio'}</div>
          </div>
          <div class="mock-list">{rows_html}</div>
          <div class="mock-player">
            <div class="mock-progress"><div class="mock-progress-fill"></div></div>
            <div class="mock-controls"><span>⏮</span><span class="play-btn">▶</span><span>⏭</span></div>
          </div>
        </div>
        """

    headline_block = f"""
    <div class="copy-zone">
      <div class="badge"><span class="badge-dot"></span>{safe_badge}</div>
      <h1 class="headline">{safe_headline}</h1>
      {f'<p class="subtitle">{safe_subtitle}</p>' if safe_subtitle else ''}
    </div>
    """
    device_block = f"""
    <div class="device-zone">
      <div class="device-glow"></div>
      <div class="phone-frame">
        <div class="phone-notch"></div>
        <div class="phone-screen">{phone_screen_html}</div>
      </div>
    </div>
    """
    ordered_sections = (
        f"{device_block}\n{headline_block}"
        if theme["layout_mode"] == "bottom_headline"
        else f"{headline_block}\n{device_block}"
    )

    return f"""<!DOCTYPE html>
<html lang="{html.escape(locale)}" dir="{direction}">
<head>
  <meta charset="utf-8" />
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
      width: 1080px;
      height: 1920px;
      overflow: hidden;
      font-family: {font_family};
      background: {theme['bg']};
      color: {theme['fg']};
      -webkit-font-smoothing: antialiased;
    }}
    .canvas {{
      position: relative;
      width: 1080px;
      height: 1920px;
      padding: 88px 72px 72px 72px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      align-items: center;
      text-align: center;
    }}
    .orb-1 {{
      position: absolute;
      width: 720px;
      height: 720px;
      border-radius: 50%;
      background: {accent};
      opacity: 0.14;
      filter: blur(110px);
      top: -160px;
      {'left' if rtl else 'right'}: -180px;
      pointer-events: none;
    }}
    .orb-2 {{
      position: absolute;
      width: 620px;
      height: 620px;
      border-radius: 50%;
      background: {accent};
      opacity: 0.10;
      filter: blur(120px);
      bottom: -140px;
      {'right' if rtl else 'left'}: -160px;
      pointer-events: none;
    }}
    .copy-zone {{
      position: relative;
      z-index: 2;
      width: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 24px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 12px;
      padding: 14px 30px;
      border-radius: 999px;
      background: {theme['badge_bg']};
      border: 1.5px solid rgba(255,255,255,0.18);
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .badge-dot {{
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: {accent};
      box-shadow: 0 0 18px {accent};
    }}
    .headline {{
      font-size: 74px;
      font-weight: 800;
      line-height: 1.24;
      max-width: 920px;
      letter-spacing: 0;
      text-wrap: balance;
    }}
    .subtitle {{
      font-size: 36px;
      font-weight: 500;
      line-height: 1.45;
      color: {theme['sub_fg']};
      max-width: 860px;
    }}
    .device-zone {{
      position: relative;
      z-index: 2;
      width: 100%;
      display: flex;
      justify-content: center;
      align-items: center;
      margin-top: 18px;
    }}
    .device-glow {{
      position: absolute;
      width: 580px;
      height: 920px;
      background: {accent};
      opacity: 0.22;
      filter: blur(90px);
      border-radius: 80px;
    }}
    .phone-frame {{
      position: relative;
      width: 630px;
      height: 1240px;
      border-radius: 64px;
      padding: 20px;
      background: linear-gradient(160deg, #2B3344 0%, #0E131D 100%);
      box-shadow: 0 42px 96px rgba(0, 0, 0, 0.55), inset 0 0 0 2px rgba(255,255,255,0.18);
      transform: {theme['phone_transform']};
    }}
    .phone-notch {{
      position: absolute;
      top: 30px;
      left: 50%;
      transform: translateX(-50%);
      width: 140px;
      height: 26px;
      border-radius: 16px;
      background: #090D16;
      z-index: 5;
    }}
    .phone-screen {{
      width: 100%;
      height: 100%;
      border-radius: 48px;
      overflow: hidden;
      background: #0B1120;
      position: relative;
    }}
    .raw-shot {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .mock-ui {{
      width: 100%;
      height: 100%;
      padding: 64px 32px 36px 32px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      background: linear-gradient(180deg, #111827 0%, #090D16 100%);
      color: #F8FAFC;
      text-align: {'right' if rtl else 'left'};
    }}
    .mock-statusbar {{
      display: flex;
      justify-content: space-between;
      font-size: 20px;
      color: #94A3B8;
      direction: ltr;
    }}
    .mock-hero {{
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      gap: 18px;
      margin-top: 12px;
    }}
    .mock-cover {{
      width: 220px;
      height: 220px;
      border-radius: 36px;
      object-fit: cover;
      box-shadow: 0 20px 50px rgba(0,0,0,0.45);
      border: 3px solid {accent};
    }}
    .mock-cover-fallback {{
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 96px;
      font-weight: 800;
      background: linear-gradient(135deg, {accent}, #1E293B);
      color: #0B1120;
    }}
    .mock-app-title {{
      font-size: 36px;
      font-weight: 800;
    }}
    .mock-pill {{
      font-size: 22px;
      padding: 8px 22px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      color: {accent};
      font-weight: 700;
    }}
    .mock-list {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .mock-row {{
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 18px 20px;
      border-radius: 22px;
      background: rgba(255,255,255,0.05);
    }}
    .mock-row.active {{
      background: rgba(255,255,255,0.12);
      border: 1.5px solid {accent};
    }}
    .mock-dot {{
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: {accent};
      flex-shrink: 0;
    }}
    .mock-meta {{ flex: 1; overflow: hidden; }}
    .mock-title {{ font-size: 24px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .mock-sub {{ font-size: 18px; color: #94A3B8; margin-top: 4px; }}
    .mock-time {{ font-size: 20px; color: #CBD5E1; direction: ltr; }}
    .mock-player {{
      padding: 24px;
      border-radius: 28px;
      background: rgba(255,255,255,0.07);
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .mock-progress {{
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.16);
      overflow: hidden;
      direction: ltr;
    }}
    .mock-progress-fill {{
      width: 58%;
      height: 100%;
      background: {accent};
      border-radius: 999px;
    }}
    .mock-controls {{
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 44px;
      font-size: 34px;
      direction: ltr;
    }}
    .play-btn {{
      width: 72px;
      height: 72px;
      border-radius: 50%;
      background: {accent};
      color: #090D16;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 900;
    }}
  </style>
</head>
<body>
  <div class="canvas">
    <div class="orb-1"></div>
    <div class="orb-2"></div>
    {ordered_sections}
  </div>
</body>
</html>
"""


def build_feature_graphic_html(
    locale: str,
    template_id: str,
    font_ar: str,
    font_en: str,
    accent_color: str,
    title: str,
    tagline: str,
    icon_path: Path | None = None,
) -> str:
    rtl = is_rtl_locale(locale)
    direction = "rtl" if rtl else "ltr"
    theme = TEMPLATE_THEMES.get(template_id, TEMPLATE_THEMES["editorial_spotlight"])
    accent = _sanitize_hex_color(accent_color)
    font_family = font_ar if rtl else font_en
    icon_uri = _file_to_data_uri(icon_path)

    safe_title = html.escape(title or "App")
    safe_tagline = html.escape(tagline or "")
    icon_html = (
        f'<img class="fg-icon" src="{icon_uri}" alt="App Icon" />'
        if icon_uri
        else f'<div class="fg-icon fg-icon-fallback">{html.escape((title or "A")[:1].upper())}</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="{html.escape(locale)}" dir="{direction}">
<head>
  <meta charset="utf-8" />
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
      width: 1024px;
      height: 500px;
      overflow: hidden;
      font-family: {font_family};
      background: {theme['bg']};
      color: {theme['fg']};
      -webkit-font-smoothing: antialiased;
    }}
    .fg {{
      position: relative;
      width: 1024px;
      height: 500px;
      padding: 56px 68px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 48px;
    }}
    .glow {{
      position: absolute;
      width: 520px;
      height: 520px;
      border-radius: 50%;
      background: {accent};
      opacity: 0.18;
      filter: blur(95px);
      top: -120px;
      {'left' if rtl else 'right'}: -80px;
    }}
    .text-col {{
      position: relative;
      z-index: 2;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 20px;
      text-align: {'right' if rtl else 'left'};
    }}
    .accent-bar {{
      width: 96px;
      height: 8px;
      border-radius: 999px;
      background: {accent};
    }}
    .fg-title {{
      font-size: 54px;
      font-weight: 800;
      line-height: 1.24;
      letter-spacing: 0;
      text-wrap: balance;
    }}
    .fg-tagline {{
      font-size: 28px;
      font-weight: 500;
      line-height: 1.45;
      color: {theme['sub_fg']};
      max-width: 600px;
    }}
    .icon-col {{
      position: relative;
      z-index: 2;
      flex-shrink: 0;
    }}
    .fg-icon {{
      width: 260px;
      height: 260px;
      border-radius: 56px;
      object-fit: cover;
      border: 4px solid {accent};
      box-shadow: 0 28px 68px rgba(0,0,0,0.48);
    }}
    .fg-icon-fallback {{
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 112px;
      font-weight: 800;
      background: linear-gradient(135deg, {accent}, #1E293B);
      color: #090D16;
    }}
  </style>
</head>
<body>
  <div class="fg">
    <div class="glow"></div>
    <div class="text-col">
      <div class="accent-bar"></div>
      <div class="fg-title">{safe_title}</div>
      <div class="fg-tagline">{safe_tagline}</div>
    </div>
    <div class="icon-col">
      {icon_html}
    </div>
  </div>
</body>
</html>
"""


def render_html_to_png(
    html_content: str,
    output_png: Path,
    width: int,
    height: int,
    browser_exe: Path | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Render an HTML document to an exact (width x height) PNG using headless Chrome/Edge."""
    browser = browser_exe or find_local_chromium_browser()
    if browser is None or not browser.exists():
        raise FileNotFoundError(
            "No local Chromium browser (Chrome or Edge) found for RTL HTML rendering."
        )

    output_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="playlaunch_render_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        html_file = tmp_dir / "canvas.html"
        profile_dir = tmp_dir / "browser_profile"
        raw_shot = tmp_dir / "raw_capture.png"
        html_file.write_text(html_content, encoding="utf-8")

        file_url = html_file.resolve().as_uri()
        cmd = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--default-background-color=00000000",
            f"--user-data-dir={profile_dir}",
            f"--window-size={width},{height}",
            f"--screenshot={raw_shot}",
            file_url,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if not raw_shot.exists():
            stderr_tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(
                f"Headless browser did not produce screenshot (exit={proc.returncode}): {stderr_tail}"
            )

        # Normalize to exact target dimensions with Pillow (handles minor headless chrome frame padding)
        with Image.open(raw_shot) as img:
            rgb_img = img.convert("RGB")
            if rgb_img.size != (width, height):
                rgb_img = rgb_img.resize((width, height), Image.Resampling.LANCZOS)
            rgb_img.save(output_png, format="PNG", optimize=True)

    with Image.open(output_png) as verified:
        final_w, final_h = verified.size

    return {
        "path": str(output_png),
        "width": final_w,
        "height": final_h,
        "size_bytes": output_png.stat().st_size,
        "browser_used": str(browser),
    }
