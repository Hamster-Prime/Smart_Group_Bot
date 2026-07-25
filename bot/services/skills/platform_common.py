from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
import threading
import weakref
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, TypeVar
from urllib.parse import urljoin, urlparse

import aiohttp

from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_META_RE = re.compile(
    r'(?is)<meta[^>]+(?:name|property)=["\'](?P<name>[^"\']+)["\'][^>]+content=["\'](?P<content>.*?)["\'][^>]*>'
)
_JSON_LD_RE = re.compile(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>')
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_URL_RE = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')
_HTTP_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "cookie2",
        "api-key",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
        "x-csrf-token",
        "x-xsrf-token",
    }
)
_DEFAULT_ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "application/xml",
    "text/xml",
)
_DNS_TIMEOUT_SEC = 3.0
_DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_DECODED_BYTES = 4 * 1024 * 1024
_NAT64_WELL_KNOWN_NETWORK = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_USE_NETWORK = ipaddress.ip_network("64:ff9b:1::/48")
_THREAD_LIMITS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Semaphore],
] = weakref.WeakKeyDictionary()
_T = TypeVar("_T")


class InvalidJsonResponseError(ValueError):
    def __init__(
        self,
        *,
        url: str,
        status: int,
        content_type: str,
        body_preview: str,
    ) -> None:
        self.url = clean_text(url, max_len=300)
        self.status = int(status or 0)
        self.content_type = clean_text(content_type, max_len=120)
        self.body_preview = clean_multiline_text(body_preview, max_len=240)
        message = (
            f"invalid_json_response status={self.status} "
            f"content_type={self.content_type or '-'} "
            f"url={self.url or '-'} "
            f"preview={self.body_preview or '(empty)'}"
        )
        super().__init__(message)


class UnsafeUrlError(ValueError):
    """Raised when an outbound URL can reach a non-public network target."""


class ResponseTooLargeError(ValueError):
    """Raised before an HTTP response can exceed the configured memory budget."""


class UnsupportedContentTypeError(ValueError):
    """Raised when a fetch endpoint returns binary or otherwise unexpected data."""


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    host: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _HopResponse:
    status: int
    text: str
    url: str
    content_type: str
    location: str = ""


