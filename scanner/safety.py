"""SSRF safeguards: only public http(s) destinations may be fetched.

Every URL - including every redirect hop - is checked before a request is made:
  * scheme must be http or https, no embedded credentials
  * hostname must resolve, and *all* resolved addresses must be globally routable
    (blocks localhost, private, link-local, CGNAT, multicast, reserved, metadata IPs)

Known limitation: the check resolves DNS before the HTTP library resolves it again
(time-of-check/time-of-use). A hostile DNS server could in theory answer differently
the second time ("DNS rebinding"). For a local research tool that only fetches
URLs from feeds/sitemaps you chose, this is an acceptable residual risk.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

Resolver = Callable[[str], Iterable[str]]

BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata.google.internal", "metadata", "instance-data",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")


class UnsafeURLError(ValueError):
    """Raised when a URL points at a non-public or otherwise disallowed destination."""


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    ):
        return False
    return True


def check_url(url: str, resolver: Resolver | None = None) -> str:
    """Validate a URL for fetching. Returns the URL or raises UnsafeURLError."""
    resolver = resolver or default_resolver
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"scheme '{parts.scheme}' is not allowed (only http/https)")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no hostname")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("invalid port") from exc
    if port is not None and port not in (80, 443, 8080, 8443):
        raise UnsafeURLError(f"port {port} is not allowed")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError(f"hostname '{host}' is not public")

    # Literal IP in the URL (including decimal/hex forms that ipaddress understands).
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(str(literal)):
            raise UnsafeURLError(f"IP address {literal} is not public")
        return url
    if host.replace(".", "").isdigit() or host.startswith("0x"):
        raise UnsafeURLError(f"numeric hostname '{host}' is not allowed")

    try:
        addresses = list(resolver(host))
    except (OSError, UnicodeError) as exc:
        raise UnsafeURLError(f"DNS resolution failed for {host}: {exc}") from exc
    if not addresses:
        raise UnsafeURLError(f"DNS returned no addresses for {host}")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise UnsafeURLError(f"{host} resolves to non-public address(es): {', '.join(bad)}")
    return url


def is_safe_url(url: str, resolver: Resolver | None = None) -> bool:
    try:
        check_url(url, resolver)
        return True
    except UnsafeURLError:
        return False
