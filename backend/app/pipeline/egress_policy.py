"""Optional egress policy for operator-supplied camera sources.

When camera_source_block_private_networks is on, sources resolving to
loopback, private, link-local, reserved or multicast addresses are refused.
Off by default because camera networks live on those ranges and the endpoints
already require an authorized role.

Limits: DNS rebinding isn't prevented (FFmpeg resolves the name again when
opening); only network source types are checked; a URI without a parseable
host isn't blocked. Defence in depth, not a substitute for host egress
filtering.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from ..config import settings

# source types that open a network connection; the rest are local
NETWORK_SOURCE_TYPES = {"rtsp", "onvif", "http", "https", "sentinel_grid"}


def _candidate_host(source_uri: str) -> str | None:
    """Best-effort host, or None for a URI with no network host (file path,
    webcam index, bare grid id)."""
    if not source_uri:
        return None
    uri = source_uri.strip()
    parsed = urlparse(uri if "://" in uri else f"//{uri}", scheme="")
    host = parsed.hostname
    if not host:
        return None
    return host


def _resolved_addresses(host: str) -> list[ipaddress._BaseAddress]:
    """All addresses the host resolves to (a literal IP resolves to itself);
    [] if resolution fails."""
    try:
        ipaddress.ip_address(host)
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addresses


def _classify(address) -> str | None:
    """Why this address is off-limits, or None if it is a fine target."""
    if address.is_loopback:
        return "a loopback address"
    if address.is_link_local:
        return "a link-local address (including cloud metadata endpoints)"
    if address.is_private:
        return "a private network address"
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return "a reserved, multicast or unspecified address"
    return None


def blocked_reason(source_type: str, source_uri: str) -> str | None:
    """Reason this source must not be opened, or None.

    Any off-limits address refuses the source, including a name that resolves
    to both public and private addresses.
    """
    if not settings.camera_source_block_private_networks:
        return None
    if (source_type or "").lower() not in NETWORK_SOURCE_TYPES:
        return None
    host = _candidate_host(source_uri)
    if host is None:
        return None
    for address in _resolved_addresses(host):
        reason = _classify(address)
        if reason is not None:
            return f"'{host}' resolves to {reason} ({address}), which this deployment does not permit as a camera source."
    return None