@dataclass(frozen=True, slots=True)
class _HopBytesResponse:
    status: int
    body: bytes
    url: str
    content_type: str
    location: str = ""


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolve one host only to the exact public IPs validated before connect."""

    def __init__(self, target: _ResolvedTarget) -> None:
        self._target = target

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[aiohttp.abc.ResolveResult]:
        normalized = _normalize_hostname(host)
        if normalized != self._target.host:
            raise OSError(f"unexpected DNS lookup for {host}")
        results: list[aiohttp.abc.ResolveResult] = []
        for address in self._target.addresses:
            ip = ipaddress.ip_address(address)
            address_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            results.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port or self._target.port,
                    "family": address_family,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise OSError(f"no validated address for {host}")
        return results

    async def close(self) -> None:
        return None


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def extract_first_url(text: str) -> str:
    match = _URL_RE.search(text or "")
    return match.group(0).strip() if match else ""


def _normalize_hostname(host: str) -> str:
    value = (host or "").strip().rstrip(".").lower()
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def normalize_host(url: str) -> str:
    try:
        host = _normalize_hostname(urlparse(url).hostname or "")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def host_matches(url: str, allowed_hosts: Iterable[str]) -> bool:
    host = normalize_host(url)
    normalized = {
        _normalize_hostname(clean_text(str(item or ""), max_len=253)).removeprefix("www.")
        for item in allowed_hosts or []
        if str(item or "").strip()
    }
    if not normalized:
        return True
    return bool(host) and any(
        host == candidate or host.endswith(f".{candidate}") for candidate in normalized if candidate
    )


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Several IPv6 transition formats tunnel or synthesize an IPv4
        # destination.  Python's classification of these ranges has changed
        # between releases, so validate the effective IPv4 endpoint explicitly
        # instead of relying only on ``IPv6Address.is_global``.
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip in _NAT64_WELL_KNOWN_NETWORK:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        # RFC 8215 reserves this prefix for network-specific translation. Its
        # IPv4 bit placement depends on the configured prefix length, which is
        # not knowable here, so fail closed for outbound untrusted URLs.
        if ip in _NAT64_LOCAL_USE_NETWORK:
            return False
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            teredo_server, teredo_client = ip.teredo
            embedded.extend((teredo_server, teredo_client))
        if any(not _is_public_ip(str(candidate)) for candidate in embedded):
            return False

    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_unspecified
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
        and not ip.is_reserved
    )


def _validate_url_syntax(url: str, *, allowed_hosts: Iterable[str] = ()) -> tuple[str, int]:
    raw_url = (url or "").strip()
    if not raw_url or any(ord(char) <= 32 or ord(char) == 127 for char in raw_url):
        raise UnsafeUrlError("invalid_url")
    try:
        parsed = urlparse(raw_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeUrlError("invalid_url") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise UnsafeUrlError("invalid_url")
    if "\\" in parsed.netloc:
        raise UnsafeUrlError("invalid_url")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("url_credentials_not_allowed")
    host = _normalize_hostname(parsed.hostname or "")
    if not host or len(host) > 253:
        raise UnsafeUrlError("invalid_host")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrlError("non_public_host")
    if allowed_hosts and not host_matches(url, allowed_hosts):
        raise UnsafeUrlError("host_not_allowed")
    resolved_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    if not 1 <= resolved_port <= 65535:
        raise UnsafeUrlError("invalid_port")
    return host, resolved_port


def _resolve_host_sync(host: str, port: int) -> tuple[str, ...]:
    rows = socket.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for family, _socktype, _proto, _canonname, sockaddr in rows:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            continue
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _thread_limit(name: str, capacity: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limits = _THREAD_LIMITS.setdefault(loop, {})
    return limits.setdefault(name, asyncio.Semaphore(max(1, int(capacity))))


def _cancel_acquire_and_return_late_permit(
    acquire_task: asyncio.Task[bool],
    semaphore: asyncio.Semaphore,
) -> None:
    """Retire an acquire task and return a permit won in the cancel race.

    Cancellation can be delivered to the owner in the same event-loop turn in
    which ``Semaphore.acquire`` completes.  In that case cancelling the already
    completed child is a no-op and the permit would otherwise be leaked.  A
    custom acquire implementation may also suppress cancellation, so observe a
    later successful completion as well.
    """

    def _finished(done: asyncio.Task[bool]) -> None:
        if done.cancelled():
            return
        try:
            done.result()
        except (asyncio.CancelledError, Exception):
            return
        semaphore.release()

    acquire_task.cancel()
    if acquire_task.done():
        _finished(acquire_task)
    else:
        acquire_task.add_done_callback(_finished)


async def _run_bounded_daemon_thread(
    call: Callable[[], _T],
    *,
    timeout_sec: float,
    limit_name: str,
    capacity: int,
) -> _T:
    """Run blocking legacy code without creating interpreter-blocking workers.

    A timeout cannot kill a Python thread.  The thread is therefore daemonized,
    while its concurrency permit remains held until it actually exits.  A
    permanently stuck dependency can consume at most ``capacity`` threads;
    later calls time out waiting for a permit and process shutdown is not held
    hostage by ``ThreadPoolExecutor``'s mandatory atexit join.
    """

    loop = asyncio.get_running_loop()
    timeout = max(0.01, float(timeout_sec))
    deadline = loop.time() + timeout
    semaphore = _thread_limit(limit_name, capacity)
    acquire_task = asyncio.create_task(semaphore.acquire())
    try:
        done, _pending = await asyncio.wait({acquire_task}, timeout=timeout)
    except asyncio.CancelledError:
        _cancel_acquire_and_return_late_permit(acquire_task, semaphore)
        raise
    if acquire_task not in done:
        _cancel_acquire_and_return_late_permit(acquire_task, semaphore)
        raise TimeoutError
    await acquire_task

    future: asyncio.Future[_T] = loop.create_future()

    def _finish(*, result: _T | None = None, error: BaseException | None = None) -> None:
        semaphore.release()
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)  # type: ignore[arg-type]

    def _worker() -> None:
        try:
            result = call()
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(partial(_finish, error=exc))
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(partial(_finish, result=result))
            except RuntimeError:
                pass

    try:
        worker_thread = threading.Thread(
            target=_worker,
            name=f"{limit_name}-worker",
            daemon=True,
        )
        worker_thread.start()
    except BaseException:
        # No worker owns the permit if construction/start failed. Return it
        # synchronously so a transient OS thread-resource error cannot poison
        # every later DNS/search request for this event loop.
        semaphore.release()
        future.cancel()
        raise
    remaining = max(0.0, deadline - loop.time())
    try:
        done, _pending = await asyncio.wait({future}, timeout=remaining)
    except asyncio.CancelledError:
        future.cancel()
        raise
    if future not in done:
        future.cancel()
        raise TimeoutError
    return future.result()


async def resolve_public_http_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] = (),
    dns_timeout_sec: float = _DNS_TIMEOUT_SEC,
) -> _ResolvedTarget:
    """Validate a URL and resolve it once to public IPs for a pinned connection."""
    host, port = _validate_url_syntax(url, allowed_hosts=allowed_hosts)
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None

    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            addresses = await _run_bounded_daemon_thread(
                partial(_resolve_host_sync, host, port),
                timeout_sec=max(0.1, dns_timeout_sec),
                limit_name="safe-dns",
                capacity=4,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise UnsafeUrlError("dns_timeout") from exc
        except OSError as exc:
            raise UnsafeUrlError("dns_resolution_failed") from exc

    if not addresses:
        raise UnsafeUrlError("dns_no_addresses")
    if any(not _is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("non_public_address")
    canonical_addresses = tuple(
        str(ipaddress.ip_address(address.split("%", 1)[0])) for address in addresses
    )
    return _ResolvedTarget(host=host, port=port, addresses=canonical_addresses)


def strip_html(text: str, *, max_len: int = 800) -> str:
    body = _SCRIPT_STYLE_RE.sub(" ", text or "")
    body = _TAG_RE.sub(" ", body)
    body = html.unescape(body)
    body = _CONTROL_RE.sub(" ", body)
    return clean_multiline_text(body, max_len=max_len)


def _extract_meta(raw_html: str, *names: str) -> str:
    lowered = {name.lower() for name in names}
    for match in _META_RE.finditer(raw_html or ""):
        key = clean_text(match.group("name") or "", max_len=80).lower()
        if key in lowered:
            return clean_multiline_text(html.unescape(match.group("content") or ""), max_len=600)
    return ""


def _walk_json_ld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_ld(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_ld(item)


def _extract_json_ld_summary(raw_html: str) -> dict[str, str]:
    candidates: list[dict[str, Any]] = []
    for match in _JSON_LD_RE.finditer(raw_html or ""):
        text = (match.group(1) or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        candidates.extend(_walk_json_ld(data))

    title = ""
    description = ""
    author = ""
    for item in candidates:
        if not title:
            title = clean_multiline_text(
                str(item.get("headline") or item.get("name") or ""),
                max_len=200,
            )
        if not description:
            description = clean_multiline_text(
                str(item.get("description") or item.get("articleBody") or ""),
                max_len=1200,
            )
        if not author:
            author_value = item.get("author")
            if isinstance(author_value, dict):
                author = clean_multiline_text(str(author_value.get("name") or ""), max_len=120)
            elif isinstance(author_value, list):
                names = [
                    clean_multiline_text(str(entry.get("name") or ""), max_len=60)
                    for entry in author_value
                    if isinstance(entry, dict) and str(entry.get("name") or "").strip()
                ]
                author = clean_text(" / ".join(names), max_len=120)
            elif author_value:
                author = clean_multiline_text(str(author_value), max_len=120)
        if title and description and author:
            break
    return {"title": title, "description": description, "author": author}


def parse_html_summary(raw_html: str, *, max_content_len: int = 1200) -> dict[str, str]:
    title_match = _TITLE_RE.search(raw_html or "")
    title = clean_multiline_text(
        html.unescape(title_match.group(1) if title_match else ""),
        max_len=200,
    )
    description = _extract_meta(
        raw_html,
        "description",
        "og:description",
        "twitter:description",
        "weibo:article:description",
    )
    site_name = _extract_meta(raw_html, "og:site_name", "application-name")
    author = _extract_meta(raw_html, "author", "article:author", "og:article:author")

    json_ld = _extract_json_ld_summary(raw_html)
    if not title:
        title = json_ld["title"]
    if not description:
        description = json_ld["description"]
    if not author:
        author = json_ld["author"]

    content = description or strip_html(raw_html, max_len=max_content_len)
    return {
        "title": title,
        "description": description,
        "author": author,
        "site_name": site_name,
        "content": clean_multiline_text(content, max_len=max_content_len),
    }


async def fetch_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 15.0,
    allow_redirects: bool = True,
    allowed_hosts: Iterable[str] = (),
    allowed_content_types: Iterable[str] = _DEFAULT_ALLOWED_CONTENT_TYPES,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    max_decoded_bytes: int = _DEFAULT_MAX_DECODED_BYTES,
    max_redirects: int = 5,
) -> tuple[int, str, str, str]:
    """Fetch text through a DNS-pinned, SSRF-safe, bounded HTTP client.

    Redirects are intentionally handled one hop at a time so every destination
    is parsed, DNS-resolved, checked, and pinned independently.  Bodies are
    streamed with a compressed-byte cap and decompressed with a second cap.
    """
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        forbidden_request_headers = {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "proxy-connection",
        }
        request_headers.update(
            {
                str(k): str(v)
                for k, v in headers.items()
                if str(k).strip()
                and str(k).strip().lower() not in forbidden_request_headers
            }
        )
    # Never allow a caller to opt back into encodings we do not bound ourselves.
    request_headers["Accept-Encoding"] = "gzip, deflate"

    timeout_value = max(0.1, float(timeout_sec))
    redirects_left = max(0, min(int(max_redirects), 10))
    current_url = (url or "").strip()
    current_params = dict(params or {}) or None
    current_headers = dict(request_headers)

    async with asyncio.timeout(timeout_value):
        while True:
            target = await resolve_public_http_url(
                current_url,
                allowed_hosts=allowed_hosts,
                dns_timeout_sec=min(_DNS_TIMEOUT_SEC, timeout_value),
            )
            hop = await _fetch_text_one_hop(
                current_url,
                target=target,
                params=current_params,
                headers=current_headers,
                timeout_sec=timeout_value,
                allowed_content_types=tuple(allowed_content_types),
                max_response_bytes=max_response_bytes,
                max_decoded_bytes=max_decoded_bytes,
            )
            current_params = None
            if hop.status not in _HTTP_REDIRECT_STATUSES or not hop.location:
                return hop.status, hop.text, hop.url, hop.content_type
            if not allow_redirects:
                return hop.status, "", hop.url, hop.content_type
            if redirects_left <= 0:
                raise UnsafeUrlError("too_many_redirects")
            redirects_left -= 1

            next_url = urljoin(hop.url, hop.location)
            old_parsed = urlparse(hop.url)
            new_parsed = urlparse(next_url)
            try:
                # Origin comparison must preserve the exact canonical host.
                # ``normalize_host`` intentionally folds www/non-www for
                # platform allowlists, but those are distinct HTTP origins and
                # must not share Authorization or Cookie headers.
                old_host = _normalize_hostname(old_parsed.hostname or "")
                new_host = _normalize_hostname(new_parsed.hostname or "")
                old_port = old_parsed.port or (
                    443 if old_parsed.scheme.lower() == "https" else 80
                )
                new_port = new_parsed.port or (
                    443 if new_parsed.scheme.lower() == "https" else 80
                )
            except ValueError as exc:
                raise UnsafeUrlError("invalid_redirect") from exc
            if not old_host or not new_host:
                raise UnsafeUrlError("invalid_redirect")
            origin_changed = (
                old_parsed.scheme.lower(),
                old_host,
                old_port,
            ) != (
                new_parsed.scheme.lower(),
                new_host,
                new_port,
            )
            if origin_changed:
                current_headers = {
                    name: value
                    for name, value in current_headers.items()
                    if name.lower() not in _SENSITIVE_REDIRECT_HEADERS
                }
            current_url = next_url


async def fetch_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 15.0,
    allowed_hosts: Iterable[str] = (),
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    max_decoded_bytes: int = _DEFAULT_MAX_DECODED_BYTES,
) -> tuple[int, Any, str]:
    status, text, final_url, content_type = await fetch_text(
        url,
        params=params,
        headers=headers,
        timeout_sec=timeout_sec,
        allowed_hosts=allowed_hosts,
        allowed_content_types=(
            "application/json",
            "text/json",
            "text/plain",
            "text/html",
            "application/xhtml+xml",
        ),
        max_response_bytes=max_response_bytes,
        max_decoded_bytes=max_decoded_bytes,
    )
    try:
        return status, json.loads(text), final_url
    except json.JSONDecodeError as exc:
        preview = clean_multiline_text((text or "").strip(), max_len=240)
        log.warning(
            "fetch_json invalid json | status=%s content_type=%s url=%s preview=%s",
            status,
            content_type or "-",
            final_url or url,
            preview or "(empty)",
        )
        raise InvalidJsonResponseError(
            url=final_url or url,
            status=status,
            content_type=content_type,
            body_preview=preview,
        ) from exc


async def request_json(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any | None = None,
    timeout_sec: float = 15.0,
    allowed_hosts: Iterable[str] = (),
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    max_decoded_bytes: int = _DEFAULT_MAX_DECODED_BYTES,
) -> tuple[int, Any | None, str, str]:
    """Make one bounded, DNS-pinned JSON request without forwarding redirects.

    The return value is ``(status, parsed_json, final_url, error_text)``.  A
    redirect is returned as an error instead of being followed, which keeps
    caller-supplied credentials confined to the explicitly configured origin.
    """
    normalized_method = str(method or "").strip().upper()
    if normalized_method not in {"GET", "POST"}:
        raise ValueError("unsupported_http_method")

    request_headers = {
        "User-Agent": "SmartGroupBot/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        forbidden_request_headers = {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "proxy-connection",
        }
        request_headers.update(
            {
                str(name): str(value)
                for name, value in headers.items()
                if str(name).strip()
                and str(name).strip().lower() not in forbidden_request_headers
            }
        )
    # Keep response decompression within the formats bounded below.
    request_headers["Accept-Encoding"] = "gzip, deflate"
    if normalized_method == "POST":
        request_headers["Content-Type"] = "application/json"

    timeout_value = max(0.1, float(timeout_sec))
    request_url = (url or "").strip()
    async with asyncio.timeout(timeout_value):
        target = await resolve_public_http_url(
            request_url,
            allowed_hosts=allowed_hosts,
            dns_timeout_sec=min(_DNS_TIMEOUT_SEC, timeout_value),
        )
        response = await _fetch_text_one_hop(
            request_url,
            target=target,
            params=dict(params or {}) or None,
            headers=request_headers,
            timeout_sec=timeout_value,
            allowed_content_types=(
                "application/json",
                "text/json",
                "text/plain",
                "text/html",
                "application/xhtml+xml",
            ),
            max_response_bytes=max_response_bytes,
            max_decoded_bytes=max_decoded_bytes,
            method=normalized_method,
            json_body=json_body,
        )

    if response.status in _HTTP_REDIRECT_STATUSES:
        return response.status, None, response.url, "redirect_not_allowed"
    try:
        payload = json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
        error_text = clean_multiline_text((response.text or "").strip(), max_len=300)
        return (
            response.status,
            None,
            response.url,
            error_text or "invalid_json_response",
        )
    return response.status, payload, response.url, ""


def _content_type_allowed(content_type: str, allowed: Iterable[str]) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if not media_type:
        return False
    normalized = {str(item or "").strip().lower() for item in allowed if str(item or "").strip()}
    if media_type in normalized:
        return True
    if any(
        candidate.endswith("/") and media_type.startswith(candidate)
        for candidate in normalized
    ):
        return True
    return bool(media_type.endswith("+json") and "application/json" in normalized)


async def _read_limited_raw_body(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    limit = max(1, int(max_bytes))
    content_length = response.content_length
    if content_length is not None and content_length > limit:
        raise ResponseTooLargeError(f"response_too_large:{content_length}>{limit}")
    body = bytearray()
    async for chunk in response.content.iter_chunked(min(64 * 1024, limit + 1)):
        body.extend(chunk)
        if len(body) > limit:
            raise ResponseTooLargeError(f"response_too_large:{len(body)}>{limit}")
    return bytes(body)


def _decompress_limited(raw: bytes, encoding: str, max_bytes: int) -> bytes:
    limit = max(1, int(max_bytes))
    normalized = (encoding or "").strip().lower()
    if not normalized or normalized == "identity":
        if len(raw) > limit:
            raise ResponseTooLargeError(f"decoded_response_too_large:{len(raw)}>{limit}")
        return raw
    if "," in normalized or normalized not in {"gzip", "deflate"}:
        raise UnsupportedContentTypeError(f"unsupported_content_encoding:{normalized}")

    window_bits = 16 + zlib.MAX_WBITS if normalized == "gzip" else zlib.MAX_WBITS

    def decompress_with_window(bits: int) -> bytes:
        decoder = zlib.decompressobj(bits)
        output = bytearray(decoder.decompress(raw, limit + 1))
        if len(output) <= limit:
            output.extend(decoder.flush(limit + 1 - len(output)))
        if len(output) > limit or decoder.unconsumed_tail:
            raise ResponseTooLargeError(f"decoded_response_too_large:>{limit}")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("invalid_or_concatenated_compressed_response")
        return bytes(output)

    try:
        return decompress_with_window(window_bits)
    except zlib.error:
        if normalized != "deflate":
            raise
        return decompress_with_window(-zlib.MAX_WBITS)


def _decode_response_text(raw: bytes, *, content_encoding: str, charset: str | None, max_bytes: int) -> str:
    decoded = _decompress_limited(raw, content_encoding, max_bytes)
    codec = (charset or "utf-8").strip() or "utf-8"
    try:
        return decoded.decode(codec, errors="ignore")
    except LookupError:
        return decoded.decode("utf-8", errors="ignore")


async def _fetch_text_one_hop(
    url: str,
    *,
    target: _ResolvedTarget,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout_sec: float,
    allowed_content_types: Iterable[str],
    max_response_bytes: int,
    max_decoded_bytes: int,
    method: str = "GET",
    json_body: Any | None = None,
) -> _HopResponse:
    resolver = _PinnedResolver(target)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        force_close=True,
        limit=1,
    )
    timeout = aiohttp.ClientTimeout(
        total=max(0.1, timeout_sec),
        connect=min(5.0, max(0.1, timeout_sec)),
        sock_read=min(10.0, max(0.1, timeout_sec)),
    )
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
        connector=connector,
        auto_decompress=False,
        trust_env=False,
    ) as session:
        async with session.request(
            method,
            url,
            params=params,
            json=json_body,
            allow_redirects=False,
        ) as response:
            response_url = str(response.url)
            content_type = str(response.headers.get("content-type") or "").lower()
            location = str(response.headers.get("location") or "").strip()
            if response.status in _HTTP_REDIRECT_STATUSES and location:
                return _HopResponse(
                    status=response.status,
                    text="",
                    url=response_url,
                    content_type=content_type,
                    location=location,
                )

            if not _content_type_allowed(content_type, allowed_content_types):
                raise UnsupportedContentTypeError(
                    f"unsupported_content_type:{content_type or 'missing'}"
                )

            connection = response.connection
            transport = connection.transport if connection is not None else None
            peer = transport.get_extra_info("peername") if transport is not None else None
            if peer:
                peer_ip = str(ipaddress.ip_address(str(peer[0]).split("%", 1)[0]))
                if peer_ip not in target.addresses:
                    raise UnsafeUrlError("connected_to_unvalidated_address")

            raw = await _read_limited_raw_body(response, max_response_bytes)
            text = _decode_response_text(
                raw,
                content_encoding=str(response.headers.get("content-encoding") or ""),
                charset=response.charset,
                max_bytes=max_decoded_bytes,
            )
            return _HopResponse(
                status=response.status,
                text=text,
                url=response_url,
                content_type=content_type,
            )


async def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 15.0,
    allow_redirects: bool = True,
    allowed_hosts: Iterable[str] = (),
    allowed_content_types: Iterable[str] = ("application/octet-stream",),
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    max_redirects: int = 5,
) -> tuple[int, bytes, str, str]:
    """Fetch a bounded binary payload with the same SSRF/DNS guarantees.

    This is intentionally separate from ``fetch_text`` so callers such as
    cover-image downloaders never decode and duplicate a large binary body.
    """

    request_headers = {
        "User-Agent": "SmartGroupBot/1.0",
        "Accept": "*/*",
        # Binary callers get a single, directly bounded representation.
        "Accept-Encoding": "identity",
    }
    if headers:
        forbidden = {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "proxy-connection",
            "accept-encoding",
        }
        request_headers.update(
            {
                str(name): str(value)
                for name, value in headers.items()
                if str(name).strip()
                and str(name).strip().lower() not in forbidden
            }
        )

    timeout_value = max(0.1, float(timeout_sec))
    redirects_left = max(0, min(int(max_redirects), 10))
    current_url = (url or "").strip()
    current_headers = dict(request_headers)

    async with asyncio.timeout(timeout_value):
        while True:
            target = await resolve_public_http_url(
                current_url,
                allowed_hosts=allowed_hosts,
                dns_timeout_sec=min(_DNS_TIMEOUT_SEC, timeout_value),
            )
            hop = await _fetch_bytes_one_hop(
                current_url,
                target=target,
                headers=current_headers,
                timeout_sec=timeout_value,
                allowed_content_types=tuple(allowed_content_types),
                max_response_bytes=max_response_bytes,
            )
            if hop.status not in _HTTP_REDIRECT_STATUSES or not hop.location:
                return hop.status, hop.body, hop.url, hop.content_type
            if not allow_redirects:
                return hop.status, b"", hop.url, hop.content_type
            if redirects_left <= 0:
                raise UnsafeUrlError("too_many_redirects")
            redirects_left -= 1

            next_url = urljoin(hop.url, hop.location)
            old_parsed = urlparse(hop.url)
            new_parsed = urlparse(next_url)
            try:
                old_host = _normalize_hostname(old_parsed.hostname or "")
                new_host = _normalize_hostname(new_parsed.hostname or "")
                old_port = old_parsed.port or (
                    443 if old_parsed.scheme.lower() == "https" else 80
                )
                new_port = new_parsed.port or (
                    443 if new_parsed.scheme.lower() == "https" else 80
                )
            except ValueError as exc:
                raise UnsafeUrlError("invalid_redirect") from exc
            if not old_host or not new_host:
                raise UnsafeUrlError("invalid_redirect")
            if (
                old_parsed.scheme.lower(),
                old_host,
                old_port,
            ) != (
                new_parsed.scheme.lower(),
                new_host,
                new_port,
            ):
                current_headers = {
                    name: value
                    for name, value in current_headers.items()
                    if name.lower() not in _SENSITIVE_REDIRECT_HEADERS
                }
            current_url = next_url


async def _fetch_bytes_one_hop(
    url: str,
    *,
    target: _ResolvedTarget,
    headers: dict[str, str],
    timeout_sec: float,
    allowed_content_types: Iterable[str],
    max_response_bytes: int,
) -> _HopBytesResponse:
    resolver = _PinnedResolver(target)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        force_close=True,
        limit=1,
    )
    timeout = aiohttp.ClientTimeout(
        total=max(0.1, timeout_sec),
        connect=min(5.0, max(0.1, timeout_sec)),
        sock_read=min(10.0, max(0.1, timeout_sec)),
    )
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
        connector=connector,
        auto_decompress=False,
        trust_env=False,
    ) as session:
        async with session.get(url, allow_redirects=False) as response:
            response_url = str(response.url)
            content_type = str(response.headers.get("content-type") or "").lower()
            location = str(response.headers.get("location") or "").strip()
            if response.status in _HTTP_REDIRECT_STATUSES and location:
                return _HopBytesResponse(
                    status=response.status,
                    body=b"",
                    url=response_url,
                    content_type=content_type,
                    location=location,
                )
            if not _content_type_allowed(content_type, allowed_content_types):
                raise UnsupportedContentTypeError(
                    f"unsupported_content_type:{content_type or 'missing'}"
                )

            connection = response.connection
            transport = connection.transport if connection is not None else None
            peer = transport.get_extra_info("peername") if transport is not None else None
            if peer:
                peer_ip = str(ipaddress.ip_address(str(peer[0]).split("%", 1)[0]))
                if peer_ip not in target.addresses:
                    raise UnsafeUrlError("connected_to_unvalidated_address")

            raw = await _read_limited_raw_body(response, max_response_bytes)
            body = _decompress_limited(
                raw,
                str(response.headers.get("content-encoding") or ""),
                max_response_bytes,
            )
            return _HopBytesResponse(
                status=response.status,
                body=body,
                url=response_url,
                content_type=content_type,
            )


async def run_search_thread(call: Callable[[], _T], *, timeout_sec: float) -> _T:
    """Run a blocking search in a bounded daemon thread with a hard await budget."""
    return await _run_bounded_daemon_thread(
        call,
        timeout_sec=timeout_sec,
        limit_name="ddgs-search",
        capacity=2,
    )


def parse_search_rows(
    rows: list[dict[str, Any]],
    *,
    max_results: int,
    allowed_hosts: Iterable[str] = (),
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for row in rows:
        if len(results) >= max_results:
            break
        url = clean_text(
            str(row.get("href") or row.get("url") or row.get("link") or ""),
            max_len=300,
        )
        if not url or url in seen_urls or not host_matches(url, allowed_hosts):
            continue
        seen_urls.add(url)

        title = strip_html(
            str(row.get("title") or row.get("headline") or url),
            max_len=160,
        )
        snippet = strip_html(
            str(
                row.get("body")
                or row.get("snippet")
                or row.get("description")
                or row.get("excerpt")
                or row.get("content")
                or ""
            ),
            max_len=240,
        )
        results.append({"title": title or url, "url": url, "snippet": snippet})

    return results


def _search_once(
    *,
    query: str,
    region: str,
    max_results: int,
    backend: str,
) -> list[dict[str, Any]]:
    from ddgs import DDGS

    with DDGS(timeout=5) as ddgs:
        return list(
            ddgs.text(
                query,
                max_results=max_results,
                region=region,
                safesearch="off",
                backend=backend,
            )
        )


async def site_search(
    query: str,
    *,
    query_candidates: Iterable[str],
    max_results: int,
    allowed_hosts: Iterable[str] = (),
    timeout_sec: float = 18.0,
) -> tuple[list[dict[str, str]], str]:
    normalized = [
        clean_text(str(item or ""), max_len=220).strip()
        for item in query_candidates
        if clean_text(str(item or ""), max_len=220).strip()
    ]
    if not normalized:
        normalized = [clean_text(query, max_len=220).strip()]
    attempts = normalized[:6]

    primary_region = "cn-zh" if contains_cjk(query) else "wt-wt"
    fallback_region = "wt-wt" if primary_region != "wt-wt" else "us-en"
    last_error = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(timeout_sec))

    for idx, candidate in enumerate(attempts, start=1):
        for region in (primary_region, fallback_region):
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("site_search total timeout: query=%s", query)
                return [], clean_text(query, max_len=220).strip()
            try:
                rows = await run_search_thread(
                    partial(
                        _search_once,
                        query=candidate,
                        region=region,
                        max_results=max_results,
                        backend="duckduckgo,google,yahoo,yandex,brave",
                    ),
                    timeout_sec=min(6.0, remaining),
                )
            except ModuleNotFoundError:
                raise
            except TimeoutError:
                last_error = "search_timeout"
                log.warning(
                    "site_search timed out: idx=%d region=%s query=%s",
                    idx,
                    region,
                    candidate,
                )
                continue
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                log.warning(
                    "site_search failed: idx=%d region=%s query=%s error=%s",
                    idx,
                    region,
                    candidate,
                    exc,
                )
                continue

            results = parse_search_rows(rows, max_results=max_results, allowed_hosts=allowed_hosts)
            if results:
                return results, candidate

    if last_error:
        log.warning("site_search exhausted without results: query=%s error=%s", query, last_error)
    return [], clean_text(query, max_len=220).strip()
