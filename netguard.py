"""Which addresses the chat browser may open: the floors of Hermes's own browser URL policy.

Every page load and every redirect hop is checked here before the browser sends it (see
``browser.Page``'s guard). A host is resolved and refused when any address it resolves to is
private, loopback, link-local, CGNAT, reserved or multicast, or is a cloud metadata endpoint. The
user's own Hermes opt-out (``security.allow_private_urls``, legacy ``browser.allow_private_urls``,
or ``HERMES_ALLOW_PRIVATE_URLS``) lifts the private-address floor, never the metadata and
link-local one, exactly as in Hermes.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.goog"})
METADATA_IPS = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "169.254.169.253", "100.100.100.200", "fd00:ec2::254"))
LINK_LOCAL = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"))
CGNAT = ipaddress.ip_network("100.64.0.0/10")
PRIVATE_SUFFIXES = (".localhost", ".local", ".lan", ".internal")
_TTL_S = 30.0

_cache: dict[tuple[str, bool], tuple[float, Optional[str]]] = {}
_cache_lock = threading.Lock()


def private_allowed() -> bool:
    """The user's own Hermes opt-out of private-address blocking."""
    env = os.environ.get("HERMES_ALLOW_PRIVATE_URLS", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return False
    for section in ("security", "browser"):
        value = (cfg.get(section) or {}).get("allow_private_urls")
        if value is True or str(value).strip().lower() in ("1", "true", "yes"):
            return True
    return False


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def _ip_refusal(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address", allow_private: bool) -> Optional[str]:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip in METADATA_IPS:
        return "a cloud metadata address"
    if any(ip in net for net in LINK_LOCAL if net.version == ip.version):
        return "a link-local address"
    if allow_private:
        return None
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_private or ip.is_reserved or ip.is_multicast or ip.is_unspecified or \
            (ip.version == 4 and ip in CGNAT):
        return "a private address"
    return None


def _host_refusal(host: str, allow_private: bool) -> Optional[str]:
    if host in METADATA_HOSTS:
        return "a cloud metadata host"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return _ip_refusal(literal, allow_private)
    if not allow_private and (host == "localhost" or host.endswith(PRIVATE_SUFFIXES)):
        return "a private host name"
    try:
        addresses = _resolve(host)
    except (socket.gaierror, UnicodeError, OSError):
        return "a host that does not resolve"
    if not addresses:
        return "a host that does not resolve"
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return "a host with an unreadable address"
        why = _ip_refusal(ip, allow_private)
        if why is not None:
            return f"{why} ({raw})"
    return None


def refusal(url: str) -> Optional[str]:
    """Why the browser must not open ``url``, or None when it may. Only http(s) to a public host
    passes; ``about:``, ``data:`` and ``blob:`` URLs carry no host and are left to the browser."""
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme in ("about", "data", "blob"):
            return None
        if scheme not in ("http", "https"):
            return f"{url[:80]} is not an http(s) address"
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return f"{url[:80]} is not a readable address"
    if not host:
        return f"{url[:80]} has no host"
    allow_private = private_allowed()
    key = (host, allow_private)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and now - hit[0] < _TTL_S:
        why = hit[1]
    else:
        why = _host_refusal(host, allow_private)
        with _cache_lock:
            _cache[key] = (now, why)
    return f"{host} is {why}" if why else None
