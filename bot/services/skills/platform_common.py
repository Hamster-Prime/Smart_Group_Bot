from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Iterable
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.request import Request, build_opener

try:
    import aiohttp
except ModuleNotFoundError:
    aiohttp = None  # type: ignore[assignment]

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


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def extract_first_url(text: str) -> str:
    match = _URL_RE.search(text or "")
    return match.group(0).strip() if match else ""


def normalize_host(url: str) -> str:
    host = urlparse(url).netloc.lower().strip()
    return host[4:] if host.startswith("www.") else host


def host_matches(url: str, allowed_hosts: Iterable[str]) -> bool:
    host = normalize_host(url)
    normalized = {
        clean_text(str(item or "").lower(), max_len=80).strip().removeprefix("www.")
        for item in allowed_hosts or []
        if str(item or "").strip()
    }
    if not normalized:
        return True
    return any(host == candidate or host.endswith(f".{candidate}") for candidate in normalized)


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
) -> tuple[int, str, str, str]:
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
    }
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items() if str(k).strip()})

    if aiohttp is None:
        return await asyncio.to_thread(
            _fetch_text_stdlib,
            url,
            params=params,
            headers=request_headers,
            timeout_sec=timeout_sec,
        )

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout, headers=request_headers) as session:
        async with session.get(url, params=params, allow_redirects=allow_redirects) as response:
            text = await response.text(errors="ignore")
            content_type = str(response.headers.get("content-type") or "").lower()
            return response.status, text, str(response.url), content_type


async def fetch_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 15.0,
) -> tuple[int, Any, str]:
    status, text, final_url, _ = await fetch_text(
        url,
        params=params,
        headers=headers,
        timeout_sec=timeout_sec,
    )
    return status, json.loads(text), final_url


def _fetch_text_stdlib(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout_sec: float,
) -> tuple[int, str, str, str]:
    query = urlencode(params or {}, doseq=True)
    full_url = f"{url}{'&' if '?' in url else '?'}{query}" if query else url
    request = Request(full_url, headers=headers)
    opener = build_opener()
    try:
        with opener.open(request, timeout=timeout_sec) as response:
            raw = response.read()
            text = raw.decode("utf-8", errors="ignore")
            content_type = str(response.headers.get("content-type") or "").lower()
            status = int(getattr(response, "status", 200) or 200)
            return status, text, str(response.geturl()), content_type
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        content_type = str(exc.headers.get("content-type") or "").lower()
        return int(exc.code or 500), body, str(exc.geturl()), content_type


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

    with DDGS() as ddgs:
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

    for idx, candidate in enumerate(attempts, start=1):
        for region in (primary_region, fallback_region):
            try:
                rows = await asyncio.to_thread(
                    _search_once,
                    query=candidate,
                    region=region,
                    max_results=max_results,
                    backend="duckduckgo,google,yahoo,yandex,brave",
                )
            except ModuleNotFoundError:
                raise
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
