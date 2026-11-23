"""Automated security tests for security/isolation_guard.py.

STRICT ISOLATION GUARANTEE:
Every test uses `tmp_path` SQLite ledgers, `tmp_path` live status JSON, synthetic keys,
and mocked network/netsh calls. NEVER touches `security/ip_ledger.sqlite3` or Google APIs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
import socks

from core.validators import sha256_file
from security.isolation_guard import (
    IsolationGuard,
    KeyLockViolationError,
    NetworkIsolationError,
    build_proxy_httplib2_http,
    get_active_windows_wifi_info,
    parse_strict_public_ipv4,
    query_public_ipv4,
)


def _make_fake_key(tmp_path: Path, name: str, email: str) -> tuple[Path, str]:
    p = tmp_path / f"{name}.json"
    p.write_text(
        json.dumps({"type": "service_account", "client_email": email, "private_key": "FAKE_TEST_ONLY"}),
        encoding="utf-8",
    )
    return p, sha256_file(p)


def _make_account_profile(
    account_id: str,
    key_path: Path,
    email: str,
    key_sha: str,
    mode: str = "router_reboot",
    strict_subnet_block: bool = True,
    require_fresh_ip_every_run: bool = False,
    dedicated_proxy_url: str = "",
    required_wifi_ssid: str = "",
) -> dict:
    return {
        "account_id": account_id,
        "publish_mode": "isolated_auto_publish",
        "default_track": "draft",
        "network_isolation": {
            "mode": mode,
            "strict_subnet_block": strict_subnet_block,
            "require_fresh_ip_every_run": require_fresh_ip_every_run,
            "dedicated_proxy_url": dedicated_proxy_url,
            "required_wifi_ssid": required_wifi_ssid,
        },
        "play_api_credentials": {
            "service_account_email": email,
            "key_file_path": str(key_path),
            "key_file_sha256": key_sha,
        },
    }


# ---------------------------------------------------------------------------
# 1. Fail-Closed IPv4-Only Validation
# ---------------------------------------------------------------------------

def test_parse_strict_public_ipv4_rejects_ipv6_private_and_malformed() -> None:
    ip, subnet = parse_strict_public_ipv4("41.44.12.90")
    assert ip == "41.44.12.90"
    assert subnet == "41.44.12.0/24"

    # IPv6 MUST fail closed
    with pytest.raises(NetworkIsolationError, match="IPv6"):
        parse_strict_public_ipv4("2001:db8::1")

    # Private / loopback IPs MUST fail closed
    for bad in ("192.168.1.10", "10.0.0.5", "127.0.0.1", "not-an-ip", ""):
        with pytest.raises(NetworkIsolationError):
            parse_strict_public_ipv4(bad)


def test_query_public_ipv4_fails_closed_when_all_endpoints_fail_or_return_ipv6() -> None:
    fake_resp_ipv6 = mock.Mock(status_code=200, text='{"ip": "2001:db8::99"}')
    with mock.patch("security.isolation_guard.requests.Session.get", return_value=fake_resp_ipv6):
        with pytest.raises(NetworkIsolationError, match="Fail-closed"):
            query_public_ipv4(endpoints=("https://api.ipify.org?format=json",))


# ---------------------------------------------------------------------------
# 2. Cross-Account Exact IP & /24 Subnet Blocking + Fresh IP Enforcement
# ---------------------------------------------------------------------------

def test_cross_account_exact_ip_and_subnet_24_hard_block(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    live = tmp_path / "guard_live_status.json"
    key_a, sha_a = _make_fake_key(tmp_path, "key_a", "dev-a@proj.iam.gserviceaccount.com")
    key_b, sha_b = _make_fake_key(tmp_path, "key_b", "dev-b@proj.iam.gserviceaccount.com")

    prof_a = _make_account_profile("account_A", key_a, "dev-a@proj.iam.gserviceaccount.com", sha_a)
    prof_b = _make_account_profile("account_B", key_b, "dev-b@proj.iam.gserviceaccount.com", sha_b, strict_subnet_block=True)

    # Account A succeeds on 198.51.100.10
    guard_a = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=lambda **kw: ("198.51.100.10", "198.51.100.0/24", "mock"),
    )
    sess_a = guard_a.acquire_verified_session(prof_a, wait_for_router_reboot=False)
    assert sess_a.verified_ip == "198.51.100.10"

    # Account B on EXACT SAME IP -> HARD BLOCK
    guard_b_same = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=lambda **kw: ("198.51.100.10", "198.51.100.0/24", "mock"),
    )
    with pytest.raises(NetworkIsolationError, match="cross_account_exact_ip"):
        guard_b_same.acquire_verified_session(prof_b, wait_for_router_reboot=False)

    # Account B on DIFFERENT IP in SAME /24 subnet (198.51.100.88) -> HARD BLOCK
    guard_b_subnet = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=lambda **kw: ("198.51.100.88", "198.51.100.0/24", "mock"),
    )
    with pytest.raises(NetworkIsolationError, match="cross_account_subnet_24"):
        guard_b_subnet.acquire_verified_session(prof_b, wait_for_router_reboot=False)


def test_require_fresh_ip_every_run_blocks_even_same_account(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    live = tmp_path / "guard_live_status.json"
    key_a, sha_a = _make_fake_key(tmp_path, "key_a", "dev-a@proj.iam.gserviceaccount.com")
    prof_a = _make_account_profile(
        "account_A", key_a, "dev-a@proj.iam.gserviceaccount.com", sha_a, require_fresh_ip_every_run=True
    )

    guard = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=lambda **kw: ("203.0.113.5", "203.0.113.0/24", "mock"),
    )
    guard.acquire_verified_session(prof_a, wait_for_router_reboot=False)

    # Second run on same IP MUST be blocked when require_fresh_ip_every_run=True
    with pytest.raises(NetworkIsolationError, match="require_fresh_ip_every_run"):
        guard.acquire_verified_session(prof_a, wait_for_router_reboot=False)


# ---------------------------------------------------------------------------
# 3. Router Reboot Polling + Live Status JSON Verification
# ---------------------------------------------------------------------------

def test_router_reboot_polls_writes_live_status_and_unlocks_on_clean_ip(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    live = tmp_path / "guard_live_status.json"
    key_a, sha_a = _make_fake_key(tmp_path, "key_a", "dev-a@proj.iam.gserviceaccount.com")
    key_b, sha_b = _make_fake_key(tmp_path, "key_b", "dev-b@proj.iam.gserviceaccount.com")

    prof_a = _make_account_profile("account_A", key_a, "dev-a@proj.iam.gserviceaccount.com", sha_a)
    prof_b = _make_account_profile("account_B", key_b, "dev-b@proj.iam.gserviceaccount.com", sha_b)

    # Seed account_A on 198.51.100.10
    IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=lambda **kw: ("198.51.100.10", "198.51.100.0/24", "mock"),
    ).acquire_verified_session(prof_a, wait_for_router_reboot=False)

    # Simulate router reboot for account_B:
    # attempt 1 -> blocked IP (198.51.100.10)
    # attempt 2 -> router offline (NetworkIsolationError)
    # attempt 3 -> fresh clean IP (203.0.113.77)
    responses = iter(
        [
            ("198.51.100.10", "198.51.100.0/24", "mock"),
            NetworkIsolationError("router offline"),
            ("203.0.113.77", "203.0.113.0/24", "mock"),
        ]
    )
    observed_states: list[dict] = []

    def fake_fetcher(**kw):
        nxt = next(responses)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def capture_sleep(_sec: float) -> None:
        observed_states.append(json.loads(live.read_text(encoding="utf-8")))

    guard_b = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        poll_interval_seconds=5.0,
        ip_fetcher=fake_fetcher,
        sleep_fn=capture_sleep,
    )
    sess_b = guard_b.acquire_verified_session(prof_b, wait_for_router_reboot=True, max_reboot_wait_seconds=30.0)

    assert sess_b.verified_ip == "203.0.113.77"
    assert len(observed_states) == 2
    assert observed_states[0]["state"] == "waiting_for_router_reboot"
    assert observed_states[0]["current_ip"] == "198.51.100.10"
    assert observed_states[0]["block_reason"] == "cross_account_exact_ip"
    assert observed_states[1]["block_reason"] == "router_reconnecting"

    final_live = json.loads(live.read_text(encoding="utf-8"))
    assert final_live["state"] == "verified"
    assert final_live["current_ip"] == "203.0.113.77"


# ---------------------------------------------------------------------------
# 4. Dedicated Network Mode: Proxy Enforcement (ZERO Direct Fallback) & Wi-Fi SSID
# ---------------------------------------------------------------------------

def test_dedicated_proxy_enforces_proxy_on_ip_check_and_httplib2_without_fallback(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    live = tmp_path / "guard_live_status.json"
    key_p, sha_p = _make_fake_key(tmp_path, "key_p", "proxy-dev@proj.iam.gserviceaccount.com")
    proxy_url = "socks5h://user1:secretpass@127.0.0.1:1080"
    prof = _make_account_profile(
        "account_proxy",
        key_p,
        "proxy-dev@proj.iam.gserviceaccount.com",
        sha_p,
        mode="dedicated_network",
        dedicated_proxy_url=proxy_url,
    )

    # Verify proxy_url is strictly passed to ip_fetcher (no direct fallback)
    captured_proxy_arg: list[str | None] = []

    def proxy_ip_fetcher(**kw):
        captured_proxy_arg.append(kw.get("proxy_url"))
        return ("198.51.100.200", "198.51.100.0/24", "mock-proxy")

    guard = IsolationGuard(ledger_db_path=db, live_status_path=live, ip_fetcher=proxy_ip_fetcher)
    session = guard.acquire_verified_session(prof)

    assert captured_proxy_arg == [proxy_url]
    assert session.proxy_url_redacted == "socks5h://user1:***@127.0.0.1:1080"
    assert session.http_transport is not None
    assert session.http_transport.proxy_info is not None
    assert session.http_transport.proxy_info.proxy_type == socks.PROXY_TYPE_SOCKS5
    assert session.http_transport.proxy_info.proxy_host == "127.0.0.1"
    assert session.http_transport.proxy_info.proxy_port == 1080

    # Verify failing proxy FAILS CLOSED (never falls back to direct requests)
    with mock.patch(
        "security.isolation_guard.requests.Session.get",
        side_effect=ConnectionError("Proxy unreachable"),
    ) as mock_get:
        with pytest.raises(NetworkIsolationError, match="dedicated proxy"):
            query_public_ipv4(endpoints=("https://api.ipify.org?format=json",), proxy_url=proxy_url)
        # Confirm proxies dict was passed on the call
        _, kwargs = mock_get.call_args
        assert kwargs["proxies"] == {"http": proxy_url, "https": proxy_url}


def test_dedicated_wifi_ssid_blocks_mismatch_and_allows_exact_match(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    live = tmp_path / "guard_live_status.json"
    key_w, sha_w = _make_fake_key(tmp_path, "key_w", "wifi-dev@proj.iam.gserviceaccount.com")
    prof = _make_account_profile(
        "account_wifi",
        key_w,
        "wifi-dev@proj.iam.gserviceaccount.com",
        sha_w,
        mode="dedicated_network",
        required_wifi_ssid="Client_Pixel_Hotspot_03",
    )

    # Mismatch SSID -> HARD BLOCK immediately before IP lookup
    ip_called = False

    def should_not_be_called(**kw):
        nonlocal ip_called
        ip_called = True
        return ("203.0.113.9", "203.0.113.0/24", "mock")

    guard_bad = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=should_not_be_called,
        wifi_inspector=lambda: {"connected": True, "state": "connected", "ssid": "Home_WiFi_Main", "adapter_name": "Wi-Fi"},
    )
    with pytest.raises(NetworkIsolationError, match="does NOT match"):
        guard_bad.acquire_verified_session(prof)
    assert ip_called is False

    # Exact SSID match -> verifies IP & succeeds
    guard_ok = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=lambda **kw: ("203.0.113.9", "203.0.113.0/24", "mock"),
        wifi_inspector=lambda: {
            "connected": True,
            "state": "connected",
            "ssid": "Client_Pixel_Hotspot_03",
            "adapter_name": "Wi-Fi",
        },
    )
    sess = guard_ok.acquire_verified_session(prof)
    assert sess.wifi_ssid_verified == "Client_Pixel_Hotspot_03"


def test_netsh_wlan_parser_ignores_bssid_and_reads_ssid() -> None:
    sample_netsh = (
        "There is 1 interface on the system:\n\n"
        "    Name                   : Wi-Fi\n"
        "    Description            : Intel Wi-Fi 6\n"
        "    State                  : connected\n"
        "    SSID                   : Client_Hotspot_Alpha\n"
        "    BSSID                  : aa:bb:cc:dd:ee:ff\n"
    )
    fake_proc = mock.Mock(returncode=0, stdout=sample_netsh, stderr="")
    with mock.patch("security.isolation_guard.subprocess.run", return_value=fake_proc):
        info = get_active_windows_wifi_info()
    assert info["connected"] is True
    assert info["ssid"] == "Client_Hotspot_Alpha"
    assert info["adapter_name"] == "Wi-Fi"


# ---------------------------------------------------------------------------
# 5. Strict 1-to-1 Service Account Key Lock
# ---------------------------------------------------------------------------

def test_strict_1_to_1_service_account_key_lock_prevents_cross_account_reuse(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    live = tmp_path / "guard_live_status.json"
    key_shared, sha_shared = _make_fake_key(tmp_path, "shared_key", "same-bot@proj.iam.gserviceaccount.com")

    prof_a = _make_account_profile("account_A", key_shared, "same-bot@proj.iam.gserviceaccount.com", sha_shared)
    prof_b = _make_account_profile("account_B", key_shared, "same-bot@proj.iam.gserviceaccount.com", sha_shared)

    guard = IsolationGuard(
        ledger_db_path=db,
        live_status_path=live,
        ip_fetcher=lambda **kw: ("203.0.113.55", "203.0.113.0/24", "mock"),
    )
    guard.acquire_verified_session(prof_a, wait_for_router_reboot=False)

    # Attempting to use the same key file on account_B MUST raise KeyLockViolationError
    with pytest.raises(KeyLockViolationError, match="1-to-1 Key Lock"):
        guard.acquire_verified_session(prof_b, wait_for_router_reboot=False)

    # Attempting to use a DIFFERENT key file with the SAME service_account_email on account_B MUST ALSO fail
    key_clone = tmp_path / "cloned_email_key.json"
    key_clone.write_text(
        json.dumps({"type": "service_account", "client_email": "same-bot@proj.iam.gserviceaccount.com", "private_key": "DIFF"}),
        encoding="utf-8",
    )
    prof_b_clone = _make_account_profile(
        "account_B", key_clone, "same-bot@proj.iam.gserviceaccount.com", sha256_file(key_clone)
    )
    with pytest.raises(KeyLockViolationError, match="1-to-1 Key Lock"):
        guard.acquire_verified_session(prof_b_clone, wait_for_router_reboot=False)

    # Tampered key hash mismatch against profile MUST fail closed
    prof_a_tampered = _make_account_profile("account_A", key_shared, "same-bot@proj.iam.gserviceaccount.com", "0" * 64)
    with pytest.raises(KeyLockViolationError, match="SHA-256 mismatch"):
        guard.acquire_verified_session(prof_a_tampered, wait_for_router_reboot=False)
