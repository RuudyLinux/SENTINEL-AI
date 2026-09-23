"""Optional egress policy for operator-supplied camera sources (workstream C3).

`docs/THREAT_MODEL.md` recorded camera SSRF as mitigated by role-gating only:
an authorized operator could still point the backend at any internal host.
Active probing during the debugging pass confirmed the *information* side is
sound — loopback, 0.0.0.0, link-local, IPv6 loopback and malformed URIs all
returned an identical bounded failure after exactly the configured timeout, so
nothing distinguishes "something is listening" from "nothing is" — but reach
itself was never restricted.

This adds the restriction as an OPT-IN policy
(`settings.camera_source_block_private_networks`, default False). Default-off
is deliberate: a great many real deployments run cameras on exactly the private
RANGES this would block (a police LAN is not the public internet), so defaulting
it on would break working installations to defend against an already-authorized
user. A hardened deployment — one where the backend must never reach internal
infrastructure — turns it on.

Honest limits, stated rather than implied:

- **DNS rebinding is not prevented.** The host is resolved here, then resolved
  again by FFmpeg when the stream is actually opened. A name that answers
  publicly now and privately a moment later defeats this check. Closing that
  needs connection-time enforcement (resolve once, connect to the pinned
  address), which OpenCV's capture API gives no hook for.
- **Only network source types are examined.** `webcam`, `video_file` and
  `mock_vms` reach no network, so there is nothing to police.
- **An unparseable or hostless URI is not blocked**, because it never yields a
  target to connect to; the existing open-timeout path already handles it.

This is defence in depth on top of the role gate, not a replacement for
network-layer egress control on the host.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from ..config import settings

# Source types that actually open a network connection. Everything else is
# local hardware or a file on disk.
NETWORK_SOURCE_TYPES = {"rtsp", "onvif", "http", "https", "sentinel_grid"}


def _candidate_host(source_uri: str) -> str | None:
    """Best-effort host extraction. Returns None when the URI names no
    network host (a file path, a webcam index, a bare grid camera id)."""
    if not source_uri:
        return None
    uri = source_uri.strip()
    parsed = urlparse(uri if "://" in uri else f"//{uri}", scheme="")
    host = parsed.hostname
    if not host:
        return None
    return host


def _resolved_addresses(host: str) -> list[ipaddress._BaseAddress]:
    """Every address the host currently resolves to. A literal IP resolves to
    itself. Resolution failure yields an empty list — nothing to judge."""
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
    """Why this source must not be opened, or None to allow it.

    Returns None whenever the policy is disabled, the source type reaches no
    network, no host can be extracted, or the host resolves only to routable
    public addresses. If ANY resolved address is off-limits the source is
    refused — a name resolving to both a public and a private address is
    exactly the shape a rebinding attempt takes.
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
