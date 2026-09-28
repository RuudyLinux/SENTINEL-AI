"""Optional egress policy for operator-supplied camera sources.

docs/THREAT_MODEL.md had camera SSRF mitigated by role only: an authorized
operator could still point the backend at any internal host. Probing showed
nothing leaks (loopback, 0.0.0.0, link-local, ::1 and junk URIs all fail the
same way after the same timeout) but reach itself wasn't limited.

Opt-in via camera_source_block_private_networks, off by default: real
camera networks live on the private ranges this blocks, and the user is
already authorized. Turn it on where the backend must never reach internal
infrastructure.

Limits:
- DNS rebinding isn't prevented. We resolve here and FFmpeg resolves again
  when opening; a name that flips from public to private in between gets
  through. Fixing that needs pinning the address at connect time and
  OpenCV has no hook for it.
- Only network source types are checked (webcam, video_file and mock_vms
  don't touch the network).
- A URI with no parseable host isn't blocked; there's nothing to connect to
  and the open timeout handles it.

Defence in depth on top of the role check, not a replacement for real
egress filtering on the host.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from ..config import settings

# source types that open a network connection; the rest are local
NETWORK_SOURCE_TYPES = {"rtsp", "onvif", "http", "https", "sentinel_grid"}


def _candidate_host(source_uri: str) -> str | None:
    """Best-effort host. None when the URI has no network host (file path,
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
    """All addresses the host resolves to (a literal IP is itself). Failure
    gives [], nothing to judge."""
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
    """Why this source must not be opened, or None.

    None when the policy is off, the type has no network, there's no host,
    or every address is public. Any off-limits address refuses it; a name
    resolving to both public and private is what rebinding looks like.
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
