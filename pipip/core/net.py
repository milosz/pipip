"""URL policy helpers used by HTTP sources and notifiers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_HTTP_SCHEMES = frozenset({"http", "https"})


class UrlNotAllowed(ValueError):
    """Raised when a URL is outside the configured network policy."""


def _blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


async def ensure_url_allowed(
    url: str,
    *,
    allow_private: bool = False,
    schemes: frozenset[str] = _HTTP_SCHEMES,
) -> None:
    """Validate scheme and host; block private networks unless allowed."""

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in schemes:
        raise UrlNotAllowed(f"unsupported URL scheme {scheme!r} in {url}")
    host = parsed.hostname
    if not host:
        raise UrlNotAllowed(f"missing host in {url}")
    if allow_private:
        return

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                host,
                parsed.port,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise UrlNotAllowed(f"cannot resolve host {host!r}") from error
        addresses = {
            ipaddress.ip_address(info[4][0]) for info in infos
        }
    else:
        addresses = {address}

    blocked = [str(address) for address in addresses if _blocked_address(address)]
    if blocked:
        raise UrlNotAllowed(
            f"host {host!r} resolves to a private/blocked address: "
            f"{', '.join(sorted(blocked))}"
        )
