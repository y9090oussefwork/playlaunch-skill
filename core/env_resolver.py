"""Environment and toolchain resolver for PlayLaunch Studio.

CRITICAL DESIGN RULE:
Defaults JAVA_HOME to Android Studio JBR (Java 17/21) for detekt, gradlew, and jarsigner
instead of system Java 23+, ensuring compatibility across Kotlin/Gradle/Detekt tooling.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import STUDIO_ROOT
from core.config_loader import expand_win_path, load_studio_config

DEFAULT_JBR_CANDIDATES = (
    r"C:\Program Files\Android\Android Studio\jbr",
    r"%LOCALAPPDATA%\Programs\Android Studio\jbr",
)


@dataclass
class ResolvedEnvironment:
    studio_root: str
    python_bin: str | None
    node_bin: str | None
    npm_bin: str | None
    git_bin: str | None
    flutter_bin: str | None
    java_home: str | None
    java_source: str
    java_version: str | None
    java_bin: str | None
    jarsigner_bin: str | None
    keytool_bin: str | None
    android_sdk: str | None
    adb_bin: str | None
    emulator_bin: str | None
    aapt2_bin: str | None
    detekt_jar: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_java_release_version(java_home: Path) -> str | None:
    """Read JAVA_VERSION from <java_home>/release without spawning a subprocess."""
    release_file = java_home / "release"
    if not release_file.exists():
        return None
    try:
        content = release_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r'^JAVA_VERSION="([^"]+)"', content, re.MULTILINE)
    return match.group(1) if match else None


def _probe_java_version(java_bin: Path) -> str | None:
    try:
        proc = subprocess.run(
            [str(java_bin), "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (proc.stderr or "") + "\n" + (proc.stdout or "")
        match = re.search(r'version\s+"([^"]+)"', output)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def _is_valid_java_home(candidate: Path | None) -> bool:
    if not candidate:
        return False
    return (candidate / "bin" / "java.exe").exists() or (candidate / "bin" / "java").exists()


def _pick_exe(base_dir: Path, name: str) -> Path | None:
    for suffix in (".exe", ".bat", ".cmd", ""):
        p = base_dir / f"{name}{suffix}"
        if p.exists() and p.is_file():
            return p
    return None


def resolve_java_home(
    config: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path | None, str, str | None]:
    """Resolve JAVA_HOME prioritizing Android Studio JBR over system Java 23.

    Resolution order:
    1. Explicit `environment.java_home_override` in studio.config.json (if valid).
    2. Android Studio JBR candidates (when `prefer_android_studio_jbr` is true, default True).
    3. `JAVA_HOME` environment variable.
    4. System `java` on PATH (last resort).
    """
    cfg = config if config is not None else load_studio_config()
    env_cfg = cfg.get("environment", {})
    proc_env = env if env is not None else dict(os.environ)

    # 1. Explicit config override
    override = expand_win_path(env_cfg.get("java_home_override"))
    if _is_valid_java_home(override):
        assert override is not None
        ver = _read_java_release_version(override) or _probe_java_version(override / "bin" / "java.exe")
        source = "android_studio_jbr" if "android studio" in str(override).lower() and "jbr" in str(override).lower() else "config_override"
        return override, source, ver

    # 2. Android Studio JBR candidates (default preferred over Java 23)
    if env_cfg.get("prefer_android_studio_jbr", True):
        candidates = env_cfg.get("android_studio_jbr_candidates") or list(DEFAULT_JBR_CANDIDATES)
        for raw in candidates:
            candidate = expand_win_path(raw)
            if _is_valid_java_home(candidate):
                assert candidate is not None
                ver = _read_java_release_version(candidate) or _probe_java_version(candidate / "bin" / "java.exe")
                return candidate, "android_studio_jbr", ver

    # 3. Existing JAVA_HOME env var
    env_java = expand_win_path(proc_env.get("JAVA_HOME"))
    if _is_valid_java_home(env_java):
        assert env_java is not None
        ver = _read_java_release_version(env_java) or _probe_java_version(env_java / "bin" / "java.exe")
        return env_java, "env_java_home", ver

    # 4. System PATH java fallback
    which_java = shutil.which("java", path=proc_env.get("PATH"))
    if which_java:
        java_path = Path(which_java).resolve()
        home = java_path.parent.parent
        ver = _read_java_release_version(home) or _probe_java_version(java_path)
        return home, "system_path_fallback", ver

    return None, "not_found", None


def _resolve_android_sdk(cfg: dict[str, Any], proc_env: dict[str, str]) -> Path | None:
    candidates = [
        expand_win_path(cfg.get("environment", {}).get("android_sdk_path")),
        expand_win_path(proc_env.get("ANDROID_HOME")),
        expand_win_path(proc_env.get("ANDROID_SDK_ROOT")),
        expand_win_path(r"%LOCALAPPDATA%\Android\Sdk"),
    ]
    for candidate in candidates:
        if candidate and candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _resolve_latest_aapt2(android_sdk: Path | None) -> Path | None:
    if not android_sdk:
        return None
    bt_dir = android_sdk / "build-tools"
    if not bt_dir.exists():
        return None
    versions = sorted((p for p in bt_dir.iterdir() if p.is_dir()), reverse=True)
    for ver_dir in versions:
        exe = _pick_exe(ver_dir, "aapt2")
        if exe:
            return exe
    return None


def _resolve_detekt_jar(studio_root: Path = STUDIO_ROOT) -> Path | None:
    detekt_dir = studio_root / "vendors" / "detekt"
    if not detekt_dir.exists():
        return None
    manifest = detekt_dir / "vendor_manifest.json"
    if manifest.exists():
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            jar_name = meta.get("jar_file")
            if jar_name and (detekt_dir / jar_name).exists():
                return detekt_dir / jar_name
        except (OSError, json.JSONDecodeError):
            pass
    jars = sorted(detekt_dir.glob("detekt-cli*-all.jar"), reverse=True)
    return jars[0] if jars else None


def resolve_environment(
    config: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    studio_root: Path = STUDIO_ROOT,
) -> ResolvedEnvironment:
    """Discover all tools needed across the 7-stage pipeline."""
    cfg = config if config is not None else load_studio_config()
    env_cfg = cfg.get("environment", {})
    proc_env = env if env is not None else dict(os.environ)

    # Prefer project .venv Python 3.12
    venv_python = studio_root / ".venv" / "Scripts" / "python.exe"
    cfg_python = expand_win_path(env_cfg.get("python_bin"))
    if cfg_python and cfg_python.exists():
        python_bin: str | None = str(cfg_python)
    elif venv_python.exists():
        python_bin = str(venv_python)
    else:
        python_bin = sys.executable

    java_home, java_source, java_version = resolve_java_home(cfg, proc_env)
    java_bin = _pick_exe(java_home / "bin", "java") if java_home else None
    jarsigner_bin = _pick_exe(java_home / "bin", "jarsigner") if java_home else None
    keytool_bin = _pick_exe(java_home / "bin", "keytool") if java_home else None

    android_sdk = _resolve_android_sdk(cfg, proc_env)
    adb_bin = _pick_exe(android_sdk / "platform-tools", "adb") if android_sdk else None
    if not adb_bin:
        found_adb = shutil.which("adb", path=proc_env.get("PATH"))
        adb_bin = Path(found_adb) if found_adb else None

    emulator_bin = _pick_exe(android_sdk / "emulator", "emulator") if android_sdk else None
    aapt2_bin = _resolve_latest_aapt2(android_sdk)
    detekt_jar = _resolve_detekt_jar(studio_root)

    node_bin = str(expand_win_path(env_cfg.get("node_bin"))) if env_cfg.get("node_bin") else shutil.which("node", path=proc_env.get("PATH"))
    npm_bin = shutil.which("npm", path=proc_env.get("PATH"))
    git_bin = shutil.which("git", path=proc_env.get("PATH"))
    flutter_bin = str(expand_win_path(env_cfg.get("flutter_bin"))) if env_cfg.get("flutter_bin") else shutil.which("flutter", path=proc_env.get("PATH"))

    return ResolvedEnvironment(
        studio_root=str(studio_root),
        python_bin=python_bin,
        node_bin=node_bin,
        npm_bin=npm_bin,
        git_bin=git_bin,
        flutter_bin=flutter_bin,
        java_home=str(java_home) if java_home else None,
        java_source=java_source,
        java_version=java_version,
        java_bin=str(java_bin) if java_bin else None,
        jarsigner_bin=str(jarsigner_bin) if jarsigner_bin else None,
        keytool_bin=str(keytool_bin) if keytool_bin else None,
        android_sdk=str(android_sdk) if android_sdk else None,
        adb_bin=str(adb_bin) if adb_bin else None,
        emulator_bin=str(emulator_bin) if emulator_bin else None,
        aapt2_bin=str(aapt2_bin) if aapt2_bin else None,
        detekt_jar=str(detekt_jar) if detekt_jar else None,
    )


def build_jvm_env(
    config: dict[str, Any] | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return subprocess environment with JAVA_HOME forced to Android Studio JBR.

    Use this for all detekt, gradlew, and jarsigner invocations so system Java 23 is never picked.
    """
    env = dict(base_env if base_env is not None else os.environ)
    resolved = resolve_environment(config=config, env=env)

    path_parts: list[str] = []
    if resolved.java_home:
        env["JAVA_HOME"] = resolved.java_home
        path_parts.append(str(Path(resolved.java_home) / "bin"))

    if resolved.android_sdk:
        env["ANDROID_HOME"] = resolved.android_sdk
        env["ANDROID_SDK_ROOT"] = resolved.android_sdk
        path_parts.append(str(Path(resolved.android_sdk) / "platform-tools"))

    existing_path = env.get("PATH", "")
    if path_parts:
        env["PATH"] = os.pathsep.join(path_parts + ([existing_path] if existing_path else []))

    return env


if __name__ == "__main__":
    resolved_env = resolve_environment()
    print(json.dumps(resolved_env.to_dict(), indent=2, ensure_ascii=False))
