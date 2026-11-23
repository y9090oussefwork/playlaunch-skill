"""PlayLaunch Studio security & account isolation package.

Phase 3 Implementation Note (Isolation Guard):
- security/isolation_guard.py MUST query IPv4-explicit public IP endpoints
  (e.g. https://api.ipify.org?format=json, https://ipv4.icanhazip.com, https://v4.ident.me)
  configured in studio.config.json -> security.ipv4_check_endpoints to guarantee accurate
  IPv4 /24 subnet derivation and cross-account /24 blocking.
"""
