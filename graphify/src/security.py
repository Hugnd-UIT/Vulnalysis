from __future__ import annotations

from collections.abc import Mapping
import html
import http.client
import ipaddress
import os
from pathlib import Path
import re
import socket
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from graphify.paths import GRAPHIFY_OUT, GRAPHIFY_OUT_NAME


SCHEMES = {'http', 'https'}
MAX_FETCH = 52428800
MAX_TEXT = 10485760
MAX_GRAPH = 512 * 1024 * 1024
BLOCKED = {'metadata.google.internal', 'metadata.google.com'}
CGN = ipaddress.ip_network('100.64.0.0/10')
NAT64 = ipaddress.ip_network('64:ff9b::/96')


def cap() -> int:
    raw = os.environ.get('GRAPHIFY_MAX_GRAPH_BYTES', '').strip()

    if not raw:
        return MAX_GRAPH

    text = raw.upper()
    multiplier = 1

    if text.endswith('GB'):
        multiplier = 1024 * 1024 * 1024
        text = text[:-2].strip()
    elif text.endswith('MB'):
        multiplier = 1024 * 1024
        text = text[:-2].strip()

    try:
        val = int(text)
    except ValueError:
        return MAX_GRAPH

    if val <= 0:
        return MAX_GRAPH

    return val * multiplier


def blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip in NAT64:
        ip = ipaddress.ip_address(int(ip) & 4294967295)

    return ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local or (ip in CGN)


def validate(url: str) -> str:
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme.lower() not in SCHEMES:
        raise ValueError(f"Blocked URL scheme '{parsed.scheme}'. Got: {url!r}")

    host = parsed.hostname

    if host:
        if host.lower() in BLOCKED:
            raise ValueError(f"Blocked cloud metadata endpoint '{host}'. Got: {url!r}")

        try:
            infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)

            for info in infos:
                addr = info[4][0]
                ip = ipaddress.ip_address(addr)

                if blocked(ip):
                    raise ValueError(f"Blocked IP {addr} for '{host}'. Got: {url!r}")
        except socket.gaierror as exc:
            raise ValueError(f"DNS failed for '{host}': {exc}. Got: {url!r}") from exc

    return url


def resolve(host: str, port: int) -> tuple[int, str]:
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)

    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]

        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue

        if blocked(ip):
            raise OSError(f"SSRF blocked: IP {addr} resolved from '{host}' is private")

        return (family, addr)

    raise OSError(f"SSRF blocked: no address for '{host}'")


class HTTP(http.client.HTTPConnection):

    def connect(self) -> None:
        family, ip = resolve(self.host, self.port)
        self.sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)

        if self._tunnel_host:
            self._tunnel()


class HTTPS(http.client.HTTPSConnection):

    def connect(self) -> None:
        family, ip = resolve(self.host, self.port)
        sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)

        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            sock = self.sock

        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class HTTPHandler(urllib.request.HTTPHandler):
    pass


class HTTPSHandler(urllib.request.HTTPSHandler):
    pass


class Redirect(urllib.request.HTTPRedirectHandler):
    pass


def redirect(self, req, fp, code, msg, headers, newurl):
    validate(newurl)

    return super(Redirect, self).redirect_request(req, fp, code, msg, headers, newurl)


setattr(HTTPHandler, 'http_open', lambda self, req: self.do_open(HTTP, req))
setattr(HTTPSHandler, 'https_open', lambda self, req: self.do_open(HTTPS, req))
setattr(Redirect, 'redirect_request', redirect)


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(HTTPHandler, HTTPSHandler, Redirect)


def fetch(url: str, limit: int = MAX_FETCH, timeout: int = 30) -> bytes:
    validate(url)
    op = opener()
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 graphify/1.0'})

    with op.open(req, timeout=timeout) as resp:
        status = getattr(resp, 'status', None) or getattr(resp, 'code', None)

        if status is not None and not 200 <= status < 300:
            raise urllib.error.HTTPError(url, status, f'HTTP {status}', {}, None)

        chunks: list[bytes] = []
        total = 0

        while True:
            chunk = resp.read(65536)

            if not chunk:
                break

            total += len(chunk)

            if total > limit:
                raise OSError(f'Response exceeds size limit: {limit}')

            chunks.append(chunk)

    return b''.join(chunks)


def text(url: str, limit: int = MAX_TEXT, timeout: int = 15) -> str:
    raw = fetch(url, limit=limit, timeout=timeout)

    return raw.decode('utf-8', errors='replace')


def path(target: str | Path, base: Path | None = None) -> Path:
    if base is None:
        hint = Path(target).resolve()

        for cand in [hint, *hint.parents]:
            if cand.name == GRAPHIFY_OUT_NAME:
                base = cand
                break

        if base is None:
            base = Path(GRAPHIFY_OUT).resolve()

    base = base.resolve()

    if not base.exists():
        raise ValueError(f'Graph directory does not exist: {base}')

    resolved = Path(target).resolve()

    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(f'Path escapes base directory: {base}')

    if not resolved.exists():
        raise FileNotFoundError(f'Graph file not found: {resolved}')

    return resolved


def check(loc: Path) -> None:
    max_size = cap()

    try:
        size = loc.stat().st_size
    except OSError:
        return

    if size > max_size:
        raise ValueError(f'Graph file exceeds size cap: {size}')


CONTROL = re.compile(r'[\x00-\x1f\x7f]')
MAX_LABEL = 256
MAX_VALUE = 512
MAX_ITEMS = 50


def label(val: str | None) -> str:
    if val is None:
        return ''

    clean = CONTROL.sub('', str(val))

    if len(clean) > MAX_LABEL:
        clean = clean[:MAX_LABEL]

    return clean


def string(val: object) -> str:
    clean = CONTROL.sub('', str(val))
    clean = html.escape(clean, quote=True)

    if len(clean) > MAX_VALUE:
        clean = clean[:MAX_VALUE]

    return clean


def value(val: object) -> object:
    if isinstance(val, bool):
        return val

    if isinstance(val, str):
        return string(val)

    if isinstance(val, dict):
        return metadata(val)

    if isinstance(val, (list, tuple)):
        return [value(item) for item in val[:MAX_ITEMS]]

    if isinstance(val, (int, float)) or val is None:
        return val

    return string(val)


def metadata(data: Mapping[str, Any] | None) -> dict[str, object]:
    if data is None:
        return {}

    result: dict[str, object] = {}

    for k, v in data.items():
        ckey = string(k)

        if not ckey:
            continue

        result[ckey] = value(v)

    return result