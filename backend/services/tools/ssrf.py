"""HTTP SSRF guards for tool HTTP ops and other outbound fetches."""

from __future__ import annotations

import ipaddress
import re
import socket

from settings import get_settings

# ───────────────────────────── HTTP SSRF guard ──────────────────────────────


def _ssrf_allow_hosts() -> set[str]:
    """Trusted internal hosts that bypass the private-IP check (e.g. an on-prem
    integration the tenant explicitly trusts). Default empty → no bypass. Set via
    ``talqing_ssrf_allow_hosts`` in the env config YAML (comma-separated)."""
    return set(get_settings().ssrf_allow_host_set)


def _reject_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in cgnat)
    ):
        raise ValueError(f"destination {ip} is not allowed (private/internal)")


def assert_public_ip(ip_str: str) -> None:
    """Raise ValueError if `ip_str` is a private/internal/metadata address. The
    public counterpart of `_reject_ip`, for callers that already hold a resolved
    peer IP (e.g. a headless-browser navigation's server address) and want to
    reject a DNS-rebind to an internal host the connection-time pin can't cover."""
    _reject_ip(ipaddress.ip_address(ip_str))


def _parse_http_host(url: str) -> str:
    m = re.match(r"^(https?)://([^/:?#]+)", url, re.I)
    if not m:
        raise ValueError("HTTP operation URL must be http(s) with a host")
    return m.group(2)


def check_url(url: str) -> None:
    """Block non-http(s) schemes and private/internal/metadata destinations.
    Synchronous (blocking DNS) — only call off the event loop; async callers use
    check_url_async / resolve_and_pin. Raises ValueError."""
    host = _parse_http_host(url)
    if host.lower() in _ssrf_allow_hosts():
        return  # explicitly trusted host — skip the private-range rejection
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"could not resolve host {host!r}") from e
    for info in infos:
        _reject_ip(ipaddress.ip_address(info[4][0]))


async def check_url_async(url: str) -> None:
    """check_url with the DNS lookup off the event loop (the worker runs this
    mid-call — a slow resolver must not stall the audio pipeline)."""
    host = _parse_http_host(url)
    if host.lower() in _ssrf_allow_hosts():
        return
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"could not resolve host {host!r}") from e
    for info in infos:
        _reject_ip(ipaddress.ip_address(info[4][0]))


async def resolve_and_pin(url: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Async-resolve the URL's host, reject private/internal IPs, and PIN the
    connection to a vetted IP so a second (rebinding) DNS answer can't redirect
    the actual request to an internal address (closes the check-then-fetch
    TOCTOU). Returns (pinned_url, extra_headers, request_extensions):
      - pinned_url swaps the hostname for the vetted IP,
      - Host header carries the original authority,
      - the sni_hostname extension keeps TLS SNI + certificate verification
        against the original hostname (httpx/httpcore support).
    Allowlisted hosts are returned unpinned. Raises ValueError."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("HTTP operation URL must be http(s) with a host")
    host = parts.hostname
    if host.lower() in _ssrf_allow_hosts():
        return url, {}, {}

    import asyncio

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as e:
        raise ValueError(f"could not resolve host {host!r}") from e
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        _reject_ip(ip)
        ips.append(ip)
    if not ips:
        raise ValueError(f"could not resolve host {host!r}")

    pin = ips[0]
    ip_literal = f"[{pin}]" if pin.version == 6 else str(pin)
    netloc = ip_literal + (f":{parts.port}" if parts.port else "")
    pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    # Host = hostname[:port] only — never netloc, which may carry userinfo
    host_header = (f"[{host}]" if ":" in host else host) + (f":{parts.port}" if parts.port else "")
    headers = {"Host": host_header}
    extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
    return pinned, headers, extensions
