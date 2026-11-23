"""Stage 3 — Raw Screenshot Capture via ADB / Pre-Captured Fallback (stages/03_capture_screens.py).

- Detects connected Android emulators or devices via `adb devices`.
- Optional Mode A hook (`--launch-via-workspace-api`): can invoke `POST http://127.0.0.1:5000/api/android/run`
  when Youssef Audio Workspace local desktop server is running.
- Graceful Fallback (never breaks the pipeline):
    * If no ADB device/emulator is connected, cleanly uses any pre-captured PNG/JPG files in
      `<run-dir>/raw_screenshots/` (or imports from `--import-dir`).
    * If neither ADB device nor pre-captured images exist, marks Stage 3 `skipped` with a clear
      informational warning so Stage 6 proceeds seamlessly with its clean UI mockup fallback.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image

STUDIO_ROOT = Path(__file__).resolve().parent.parent
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from core.config_loader import load_studio_config
from core.env_resolver import resolve_environment
from core.logger import get_logger
from core.run_state import DEFAULT_RUNS_ROOT, RunStateManager, utc_now_iso
from core.validators import assert_path_within

logger = get_logger("stage.03_capture_screens")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def list_connected_adb_devices(adb_bin: str | None) -> list[str]:
    """Return serial IDs of connected, ready (`device` state) ADB devices/emulators."""
    if not adb_bin:
        return []
    try:
        proc = subprocess.run(
            [adb_bin, "devices"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if proc.returncode != 0:
            return []
        devices: list[str] = []
        for line in (proc.stdout or "").splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except (OSError, subprocess.SubprocessError):
        return []


def capture_adb_screenshot(adb_bin: str, serial: str, output_png: Path) -> dict[str, Any]:
    output_png.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [adb_bin, "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout or not proc.stdout.startswith(b"\x89PNG"):
        raise RuntimeError(f"ADB screencap failed on device '{serial}' (exit={proc.returncode}).")
    output_png.write_bytes(proc.stdout)
    with Image.open(output_png) as im:
        w, h = im.size
    return {"path": str(output_png), "width": w, "height": h, "size_bytes": output_png.stat().st_size}


def _inventory_existing_raw_screenshots(raw_dir: Path) -> list[dict[str, Any]]:
    if not raw_dir.exists():
        return []
    found: list[dict[str, Any]] = []
    for p in sorted(raw_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            try:
                with Image.open(p) as im:
                    w, h = im.size
            except Exception:
                continue
            found.append(
                {
                    "path": str(p),
                    "relative": str(p.relative_to(raw_dir)),
                    "width": w,
                    "height": h,
                    "size_bytes": p.stat().st_size,
                }
            )
    return found


def run_stage_03(
    run_dir: Path,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    serial: str | None = None,
    count: int = 3,
    delay_seconds: float = 2.0,
    import_dir: Path | None = None,
    use_existing_only: bool = False,
    launch_via_workspace_api: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Capture raw screenshots via ADB or gracefully fall back to pre-captured shots."""
    manager, state = RunStateManager.load(run_dir=run_dir, runs_root=runs_root)
    manifest = json.loads((manager.run_dir / "intake_manifest.json").read_text(encoding="utf-8"))
    config = load_studio_config()
    env_info = resolve_environment(config=config)

    manager.update_stage(state, "03_capture_screens", "running", message="Inspecting ADB devices & raw_screenshots/")

    raw_root = manager.run_dir / "raw_screenshots"
    assert_path_within(raw_root, manager.run_dir)
    raw_root.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    imported_count = 0
    if import_dir is not None and import_dir.exists():
        for src in sorted(import_dir.iterdir()):
            if src.is_file() and src.suffix.lower() in IMAGE_EXTS:
                shutil.copy2(src, raw_root / src.name)
                imported_count += 1

    devices = [] if use_existing_only else list_connected_adb_devices(env_info.adb_bin)
    chosen_device = serial if (serial and serial in devices) else (devices[0] if devices else None)
    captured_items: list[dict[str, Any]] = []
    source_mode = "none"

    if chosen_device and env_info.adb_bin:
        source_mode = "adb_capture"
        app = manifest.get("app", {})
        package_name = str(app.get("package_name") or "")
        locales = app.get("target_locales") or ["ar"]

        # Optional Mode A local API launch hook
        if launch_via_workspace_api and manifest.get("mode") == "audio_workspace":
            api_base = (manifest.get("audio_workspace_refs") or {}).get("local_api_url", "http://127.0.0.1:5000")
            try:
                requests.post(
                    f"{api_base.rstrip('/')}/api/android/run",
                    json={"app_slug": app.get("slug"), "flavor": app.get("flavor"), "device": chosen_device},
                    timeout=5,
                )
            except Exception as exc:
                warnings.append(f"Optional workspace API launch skipped ({api_base}): {exc}")

        if package_name:
            subprocess.run(
                [env_info.adb_bin, "-s", chosen_device, "shell", "monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        target_locale_dir = raw_root / locales[0]
        for idx in range(1, max(1, count) + 1):
            out_png = target_locale_dir / f"raw_{idx:02d}.png"
            try:
                shot = capture_adb_screenshot(env_info.adb_bin, chosen_device, out_png)
                captured_items.append(shot)
                if idx < count and delay_seconds > 0:
                    time.sleep(delay_seconds)
            except Exception as exc:
                warnings.append(f"ADB capture #{idx} failed: {exc}")
                break

    existing_inventory = _inventory_existing_raw_screenshots(raw_root)
    if source_mode == "none" and existing_inventory:
        source_mode = "pre_captured_existing"
        if not use_existing_only:
            warnings.append(
                f"No ADB device/emulator connected; seamlessly using {len(existing_inventory)} pre-captured image(s) in raw_screenshots/."
            )
    elif source_mode == "none":
        warnings.append(
            "No ADB device/emulator connected and raw_screenshots/ is empty. "
            "Stage 3 marked skipped safely; Stage 6 will render clean UI mockups automatically."
        )

    stage_status = "completed" if existing_inventory else "skipped"
    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "source_mode": source_mode,
        "adb_bin": env_info.adb_bin,
        "connected_devices": devices,
        "device_used": chosen_device,
        "imported_count": imported_count,
        "captured_count": len(captured_items),
        "total_raw_screenshots": len(existing_inventory),
        "raw_screenshots": existing_inventory,
        "warnings": warnings,
    }

    report_path = manager.write_json_artifact("screen_capture_report.json", report)
    manager.update_stage(
        state,
        "03_capture_screens",
        stage_status,
        message=(
            f"Stage 3 ready: {len(existing_inventory)} raw screenshot(s) available ({source_mode})."
            if existing_inventory
            else "Stage 3 gracefully skipped (no ADB device and no pre-captured raw_screenshots)."
        ),
        artifacts={
            "screen_capture_report": str(report_path),
            "raw_screenshots_dir": str(raw_root),
        },
        warnings=warnings,
    )
    return report_path, report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlayLaunch Studio — Stage 3 Raw Screen Capture")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--serial", help="Specific ADB device/emulator serial")
    parser.add_argument("--count", type=int, default=3, help="Number of ADB screenshots to capture")
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--import-dir", type=Path, help="Import pre-captured raw screenshots from this folder")
    parser.add_argument("--use-existing-only", action="store_true", help="Skip ADB probing and use raw_screenshots/")
    parser.add_argument("--launch-via-workspace-api", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report_path, report = run_stage_03(
        run_dir=args.run_dir,
        runs_root=args.runs_root,
        serial=args.serial,
        count=args.count,
        delay_seconds=args.delay_seconds,
        import_dir=args.import_dir,
        use_existing_only=args.use_existing_only,
        launch_via_workspace_api=args.launch_via_workspace_api,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "screen_capture_report": str(report_path),
                "source_mode": report["source_mode"],
                "total_raw_screenshots": report["total_raw_screenshots"],
                "warnings_count": len(report["warnings"]),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
