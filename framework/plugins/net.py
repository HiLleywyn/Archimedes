"""framework/plugins/net.py -- the SSRF-guarded HTTP client behind `arch.http`.

A plugin gets outbound HTTP through this module and nowhere else. Before any
request leaves the process the target is validated:

* the scheme must be ``http`` or ``https``,
* the URL may not carry userinfo,
* every IP the host resolves to is checked, and a request to a private,
  loopback, link-local, multicast or otherwise non-public address is refused.

Redirects are followed by hand so the ``Location`` scheme of every hop is
validated the same way -- a public URL cannot bounce a plugin onto an
internal one.

For a host given by name the address guard runs inside a custom
:class:`GuardedResolver`, so the IP ``aiohttp`` connects to is the exact IP
that was checked -- there is no second DNS lookup and so no rebinding window.
A host given as a bare IP literal never reaches a resolver, so it is checked
directly before the request.

:func:`is_blocked_ip` and :func:`validate_url` are pure -- the offline test
suite exercises the guard logic through them with no network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from config import Config

log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")
_REDIRECT_CODES = (301, 302, 303, 307, 308)
# Redirects that downgrade the method to a bodyless GET. 307/308 preserve it.
_GET_REDIRECTS = (301, 302, 303)


class HttpError(Exception):
    """A request was refused by the guard or could not be completed."""


def is_blocked_ip(ip_str: str) -> bool:
    """True when ``ip_str`` is an address a plugin must never reach.

    Unparseable input is treated as blocked -- failing closed is the only
    safe default for a security check.
    """
    try:
        ip = ipaddress.ip_address(str(ip_str).strip())
    except ValueError:
        return True
    # An IPv4-mapped IPv6 address (``::ffff:10.0.0.1``) is just an IPv4 host
    # wearing a disguise -- judge it by the address it actually maps to.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def validate_url(url: str) -> tuple[str, str, int | None]:
    """Validate one URL's shape, returning ``(scheme, host, port)``.

    Raises :class:`HttpError` for a non-HTTP scheme, a missing host, or a URL
    that smuggles credentials in its userinfo. This is a pure check -- it does
    no DNS and so is safe and cheap to unit-test.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError as exc:
        raise HttpError(f"that URL could not be parsed: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise HttpError(f"only http and https URLs are allowed, not {scheme!r}")
    if parts.username or parts.password:
        raise HttpError("URLs with embedded credentials are not allowed")
    host = parts.hostname
    if not host:
        raise HttpError("that URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise HttpError("that URL has an invalid port") from exc
    return scheme, host, port


def reject_blocked_literal(host: str) -> None:
    """Refuse a host that is a bare, non-public IP literal.

    ``aiohttp`` connects straight to an IP-literal host without ever calling
    a resolver, so :class:`GuardedResolver` never sees it. A literal is
    checked here instead. A host given by name is left for the resolver and
    passes through untouched.
    """
    if Config.PLUGIN_HTTP_ALLOW_PRIVATE:
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return  # a name, not a literal -- GuardedResolver judges it later
    if is_blocked_ip(host):
        raise HttpError(f"{host!r} is a non-public address and is blocked")


class GuardedResolver(AbstractResolver):
    """An ``aiohttp`` resolver that refuses to hand back a non-public address.

    Pinning every connection to addresses this resolver has already cleared
    closes the gap between validation and connect: ``aiohttp`` dials the exact
    IPs returned here and resolves the host nowhere else, so a name cannot be
    rebound to an internal address after the check.

    If any address a host resolves to is blocked, the whole host is refused --
    a name that round-robins a public and a private record cannot slip a
    request through on a lucky draw.
    """

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET,
    ) -> list[dict]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM,
            )
        except (socket.gaierror, OSError) as exc:
            raise HttpError(f"could not resolve {host!r}") from exc
        results: list[dict] = []
        for fam, _type, proto, _canon, sockaddr in infos:
            addr = sockaddr[0]
            if not Config.PLUGIN_HTTP_ALLOW_PRIVATE and is_blocked_ip(addr):
                raise HttpError(
                    f"{host!r} resolves to a non-public address and is blocked"
                )
            results.append({
                "hostname": host,
                "host": addr,
                "port": sockaddr[1],
                "family": fam,
                "proto": proto,
                "flags": socket.AI_NUMERICHOST,
            })
        if not results:
            raise HttpError(f"could not resolve {host!r}")
        return results

    async def close(self) -> None:
        return None


def _headers_to_dict(headers) -> dict:
    """Collapse a response's headers into a plain lower-cased dict."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[str(key).lower()] = str(value)
    return out


async def fetch(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: str | None = None,
    json_body=None,
    timeout: float = 10.0,
    max_bytes: int = 1048576,
    max_redirects: int = 3,
) -> dict:
    """Perform one guarded HTTP request and return a plain-Python result dict.

    The result is always a dict with ``ok`` / ``status`` / ``body`` / ``json``
    / ``headers`` / ``error`` keys -- a refused or failed request comes back
    as ``{"ok": False, ..., "error": "<why>"}`` rather than raising, so a
    plugin handler never has to wrap calls in ``pcall``.
    """
    method = str(method or "GET").upper()
    request_headers = {
        str(k): str(v) for k, v in (headers or {}).items()
    }
    client_timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout)))
    current_url = str(url or "")
    current_method = method
    current_body = body
    current_json = json_body

    connector = aiohttp.TCPConnector(resolver=GuardedResolver())
    try:
        async with aiohttp.ClientSession(
            timeout=client_timeout, connector=connector,
        ) as session:
            for _hop in range(max(0, int(max_redirects)) + 1):
                # Scheme and userinfo are checked here, and an IP-literal host
                # directly; a named host is guarded in GuardedResolver when
                # the connection opens.
                _scheme, host, _port = validate_url(current_url)
                reject_blocked_literal(host)
                kwargs: dict = {"headers": request_headers,
                                "allow_redirects": False}
                if current_json is not None:
                    kwargs["json"] = current_json
                elif current_body is not None:
                    kwargs["data"] = str(current_body)
                async with session.request(
                    current_method, current_url, **kwargs,
                ) as resp:
                    if resp.status in _REDIRECT_CODES:
                        location = resp.headers.get("Location")
                        if location:
                            current_url = urljoin(current_url, location)
                            if resp.status in _GET_REDIRECTS:
                                current_method = "GET"
                                current_body = None
                                current_json = None
                            continue
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise HttpError(
                                f"response exceeded {max_bytes} bytes"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    text = raw.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(text)
                    except (ValueError, TypeError):
                        parsed = None
                    return {
                        "ok": resp.status < 400,
                        "status": resp.status,
                        "body": text,
                        "json": parsed,
                        "headers": _headers_to_dict(resp.headers),
                        "error": None,
                    }
        raise HttpError(f"too many redirects (over {max_redirects})")
    except HttpError as exc:
        return _error_result(str(exc))
    except aiohttp.ClientError as exc:
        return _error_result(f"request failed: {exc}")
    except asyncio.TimeoutError:
        return _error_result(f"request timed out after {timeout}s")


def _error_result(message: str) -> dict:
    return {
        "ok": False, "status": 0, "body": "", "json": None,
        "headers": {}, "error": message,
    }
