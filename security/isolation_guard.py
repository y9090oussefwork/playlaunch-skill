"""Strict Fail-Closed Account Isolation Guard (security/isolation_guard.py).

Non-negotiable security gate preventing Google Play Developer Account association.
EVERY outbound Google Play API call (Publishing API v3 or Developer Reporting API)
MUST acquire a verified `IsolatedPlaySession` from this module BEFORE initializing
`google-api-python-client`.

Enforces:
  - Fail-Closed IPv4-only public IP discovery (rejects IPv6, private IPs, or lookup failures).
  - Layer 1: Persistent SQLite IP & 1-to-1 Key Binding Ledger (`security/ip_ledger.sqlite3`).
  - Layer 2A: `router_reboot` mode with /24 subnet blocking, fresh-IP enforcement, 5s polling,
              and live UI status updates in `security/guard_live_status.json`.
  - Layer 2B: `dedicated_network` mode enforcing EITHER:
              (i)  Exclusive HTTP/SOCKS5 proxy (with optional Windows Credential Manager `keyring`
                   password) for BOTH IP check and `httplib2.Http` Play API transport (ZERO direct fallback), OR
              (ii) Windows Wi-Fi/Hotspot SSID verification via `netsh wlan show interfaces`
                   (aborts immediately on mismatch).
  - Layer 3: Strict 1-to-1 Service Account Key Lock (`key_file_sha256` UNIQUE + `service_account_email` UNIQUE).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import httplib2
import requests
import socks

from core import STUDIO_ROOT
from core.config_loader import load_studio_config
from core.logger import get_logger
from core.validators import sha256_file

logger = get_logger("security.isolation_guard")

DEFAULT_IP_LEDGER_DB = STUDIO_ROOT / "security" / "ip_ledger.sqlite3"
DEFAULT_LIVE_STATUS_PATH = STUDIO_ROOT / "security" / "guard_live_status.json"

DEFAULT_IPV4_ENDPOINTS: tuple[str, ...] = (
    "https://api.ipify.org?format=json",
    "https://ipv4.icanhazip.com",
    "https://v4.ident.me",
)


class IsolationGuardError(RuntimeError):
    """Base fail-closed exception raised whenever any isolation rule fails."""


class KeyLockViolationError(IsolationGuardError):
    """Raised when a service account key fails 1-to-1 account binding verification."""


class NetworkIsolationError(IsolationGuardError):
    """Raised when proxy, Wi-Fi SSID, IPv4 lookup, or IP ledger rules fail."""


@dataclass
class IsolatedPlaySession:
    """Verified isolation proof returned to Stage 7 before touching Google Play APIs."""

    account_id: str
    action_type: str
    isolation_mode: str
    verified_ip: str
    subnet_24: str
    key_file_path: str
    key_file_sha256: str
    service_account_email: str
    proxy_url_redacted: str | None
    wifi_ssid_verified: str | None
    dry_run: bool
    verified_at: str
    # Dedicated httplib2 transport pre-bound to the account's proxy (if proxy mode)
    http_transport: httplib2.Http | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "action_type": self.action_type,
            "isolation_mode": self.isolation_mode,
            "verified_ip": self.verified_ip,
            "subnet_24": self.subnet_24,
            "key_file_path": self.key_file_path,
            "key_file_sha256": self.key_file_sha256,
            "service_account_email": self.service_account_email,
            "proxy_url_redacted": self.proxy_url_redacted,
            "wifi_ssid_verified": self.wifi_ssid_verified,
            "dry_run": self.dry_run,
            "verified_at": self.verified_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_strict_public_ipv4(raw_ip: str) -> tuple[str, str]:
    """Validate that `raw_ip` is a public IPv4 address and return `(ip, subnet_24)`.

    FAIL-CLOSED: Rejects IPv6, malformed strings, and private/loopback/reserved addresses.
    """
    cleaned = (raw_ip or "").strip()
    try:
        addr = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise NetworkIsolationError(f"Fail-closed: Invalid IP address response '{cleaned}'.") from exc

    if not isinstance(addr, ipaddress.IPv4Address):
        raise NetworkIsolationError(
            f"Fail-closed: Received non-IPv4 address '{cleaned}' (IPv6 rejected for /24 subnet accuracy)."
        )
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        raise NetworkIsolationError(
            f"Fail-closed: Non-public IPv4 address '{cleaned}' rejected."
        )

    network = ipaddress.IPv4Network(f"{addr}/24", strict=False)
    return str(addr), str(network)


def redact_proxy_url(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    host_port = parsed.hostname or ""
    if parsed.port:
        host_port = f"{host_port}:{parsed.port}"
    if parsed.username:
        netloc = f"{parsed.username}:***@{host_port}"
    else:
        netloc = host_port
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def resolve_proxy_url_with_keyring(net_cfg: dict[str, Any]) -> str | None:
    """Attach proxy password from Windows Credential Manager (`keyring`) if configured."""
    raw_url = str(net_cfg.get("dedicated_proxy_url") or "").strip()
    if not raw_url:
        return None

    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in ("http", "https", "socks5", "socks5h"):
        raise NetworkIsolationError(
            f"Fail-closed: Unsupported proxy scheme '{parsed.scheme}'. Use http, https, socks5, or socks5h."
        )
    if not parsed.hostname or not parsed.port:
        raise NetworkIsolationError("Fail-closed: dedicated_proxy_url must include both host and port.")

    if parsed.password:
        return raw_url

    service = str(net_cfg.get("proxy_keyring_service") or "").strip()
    username = str(net_cfg.get("proxy_username") or parsed.username or "").strip()
    if service and username:
        try:
            import keyring  # Lazy import so tests can mock easily

            secret = keyring.get_password(service, username)
        except Exception as exc:
            raise NetworkIsolationError(
                f"Fail-closed: Failed to read proxy credentials from keyring ({service}): {exc}"
            ) from exc
        if not secret:
            raise NetworkIsolationError(
                f"Fail-closed: No proxy password found in Windows Credential Manager for service='{service}', user='{username}'."
            )
        netloc = f"{username}:{secret}@{parsed.hostname}:{parsed.port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))

    return raw_url


def build_proxy_httplib2_http(proxy_url: str, timeout: int = 30) -> httplib2.Http:
    """Create an `httplib2.Http` strictly bound to `proxy_url` (ZERO direct fallback)."""
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme in ("socks5", "socks5h"):
        proxy_type = socks.PROXY_TYPE_SOCKS5
        rdns = scheme == "socks5h"
    elif scheme in ("http", "https"):
        proxy_type = socks.PROXY_TYPE_HTTP
        rdns = True
    else:
        raise NetworkIsolationError(f"Fail-closed: Unsupported proxy scheme '{scheme}'.")

    if not parsed.hostname or not parsed.port:
        raise NetworkIsolationError("Fail-closed: Proxy URL missing hostname or port.")

    proxy_info = httplib2.ProxyInfo(
        proxy_type=proxy_type,
        proxy_host=parsed.hostname,
        proxy_port=int(parsed.port),
        proxy_rdns=rdns,
        proxy_user=parsed.username,
        proxy_pass=parsed.password,
    )
    return httplib2.Http(timeout=timeout, proxy_info=proxy_info)


def get_active_windows_wifi_info() -> dict[str, Any]:
    """Inspect active Windows Wi-Fi SSID & interface name via `netsh wlan show interfaces`."""
    try:
        proc = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NetworkIsolationError(f"Fail-closed: Unable to run 'netsh wlan show interfaces': {exc}") from exc

    if proc.returncode != 0:
        raise NetworkIsolationError(
            f"Fail-closed: 'netsh wlan show interfaces' failed (exit={proc.returncode}): {(proc.stderr or proc.stdout).strip()}"
        )

    output = proc.stdout or ""
    ssid: str | None = None
    state: str | None = None
    adapter_name: str | None = None

    for line in output.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        k = key.strip().lower()
        v = val.strip()
        if k == "name" and adapter_name is None:
            adapter_name = v
        elif k == "state":
            state = v.lower()
        elif k == "ssid":  # Note: strictly 'ssid', NOT 'bssid'
            ssid = v

    return {
        "connected": state == "connected",
        "state": state,
        "ssid": ssid,
        "adapter_name": adapter_name,
    }


def query_public_ipv4(
    endpoints: tuple[str, ...] = DEFAULT_IPV4_ENDPOINTS,
    proxy_url: str | None = None,
    timeout_seconds: int = 8,
) -> tuple[str, str, str]:
    """Query public IPv4 strictly from IPv4-only endpoints.

    When `proxy_url` is set, forces ALL HTTP(S) traffic exclusively through that proxy
    with `trust_env=False` (ZERO direct fallback).
    Returns `(ip_address, subnet_24, endpoint_used)`.
    """
    session = requests.Session()
    session.trust_env = False  # Prevent OS/env proxy leakage
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    errors: list[str] = []
    for url in endpoints:
        try:
            resp = session.get(url, proxies=proxies, timeout=timeout_seconds)
            if resp.status_code != 200:
                errors.append(f"{url} -> HTTP {resp.status_code}")
                continue
            body = resp.text.strip()
            candidate_ip = body
            if body.startswith("{"):
                parsed = json.loads(body)
                candidate_ip = str(parsed.get("ip") or "")
            ip_str, subnet_24 = parse_strict_public_ipv4(candidate_ip)
            return ip_str, subnet_24, url
        except NetworkIsolationError as exc:
            errors.append(f"{url} -> {exc}")
        except Exception as exc:
            errors.append(f"{url} -> {exc}")

    raise NetworkIsolationError(
        "Fail-closed: Unable to obtain a verified public IPv4 address from any IPv4 endpoint"
        + (f" via dedicated proxy ({redact_proxy_url(proxy_url)})" if proxy_url else "")
        + f". Errors: {'; '.join(errors)}"
    )


class IsolationGuard:
    """Hard security gate governing every outbound Google Play API session."""

    def __init__(
        self,
        ledger_db_path: Path = DEFAULT_IP_LEDGER_DB,
        live_status_path: Path = DEFAULT_LIVE_STATUS_PATH,
        ipv4_endpoints: tuple[str, ...] | None = None,
        poll_interval_seconds: float | None = None,
        ip_check_timeout_seconds: int | None = None,
        ip_fetcher: Callable[..., tuple[str, str, str]] | None = None,
        wifi_inspector: Callable[[], dict[str, Any]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        cfg = load_studio_config().get("security", {})
        self.ledger_db_path = Path(ledger_db_path)
        self.live_status_path = Path(live_status_path)
        self.ipv4_endpoints = tuple(ipv4_endpoints or cfg.get("ipv4_check_endpoints") or DEFAULT_IPV4_ENDPOINTS)
        self.poll_interval_seconds = float(
            poll_interval_seconds if poll_interval_seconds is not None else cfg.get("router_reboot_poll_seconds", 5)
        )
        self.ip_check_timeout_seconds = int(
            ip_check_timeout_seconds if ip_check_timeout_seconds is not None else cfg.get("ip_check_timeout_seconds", 8)
        )
        self._ip_fetcher = ip_fetcher or query_public_ipv4
        self._wifi_inspector = wifi_inspector or get_active_windows_wifi_info
        self._sleep = sleep_fn

        self.ledger_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ledger_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ip_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip_address TEXT NOT NULL,
                    subnet_24 TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    publish_mode_used TEXT NOT NULL,
                    isp_or_org TEXT DEFAULT '',
                    first_used_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ip_usage_ip ON ip_usage (ip_address)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ip_usage_subnet ON ip_usage (subnet_24)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS key_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_file_sha256 TEXT NOT NULL UNIQUE,
                    service_account_email TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    bound_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def write_live_status(self, payload: dict[str, Any]) -> None:
        """Persist live guard status to security/guard_live_status.json for agent/CLI monitoring."""
        self.live_status_path.parent.mkdir(parents=True, exist_ok=True)
        enriched = dict(payload)
        enriched["updated_at"] = _utc_now()
        tmp = self.live_status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(enriched, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.live_status_path)

    # ------------------------------------------------------------------
    # Layer 3: Strict 1-to-1 Service Account Key Lock
    # ------------------------------------------------------------------
    def verify_and_bind_key(
        self,
        account_id: str,
        account_profile: dict[str, Any],
        studio_root: Path = STUDIO_ROOT,
        record_binding: bool = True,
    ) -> tuple[Path, str, str]:
        """Verify 1-to-1 binding between key file SHA-256, service_account_email, and account_id."""
        creds_cfg = account_profile.get("play_api_credentials", {})
        raw_key_path = str(creds_cfg.get("key_file_path") or "").strip()
        if not raw_key_path:
            raise KeyLockViolationError(
                f"Fail-closed: Account '{account_id}' has no play_api_credentials.key_file_path configured."
            )

        key_path = Path(raw_key_path)
        if not key_path.is_absolute():
            key_path = (studio_root / key_path).resolve()
        if not key_path.exists() or not key_path.is_file():
            raise KeyLockViolationError(
                f"Fail-closed: Service account key file does not exist for '{account_id}': {key_path}"
            )

        actual_sha = sha256_file(key_path)
        expected_sha = str(creds_cfg.get("key_file_sha256") or "").strip().lower()
        if expected_sha and actual_sha.lower() != expected_sha:
            raise KeyLockViolationError(
                f"Fail-closed: Key file SHA-256 mismatch for '{account_id}'. "
                f"Expected {expected_sha[:12]}..., got {actual_sha[:12]}..."
            )

        try:
            key_json = json.loads(key_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise KeyLockViolationError(
                f"Fail-closed: Service account key file is not valid JSON ({key_path}): {exc}"
            ) from exc

        actual_email = str(key_json.get("client_email") or "").strip().lower()
        if not actual_email or "@" not in actual_email:
            raise KeyLockViolationError(
                f"Fail-closed: Service account key file missing valid 'client_email': {key_path}"
            )

        expected_email = str(creds_cfg.get("service_account_email") or "").strip().lower()
        if expected_email and actual_email != expected_email:
            raise KeyLockViolationError(
                f"Fail-closed: Service account email mismatch for '{account_id}'. "
                f"Profile expects '{expected_email}', key file has '{actual_email}'."
            )

        with self._connect() as conn:
            by_sha = conn.execute(
                "SELECT account_id, service_account_email FROM key_bindings WHERE key_file_sha256 = ?",
                (actual_sha,),
            ).fetchone()
            if by_sha and by_sha["account_id"] != account_id:
                raise KeyLockViolationError(
                    f"HARD BLOCK (1-to-1 Key Lock): Key SHA-256 {actual_sha[:12]}... is already bound to "
                    f"different account '{by_sha['account_id']}' and CANNOT be used for '{account_id}'."
                )

            by_email = conn.execute(
                "SELECT account_id, key_file_sha256 FROM key_bindings WHERE service_account_email = ?",
                (actual_email,),
            ).fetchone()
            if by_email and by_email["account_id"] != account_id:
                raise KeyLockViolationError(
                    f"HARD BLOCK (1-to-1 Key Lock): Service account email '{actual_email}' is already bound to "
                    f"different account '{by_email['account_id']}' and CANNOT be used for '{account_id}'."
                )

            if record_binding and not by_sha and not by_email:
                conn.execute(
                    """
                    INSERT INTO key_bindings (key_file_sha256, service_account_email, account_id, bound_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (actual_sha, actual_email, account_id, _utc_now()),
                )
                conn.commit()

        return key_path, actual_sha, actual_email

    # ------------------------------------------------------------------
    # Layer 1: IP Ledger Evaluation & Recording
    # ------------------------------------------------------------------
    def check_ip_against_ledger(
        self,
        account_id: str,
        ip_address: str,
        subnet_24: str,
        strict_subnet_block: bool = True,
        require_fresh_ip_every_run: bool = False,
    ) -> dict[str, Any]:
        """Evaluate whether `(ip_address, subnet_24)` is clean for `account_id`."""
        with self._connect() as conn:
            # 1. Exact IP used by DIFFERENT account -> HARD BLOCK
            cross_ip = conn.execute(
                "SELECT account_id, last_used_at FROM ip_usage WHERE ip_address = ? AND account_id != ? "
                "ORDER BY id DESC LIMIT 1",
                (ip_address, account_id),
            ).fetchone()
            if cross_ip:
                return {
                    "allowed": False,
                    "reason": "cross_account_exact_ip",
                    "blocked_by_account": cross_ip["account_id"],
                    "message": (
                        f"IP [{ip_address}] was previously used by another account "
                        f"('{cross_ip['account_id']}')."
                    ),
                }

            # 2. /24 Subnet used by DIFFERENT account -> HARD BLOCK if strict_subnet_block=True
            if strict_subnet_block:
                cross_subnet = conn.execute(
                    "SELECT account_id, ip_address, last_used_at FROM ip_usage "
                    "WHERE subnet_24 = ? AND account_id != ? ORDER BY id DESC LIMIT 1",
                    (subnet_24, account_id),
                ).fetchone()
                if cross_subnet:
                    return {
                        "allowed": False,
                        "reason": "cross_account_subnet_24",
                        "blocked_by_account": cross_subnet["account_id"],
                        "message": (
                            f"IP [{ip_address}] shares /24 subnet ({subnet_24}) with another account "
                            f"('{cross_subnet['account_id']}', IP {cross_subnet['ip_address']})."
                        ),
                    }

            # 3. Fresh IP every run -> BLOCK if IP exists for ANY account (including same account)
            if require_fresh_ip_every_run:
                any_ip = conn.execute(
                    "SELECT account_id, last_used_at FROM ip_usage WHERE ip_address = ? ORDER BY id DESC LIMIT 1",
                    (ip_address,),
                ).fetchone()
                if any_ip:
                    return {
                        "allowed": False,
                        "reason": "require_fresh_ip_every_run",
                        "blocked_by_account": any_ip["account_id"],
                        "message": (
                            f"IP [{ip_address}] was already used previously (require_fresh_ip_every_run=true)."
                        ),
                    }

        return {
            "allowed": True,
            "reason": "clean",
            "blocked_by_account": None,
            "message": f"IP [{ip_address}] ({subnet_24}) is clean for account '{account_id}'.",
        }

    def record_ip_usage(
        self,
        ip_address: str,
        subnet_24: str,
        account_id: str,
        publish_mode_used: str,
        action_type: str,
        status: str = "verified",
        isp_or_org: str = "",
    ) -> None:
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM ip_usage WHERE ip_address = ? AND account_id = ? AND action_type = ?",
                (ip_address, account_id, action_type),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE ip_usage SET last_used_at = ?, status = ?, publish_mode_used = ? WHERE id = ?",
                    (now, status, publish_mode_used, existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO ip_usage (
                        ip_address, subnet_24, account_id, publish_mode_used,
                        isp_or_org, first_used_at, last_used_at, action_type, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ip_address, subnet_24, account_id, publish_mode_used, isp_or_org, now, now, action_type, status),
                )
            conn.commit()

    def get_ip_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ip_usage ORDER BY last_used_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_key_bindings(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM key_bindings ORDER BY bound_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Master Session Acquisition Gate
    # ------------------------------------------------------------------
    def acquire_verified_session(
        self,
        account_profile: dict[str, Any],
        action_type: str = "play_api_publish",
        dry_run: bool = False,
        wait_for_router_reboot: bool = True,
        max_reboot_wait_seconds: float = 300.0,
        studio_root: Path = STUDIO_ROOT,
    ) -> IsolatedPlaySession:
        """Acquire a strictly verified Google Play API session or FAIL-CLOSED."""
        account_id = str(account_profile.get("account_id") or "").strip()
        if not account_id:
            raise IsolationGuardError("Fail-closed: Account profile missing 'account_id'.")

        net_cfg = account_profile.get("network_isolation", {})
        mode = str(net_cfg.get("mode") or "router_reboot").strip().lower()
        if mode not in ("router_reboot", "dedicated_network"):
            raise NetworkIsolationError(f"Fail-closed: Unsupported network_isolation.mode '{mode}'.")

        strict_subnet = bool(net_cfg.get("strict_subnet_block", True))
        require_fresh_ip = bool(net_cfg.get("require_fresh_ip_every_run", False))

        # Layer 3 FIRST: Strict 1-to-1 Key Lock BEFORE any network action
        key_path, key_sha, svc_email = self.verify_and_bind_key(
            account_id=account_id,
            account_profile=account_profile,
            studio_root=studio_root,
            record_binding=not dry_run,
        )

        # Layer 2B: dedicated_network (Proxy OR Wi-Fi/Hotspot SSID)
        if mode == "dedicated_network":
            proxy_url = resolve_proxy_url_with_keyring(net_cfg)
            required_ssid = str(
                net_cfg.get("required_wifi_ssid") or net_cfg.get("wifi_ssid") or ""
            ).strip()
            required_adapter = str(net_cfg.get("network_adapter_name") or "").strip()

            if not proxy_url and not required_ssid:
                raise NetworkIsolationError(
                    f"Fail-closed: Account '{account_id}' is in 'dedicated_network' mode but defines neither "
                    "'dedicated_proxy_url' nor 'required_wifi_ssid'."
                )

            verified_ssid: str | None = None
            if required_ssid:
                wifi_info = self._wifi_inspector()
                if not wifi_info.get("connected"):
                    self.write_live_status(
                        {
                            "state": "blocked",
                            "mode": mode,
                            "account_id": account_id,
                            "block_reason": "wifi_not_connected",
                            "required_wifi_ssid": required_ssid,
                            "actual_wifi_ssid": wifi_info.get("ssid"),
                        }
                    )
                    raise NetworkIsolationError(
                        f"Fail-closed: Wi-Fi is not connected (required SSID: '{required_ssid}')."
                    )
                actual_ssid = str(wifi_info.get("ssid") or "")
                if actual_ssid != required_ssid:
                    self.write_live_status(
                        {
                            "state": "blocked",
                            "mode": mode,
                            "account_id": account_id,
                            "block_reason": "wifi_ssid_mismatch",
                            "required_wifi_ssid": required_ssid,
                            "actual_wifi_ssid": actual_ssid,
                        }
                    )
                    raise NetworkIsolationError(
                        f"Fail-closed: Connected Wi-Fi SSID '{actual_ssid}' does NOT match account "
                        f"'{account_id}' required SSID '{required_ssid}'. Aborting immediately."
                    )
                if required_adapter and str(wifi_info.get("adapter_name") or "").lower() != required_adapter.lower():
                    raise NetworkIsolationError(
                        f"Fail-closed: Active Wi-Fi adapter '{wifi_info.get('adapter_name')}' does not match "
                        f"required adapter '{required_adapter}'."
                    )
                verified_ssid = actual_ssid

            # Query public IPv4 STRICTLY via proxy if proxy_url is configured (ZERO direct fallback)
            ip_str, subnet_24, _ = self._ip_fetcher(
                endpoints=self.ipv4_endpoints,
                proxy_url=proxy_url,
                timeout_seconds=self.ip_check_timeout_seconds,
            )

            ledger_check = self.check_ip_against_ledger(
                account_id=account_id,
                ip_address=ip_str,
                subnet_24=subnet_24,
                strict_subnet_block=strict_subnet,
                require_fresh_ip_every_run=require_fresh_ip,
            )
            if not ledger_check["allowed"]:
                self.write_live_status(
                    {
                        "state": "blocked",
                        "mode": mode,
                        "account_id": account_id,
                        "current_ip": ip_str,
                        "current_subnet_24": subnet_24,
                        "block_reason": ledger_check["reason"],
                        "blocked_by_account": ledger_check["blocked_by_account"],
                        "instruction": ledger_check["message"],
                    }
                )
                raise NetworkIsolationError(
                    f"Fail-closed (dedicated_network): {ledger_check['message']}"
                )

            http_transport = build_proxy_httplib2_http(proxy_url) if proxy_url else None

            if not dry_run:
                self.record_ip_usage(
                    ip_address=ip_str,
                    subnet_24=subnet_24,
                    account_id=account_id,
                    publish_mode_used=str(account_profile.get("publish_mode", "isolated_auto_publish")),
                    action_type=action_type,
                    status="verified",
                )

            self.write_live_status(
                {
                    "state": "verified",
                    "mode": mode,
                    "account_id": account_id,
                    "current_ip": ip_str,
                    "current_subnet_24": subnet_24,
                    "proxy_url_redacted": redact_proxy_url(proxy_url),
                    "wifi_ssid_verified": verified_ssid,
                    "dry_run": dry_run,
                    "block_reason": None,
                }
            )
            return IsolatedPlaySession(
                account_id=account_id,
                action_type=action_type,
                isolation_mode=mode,
                verified_ip=ip_str,
                subnet_24=subnet_24,
                key_file_path=str(key_path),
                key_file_sha256=key_sha,
                service_account_email=svc_email,
                proxy_url_redacted=redact_proxy_url(proxy_url),
                wifi_ssid_verified=verified_ssid,
                dry_run=dry_run,
                verified_at=_utc_now(),
                http_transport=http_transport,
            )

        # Layer 2A: router_reboot mode with live polling and UI status file
        elapsed = 0.0
        attempt = 0
        while True:
            attempt += 1
            try:
                ip_str, subnet_24, _ = self._ip_fetcher(
                    endpoints=self.ipv4_endpoints,
                    proxy_url=None,
                    timeout_seconds=self.ip_check_timeout_seconds,
                )
            except NetworkIsolationError as exc:
                if not wait_for_router_reboot or elapsed >= max_reboot_wait_seconds:
                    self.write_live_status(
                        {
                            "state": "blocked",
                            "mode": mode,
                            "account_id": account_id,
                            "current_ip": None,
                            "block_reason": "ipv4_lookup_failed",
                            "instruction": str(exc),
                            "attempt": attempt,
                        }
                    )
                    raise
                # Router is currently rebooting / offline — keep waiting cleanly
                self.write_live_status(
                    {
                        "state": "waiting_for_router_reboot",
                        "mode": mode,
                        "account_id": account_id,
                        "current_ip": None,
                        "block_reason": "router_reconnecting",
                        "instruction": "Router offline/reconnecting... waiting for a fresh IPv4 address.",
                        "attempt": attempt,
                        "poll_interval_seconds": self.poll_interval_seconds,
                    }
                )
                self._sleep(self.poll_interval_seconds)
                elapsed += self.poll_interval_seconds
                continue

            ledger_check = self.check_ip_against_ledger(
                account_id=account_id,
                ip_address=ip_str,
                subnet_24=subnet_24,
                strict_subnet_block=strict_subnet,
                require_fresh_ip_every_run=require_fresh_ip,
            )

            if ledger_check["allowed"]:
                if not dry_run:
                    self.record_ip_usage(
                        ip_address=ip_str,
                        subnet_24=subnet_24,
                        account_id=account_id,
                        publish_mode_used=str(account_profile.get("publish_mode", "isolated_auto_publish")),
                        action_type=action_type,
                        status="verified",
                    )
                self.write_live_status(
                    {
                        "state": "verified",
                        "mode": mode,
                        "account_id": account_id,
                        "current_ip": ip_str,
                        "current_subnet_24": subnet_24,
                        "block_reason": None,
                        "dry_run": dry_run,
                        "attempt": attempt,
                    }
                )
                return IsolatedPlaySession(
                    account_id=account_id,
                    action_type=action_type,
                    isolation_mode=mode,
                    verified_ip=ip_str,
                    subnet_24=subnet_24,
                    key_file_path=str(key_path),
                    key_file_sha256=key_sha,
                    service_account_email=svc_email,
                    proxy_url_redacted=None,
                    wifi_ssid_verified=None,
                    dry_run=dry_run,
                    verified_at=_utc_now(),
                    http_transport=None,
                )

            instruction = (
                f"IP [{ip_str}] is blocked/used ({ledger_check['reason']}). "
                "Please restart your router now to obtain a fresh IP."
            )
            self.write_live_status(
                {
                    "state": "waiting_for_router_reboot" if wait_for_router_reboot else "blocked",
                    "mode": mode,
                    "account_id": account_id,
                    "current_ip": ip_str,
                    "current_subnet_24": subnet_24,
                    "block_reason": ledger_check["reason"],
                    "blocked_by_account": ledger_check["blocked_by_account"],
                    "instruction": instruction,
                    "attempt": attempt,
                    "poll_interval_seconds": self.poll_interval_seconds,
                }
            )
            logger.warning(instruction)

            if not wait_for_router_reboot or elapsed >= max_reboot_wait_seconds:
                raise NetworkIsolationError(f"Fail-closed: {instruction}")

            self._sleep(self.poll_interval_seconds)
            elapsed += self.poll_interval_seconds
