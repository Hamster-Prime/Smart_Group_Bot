from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.parse import parse_qs, quote, quote_plus, urljoin, urlparse

import aiohttp

from bot.config import Settings
from bot.services.resource_health import register_resource_health_provider
from bot.services.skills.platform_common import fetch_text

log = logging.getLogger(__name__)

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_SIZE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:TB|GB|MB|KB)\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\b([A-Za-z]{2,10})[-_ ]?(\d{2,5})\b")
_FC2_CODE_RE = re.compile(r"\bFC2[-_ ]?(?:PPV[-_ ]?)?(\d{5,9})\b", re.IGNORECASE)
_FC2_ARTICLE_RE = re.compile(r"/article/(\d{5,9})", re.IGNORECASE)
_STAR_ID_RE = re.compile(r"/star/([A-Za-z0-9]+)", re.IGNORECASE)
_STAR_NAME_CACHE: dict[str, str] = {}
_STAR_NAME_CACHE_MAX = 512
_AV_QUERY_CONCURRENCY = 3
_AV_QUERY_DEADLINE_SECONDS = 45.0
_AV_QUERY_ADMISSION_TIMEOUT_SECONDS = 2.0
_AV_QUERY_SEMAPHORE = asyncio.Semaphore(_AV_QUERY_CONCURRENCY)
_AV_RESULT = TypeVar("_AV_RESULT")


async def _run_av_query_bounded(
    operation: Callable[[], Awaitable[_AV_RESULT]],
    *,
    fallback: _AV_RESULT,
    label: str,
) -> _AV_RESULT:
    """Run one AV query with a deadline that includes semaphore admission.

    Starting the timeout only after acquiring the semaphore allowed an
    arbitrary number of callers to wait forever during an upstream outage.
    AV lookup is best-effort work, so reject it quickly under saturation and
    preserve event-loop/update capacity for interactive security operations.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _AV_QUERY_DEADLINE_SECONDS
    acquired = False
    try:
        admission_budget = min(
            _AV_QUERY_ADMISSION_TIMEOUT_SECONDS,
            max(0.01, deadline - loop.time()),
        )
        try:
            async with asyncio.timeout(admission_budget):
                await _AV_QUERY_SEMAPHORE.acquire()
                acquired = True
        except TimeoutError:
            log.warning("av query admission timed out | %s", label)
            return fallback

        remaining = deadline - loop.time()
        if remaining <= 0:
            log.warning(
                "av query total deadline exceeded before execution | %s",
                label,
            )
            return fallback
        try:
            async with asyncio.timeout(remaining):
                return await operation()
        except TimeoutError:
            log.warning("av query total deadline exceeded | %s", label)
            return fallback
    finally:
        if acquired:
            _AV_QUERY_SEMAPHORE.release()


def av_resource_health_snapshot() -> dict[str, object]:
    waiters = getattr(_AV_QUERY_SEMAPHORE, "_waiters", None)
    return {
        "ok": True,
        "fatal": False,
        "capacity": _AV_QUERY_CONCURRENCY,
        "available": int(getattr(_AV_QUERY_SEMAPHORE, "_value", 0)),
        "waiters": len(waiters or ()),
        "star_name_cache_entries": len(_STAR_NAME_CACHE),
    }


register_resource_health_provider("av_search", av_resource_health_snapshot)


def _cache_star_name(star_id: str, name: str) -> None:
    if star_id in _STAR_NAME_CACHE:
        _STAR_NAME_CACHE.pop(star_id, None)
    elif len(_STAR_NAME_CACHE) >= _STAR_NAME_CACHE_MAX:
        oldest = next(iter(_STAR_NAME_CACHE), None)
        if oldest is not None:
            _STAR_NAME_CACHE.pop(oldest, None)
    _STAR_NAME_CACHE[star_id] = name


def normalize_av_code(raw: str) -> str:
    text = (raw or "").strip()
    matched = _CODE_RE.search(text)
    if not matched:
        return ""
    return f"{matched.group(1).upper()}-{matched.group(2)}"


def normalize_fc2_code(raw: str) -> str:
    text = (raw or "").strip()
    matched = _FC2_CODE_RE.search(text)
    if not matched:
        return ""
    return f"FC2-PPV-{matched.group(1)}"


def is_av_code_query(query: str) -> bool:
    text = (query or "").strip()
    if not text:
        return False
    fc2_norm = normalize_fc2_code(text)
    if fc2_norm:
        cleaned = re.sub(r"[\s_]+", "-", text).upper()
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
        return cleaned == fc2_norm
    norm = normalize_av_code(text)
    if not norm:
        return False
    cleaned = re.sub(r"[\s_]+", "-", text).upper()
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned == norm


def _strip_tags(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip()


def _clean_text(value: str, max_len: int = 0) -> str:
    text = _SPACE_RE.sub(" ", (value or "").replace("\n", " ")).strip()
    if max_len > 0 and len(text) > max_len:
        return text[:max_len].rstrip() + "..."
    return text


def _extract_first(pattern: str, text: str, *, flags: int = re.IGNORECASE | re.DOTALL) -> str:
    matched = re.search(pattern, text or "", flags)
    return matched.group(1).strip() if matched else ""


def _extract_meta(content: str, key: str) -> str:
    if not content:
        return ""
    escaped = re.escape(key)
    candidates = (
        rf'<meta[^>]+property=["\']{escaped}["\'][^>]+content=["\'](.*?)["\']',
        rf'<meta[^>]+name=["\']{escaped}["\'][^>]+content=["\'](.*?)["\']',
    )
    for pattern in candidates:
        found = _extract_first(pattern, content)
        if found:
            return html.unescape(found)
    return ""


def _abs_url(base_url: str, maybe_relative: str) -> str:
    value = html.unescape((maybe_relative or "").strip())
    if not value:
        return ""
    return urljoin(base_url, value)


def _strip_url_query(raw_url: str) -> str:
    value = (raw_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value.split("?", 1)[0]
    return parsed._replace(query="", fragment="").geturl()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = _clean_text(raw, max_len=80)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _looks_like_person_name(text: str) -> bool:
    v = _clean_text(text, max_len=80)
    if not v:
        return False
    if v in {":", "：", "-", "--", "/", "|", "N/A", "n/a"}:
        return False
    low = v.lower()
    if low in {"more", "detail", "\u66f4\u591a", "\u8a73\u60c5", "\u8be6\u60c5"}:
        return False
    if len(v) <= 1:
        return False
    return bool(re.search(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]", v))


def _guess_actor_from_title_text(raw: str, *, code: str = "") -> str:
    text = _clean_text(raw, max_len=320)
    if not text:
        return ""

    norm_code = normalize_av_code(code)
    if norm_code:
        code_pattern = re.escape(norm_code).replace(r"\-", r"[-_ ]?")
        # Common title variants:
        # 1) WANZ-530 <actor>...
        # 2) (WANZ-530)<actor>...
        text = re.sub(
            rf"^\s*(?:[\(\uFF08]\s*)?{code_pattern}(?:\s*[\)\uFF09])?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        m_after_code = re.search(
            rf"(?:[\(\uFF08]\s*)?{code_pattern}(?:\s*[\)\uFF09])?\s*([A-Za-z\u3040-\u30ff\u3400-\u9fff][^\n\r]*)",
            text,
            flags=re.IGNORECASE,
        )
        if m_after_code:
            text = _clean_text(m_after_code.group(1), max_len=260)

    # Trim noisy leading punctuation and common wrappers.
    text = text.strip(" \t\r\n-_:|/\\[](){}<>\u3010\u3011\uFF08\uFF09")
    if not text:
        return ""

    patterns = (
        # Japanese/Chinese style: <actor> + possessive marker.
        r"^([A-Za-z\u3040-\u30ff\u3400-\u9fff\u30FB' .-]{2,40}?)(?:\u306E|\u4E4B|\u7684)",
        # Generic fallback: leading token before obvious separators.
        r"^([A-Za-z\u3040-\u30ff\u3400-\u9fff\u30FB' .-]{2,40}?)(?:\s+-\s+|[|\uFF5C/\uFF0F,\uFF0C])",
    )
    for pattern in patterns:
        found = _extract_first(pattern, text)
        name = _clean_text(found, max_len=80).strip(" \t\r\n-_:|/\\[](){}<>\u3010\u3011\uFF08\uFF09")
        if _looks_like_person_name(name):
            return name

    # Last fallback: first token before whitespace, for titles like "<actor> ..."
    head = _extract_first(r"^([A-Za-z\u3040-\u30ff\u3400-\u9fff\u30FB' .-]{2,40})\s", text)
    name = _clean_text(head, max_len=80).strip(" \t\r\n-_:|/\\[](){}<>\u3010\u3011\uFF08\uFF09")
    if _looks_like_person_name(name):
        return name
    return ""


def _extract_fc2_article_id(raw: str) -> str:
    value = html.unescape((raw or "").strip())
    if not value:
        return ""
    matched = _FC2_ARTICLE_RE.search(value)
    if matched:
        return matched.group(1)
    matched = _FC2_CODE_RE.search(value)
    if matched:
        return matched.group(1)
    if re.fullmatch(r"\d{5,9}", value):
        return value
    return ""


def _normalize_relaxed_av_code(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    direct = normalize_av_code(value)
    if direct:
        return direct

    compact = re.sub(r"[^A-Za-z0-9]+", "", value)
    if not compact:
        return ""

    candidates = [
        compact,
        compact.lstrip("0123456789"),
        re.sub(r"^[A-Za-z]_\d+", "", compact, flags=re.IGNORECASE),
    ]
    for candidate in candidates:
        matched = re.search(r"([A-Za-z]{2,10})(\d{2,5})(?:[A-Za-z]{0,4})$", candidate, re.IGNORECASE)
        if matched:
            return f"{matched.group(1).upper()}-{matched.group(2)}"
    return ""


def _split_text_tokens(raw: str, *, max_len: int, keep_spaces: bool = False) -> list[str]:
    text = _clean_text(raw, max_len=0)
    if not text:
        return []
    pattern = r"[\u3001,/|]" if keep_spaces else r"[\u3001,/|\s]+"
    items: list[str] = []
    for token in re.split(pattern, text):
        cleaned = _clean_text(token, max_len=max_len)
        if cleaned and cleaned not in {"-", "--", "----", "N/A", "n/a"}:
            items.append(cleaned)
    return _dedupe_keep_order(items)


@dataclass(slots=True)
class AVSeed:
    title: str
    magnet: str
    size: str = ""
    date: str = ""


@dataclass(slots=True)
class AVSearchItem:
    source: str
    title: str
    url: str
    code: str = ""
    cover_url: str = ""
    date: str = ""
    summary: str = ""


@dataclass(slots=True)
class AVDetail:
    source: str
    title: str
    url: str
    code: str = ""
    cover_url: str = ""
    date: str = ""
    runtime: str = ""
    score: str = ""
    actors: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    director: str = ""
    studio: str = ""
    publisher: str = ""
    series: str = ""
    summary: str = ""
    seeds: list[AVSeed] = field(default_factory=list)


@dataclass(slots=True)
class AVQuerySession:
    token: str
    owner_user_id: int
    query: str
    results: list[AVSearchItem]
    details: dict[int, AVDetail] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class AVQuerySessionStore:
    def __init__(self, *, ttl_seconds: int = 900, max_sessions: int = 256) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_sessions = max(16, int(max_sessions))
        self._store: dict[str, AVQuerySession] = {}

    def _prune(self) -> None:
        now = time.time()
        expired = [
            token
            for token, session in self._store.items()
            if (now - session.created_at) > self.ttl_seconds
        ]
        for token in expired:
            self._store.pop(token, None)

        if len(self._store) <= self.max_sessions:
            return

        oldest = sorted(self._store.values(), key=lambda item: item.created_at)
        for session in oldest[: len(self._store) - self.max_sessions]:
            self._store.pop(session.token, None)

    def create(self, *, owner_user_id: int, query: str, results: list[AVSearchItem]) -> AVQuerySession:
        self._prune()
        token = secrets.token_hex(4)
        while token in self._store:
            token = secrets.token_hex(4)
        session = AVQuerySession(
            token=token,
            owner_user_id=owner_user_id,
            query=(query or "").strip(),
            results=list(results),
        )
        self._store[token] = session
        return session

    def get(self, token: str) -> AVQuerySession | None:
        self._prune()
        session = self._store.get((token or "").strip())
        if not session:
            return None
        if (time.time() - session.created_at) > self.ttl_seconds:
            self._store.pop(session.token, None)
            return None
        return session


class AVSearchService:
    def __init__(self, settings: Settings) -> None:
        self.timeout_seconds = min(
            20.0,
            max(5.0, float(getattr(settings, "av_http_timeout_sec", 15.0))),
        )
        self.max_results = max(5, int(getattr(settings, "av_max_results", 18)))
        self.javbus_base = str(
            getattr(settings, "av_javbus_base_url", "https://www.javbus.com")
        ).rstrip("/")
        self.madouqu_base = str(
            getattr(settings, "av_madouqu_base_url", "https://madouqu.com")
        ).rstrip("/")
        self.dmm_base = str(
            getattr(settings, "av_dmm_base_url", "https://www.dmm.co.jp")
        ).rstrip("/")
        self.fc2_base = str(
            getattr(settings, "av_fc2_base_url", "https://adult.contents.fc2.com")
        ).rstrip("/")
        self.enabled = bool(getattr(settings, "av_enabled", True))

    @staticmethod
    def _headers(*, referer: str = "") -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    async def _fetch_text(
        self,
        _session: aiohttp.ClientSession,
        url: str,
        *,
        referer: str = "",
        cookies: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        try:
            headers = self._headers(referer=referer)
            if cookies:
                headers["Cookie"] = "; ".join(
                    f"{key}={value}" for key, value in cookies.items()
                )
            status, content, final_url, _content_type = await fetch_text(
                url,
                headers=headers,
                timeout_sec=self.timeout_seconds,
                max_response_bytes=4 * 1024 * 1024,
                max_decoded_bytes=6 * 1024 * 1024,
            )
            return int(status), str(final_url), content
        except Exception:
            log.exception("av fetch failed: %s", url)
            return 599, url, ""

    async def search(self, query: str, *, max_results: int | None = None) -> list[AVSearchItem]:
        return await _run_av_query_bounded(
            lambda: self._search_impl(query, max_results=max_results),
            fallback=[],
            label=f"search query={_clean_text(query, 40)}",
        )

    async def _search_impl(self, query: str, *, max_results: int | None = None) -> list[AVSearchItem]:
        if not self.enabled:
            return []
        cleaned_query = _clean_text(query, max_len=120)
        if not cleaned_query:
            return []

        limit = max_results if isinstance(max_results, int) else self.max_results
        limit = max(3, min(limit, 40))
        per_source = max(3, min(20, limit))

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            jav_task = self._search_javbus(session, cleaned_query, max_results=per_source)
            mad_task = self._search_madouqu(session, cleaned_query, max_results=per_source)
            dmm_task = self._search_dmm(session, cleaned_query, max_results=per_source)
            fc2_task = self._search_fc2(session, cleaned_query, max_results=per_source)
            jav_result, mad_result, dmm_result, fc2_result = await asyncio.gather(
                jav_task,
                mad_task,
                dmm_task,
                fc2_task,
                return_exceptions=True,
            )

        jav_items = jav_result if isinstance(jav_result, list) else []
        mad_items = mad_result if isinstance(mad_result, list) else []
        dmm_items = dmm_result if isinstance(dmm_result, list) else []
        fc2_items = fc2_result if isinstance(fc2_result, list) else []
        if isinstance(jav_result, Exception):
            log.warning("javbus search failed: %s", jav_result)
        if isinstance(mad_result, Exception):
            log.warning("madouqu search failed: %s", mad_result)
        if isinstance(dmm_result, Exception):
            log.warning("dmm search failed: %s", dmm_result)
        if isinstance(fc2_result, Exception):
            log.warning("fc2 search failed: %s", fc2_result)

        merged: list[AVSearchItem] = []
        seen: set[str] = set()
        for item in [*jav_items, *mad_items, *dmm_items, *fc2_items]:
            key = (item.url or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= limit:
                break
        return merged

    async def lookup_by_code(self, code_or_query: str) -> AVDetail | None:
        return await _run_av_query_bounded(
            lambda: self._lookup_by_code_impl(code_or_query),
            fallback=None,
            label=f"lookup query={_clean_text(code_or_query, 40)}",
        )

    async def _lookup_by_code_impl(self, code_or_query: str) -> AVDetail | None:
        if not self.enabled:
            return None
        fc2_code = normalize_fc2_code(code_or_query)
        if fc2_code:
            article_id = _extract_fc2_article_id(fc2_code)
            if not article_id:
                return None
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                detail = await self._fetch_fc2_detail(session, f"{self.fc2_base}/article/{article_id}/")
                if detail:
                    return detail
            return None

        code = normalize_av_code(code_or_query)
        if not code:
            return None

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            direct_url = f"{self.javbus_base}/{quote(code, safe='')}"
            detail = await self._fetch_javbus_detail(session, direct_url)
            if detail and detail.code:
                if normalize_av_code(detail.code) == code:
                    return detail
            if detail and not detail.code:
                detail.code = code
                return detail

            search_items = await self._search_javbus(session, code, max_results=8)
            for item in search_items:
                if normalize_av_code(item.code) == code:
                    exact = await self._fetch_javbus_detail(session, item.url)
                    if exact:
                        return exact
            if search_items:
                first = await self._fetch_javbus_detail(session, search_items[0].url)
                if first:
                    if not first.code:
                        first.code = code
                    return first

            dmm_items = await self._search_dmm(session, code, max_results=8)
            for item in dmm_items:
                if normalize_av_code(item.code) != code:
                    continue
                exact = await self._fetch_dmm_detail(session, item.url)
                if exact:
                    return await self._enrich_with_javbus(session, exact)
            if dmm_items:
                first = await self._fetch_dmm_detail(session, dmm_items[0].url)
                if first:
                    return await self._enrich_with_javbus(session, first)
        return None

    async def fetch_detail(self, item: AVSearchItem) -> AVDetail | None:
        return await _run_av_query_bounded(
            lambda: self._fetch_detail_impl(item),
            fallback=None,
            label=f"detail url={_clean_text(item.url, 80)}",
        )

    async def _fetch_detail_impl(self, item: AVSearchItem) -> AVDetail | None:
        if not self.enabled:
            return None
        source = (item.source or "").strip().lower()
        url = (item.url or "").strip()
        if not url:
            return None

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if source == "javbus":
                return await self._fetch_javbus_detail(session, url)
            if source == "madouqu":
                detail = await self._fetch_madouqu_detail(session, url)
                if not detail:
                    return None
                return await self._enrich_with_javbus(session, detail)
            if source == "dmm":
                detail = await self._fetch_dmm_detail(session, url)
                if not detail:
                    return None
                return await self._enrich_with_javbus(session, detail)
            if source == "fc2":
                return await self._fetch_fc2_detail(session, url)
        return None

    async def _search_javbus(
        self,
        session: aiohttp.ClientSession,
        query: str,
        *,
        max_results: int,
    ) -> list[AVSearchItem]:
        search_url = f"{self.javbus_base}/search/{quote(query, safe='')}/1"
        status, final_url, content = await self._fetch_text(
            session,
            search_url,
            referer=self.javbus_base,
            cookies={"existmag": "all"},
        )
        if status >= 400 or not content:
            return []

        items = self._parse_javbus_search(content, base_url=final_url)
        if items:
            return items[:max_results]

        # Direct detail fallback when search layout is not present.
        detail = self._parse_javbus_detail(content, final_url)
        if detail:
            return [
                AVSearchItem(
                    source="javbus",
                    title=detail.title,
                    code=detail.code,
                    url=detail.url,
                    cover_url=detail.cover_url,
                    date=detail.date,
                    summary=detail.summary,
                )
            ]
        return []

    async def _search_madouqu(
        self,
        session: aiohttp.ClientSession,
        query: str,
        *,
        max_results: int,
    ) -> list[AVSearchItem]:
        search_url = f"{self.madouqu_base}/?s={quote_plus(query)}"
        status, final_url, content = await self._fetch_text(session, search_url, referer=self.madouqu_base)
        if status >= 400 or not content:
            return []
        return self._parse_madouqu_search(content, base_url=final_url)[:max_results]

    async def _search_dmm(
        self,
        session: aiohttp.ClientSession,
        query: str,
        *,
        max_results: int,
    ) -> list[AVSearchItem]:
        search_url = f"{self.dmm_base}/search/?redirect=1&enc=UTF-8&category=&searchstr={quote_plus(query)}"
        status, final_url, content = await self._fetch_text(
            session,
            search_url,
            referer=self.dmm_base,
            cookies={"ckcy": "1"},
        )
        if status >= 400 or not content:
            return []

        items = self._parse_dmm_search(content, base_url=final_url)
        if items:
            return items[:max_results]

        detail = self._parse_dmm_detail(content, final_url)
        if detail:
            return [
                AVSearchItem(
                    source="dmm",
                    title=detail.title,
                    code=detail.code,
                    url=detail.url,
                    cover_url=detail.cover_url,
                    date=detail.date,
                    summary=detail.summary,
                )
            ]
        return []

    async def _search_fc2(
        self,
        session: aiohttp.ClientSession,
        query: str,
        *,
        max_results: int,
    ) -> list[AVSearchItem]:
        search_url = f"{self.fc2_base}/search/?q={quote_plus(query)}"
        status, final_url, content = await self._fetch_text(
            session,
            search_url,
            referer=self.fc2_base,
            cookies={
                "GDPRCHECK": "true",
                "contents_mode": "digital",
                "contents_func_mode": "buy",
                "wei6H": "1",
            },
        )
        if status >= 400 or not content:
            return []

        items = self._parse_fc2_search(content, base_url=final_url)
        if items:
            return items[:max_results]

        detail = self._parse_fc2_detail(content, final_url)
        if detail:
            return [
                AVSearchItem(
                    source="fc2",
                    title=detail.title,
                    code=detail.code,
                    url=detail.url,
                    cover_url=detail.cover_url,
                    date=detail.date,
                    summary=detail.summary,
                )
            ]
        return []

    def _parse_javbus_search(self, content: str, *, base_url: str) -> list[AVSearchItem]:
        items: list[AVSearchItem] = []
        seen: set[str] = set()

        for href, block in re.findall(
            r'<a[^>]+class=["\'][^"\']*movie-box[^"\']*["\'][^>]+href=["\'](.*?)["\'][^>]*>(.*?)</a>',
            content or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            url = _abs_url(base_url, href)
            if not url or url in seen:
                continue
            seen.add(url)

            spans = [
                _clean_text(_strip_tags(piece), max_len=300)
                for piece in re.findall(r"<span[^>]*>(.*?)</span>", block, flags=re.IGNORECASE | re.DOTALL)
            ]
            spans = [piece for piece in spans if piece]
            code = normalize_av_code(spans[0] if spans else "")
            title = spans[1] if len(spans) > 1 else ""
            if not title:
                title = _clean_text(_strip_tags(_extract_first(r"<h\d[^>]*>(.*?)</h\d>", block)), max_len=300)
            if not title:
                title = _clean_text(_strip_tags(block), max_len=200)

            cover = _extract_first(r'<img[^>]+(?:data-src|src)=["\'](.*?)["\']', block)
            date = _clean_text(
                _strip_tags(_extract_first(r'<span[^>]+class=["\']date["\'][^>]*>(.*?)</span>', block))
            )
            if not date:
                matched_date = _DATE_RE.search(_strip_tags(block))
                date = matched_date.group(1) if matched_date else ""

            items.append(
                AVSearchItem(
                    source="javbus",
                    title=title or code or url,
                    code=code,
                    url=url,
                    cover_url=_abs_url(base_url, cover),
                    date=date,
                )
            )
        return items

    def _parse_madouqu_search(self, content: str, *, base_url: str) -> list[AVSearchItem]:
        items: list[AVSearchItem] = []
        seen: set[str] = set()

        articles = re.findall(r"<article\b[^>]*>(.*?)</article>", content or "", flags=re.IGNORECASE | re.DOTALL)
        for article in articles:
            href = _extract_first(r'<h\d[^>]*>\s*<a[^>]+href=["\'](.*?)["\']', article)
            if not href:
                href = _extract_first(r'<a[^>]+href=["\'](.*?)["\']', article)
            url = _abs_url(base_url, href)
            if not url or url in seen:
                continue
            if "/tag/" in url or "/category/" in url:
                continue
            seen.add(url)

            title = _extract_first(r'<h\d[^>]*>\s*<a[^>]*>(.*?)</a>', article)
            title = _clean_text(_strip_tags(title), max_len=300)
            if not title:
                title = _clean_text(_strip_tags(_extract_first(r"<h\d[^>]*>(.*?)</h\d>", article)), max_len=300)
            if not title:
                continue

            cover = _extract_first(r'<img[^>]+(?:data-src|src)=["\'](.*?)["\']', article)
            date = _clean_text(_extract_first(r'<time[^>]+datetime=["\'](.*?)["\']', article))
            if not date:
                date = _clean_text(_strip_tags(_extract_first(r"<time[^>]*>(.*?)</time>", article)))
            snippet = _clean_text(_strip_tags(_extract_first(r"<p[^>]*>(.*?)</p>", article)), max_len=220)

            items.append(
                AVSearchItem(
                    source="madouqu",
                    title=title,
                    code=normalize_av_code(title),
                    url=url,
                    cover_url=_abs_url(base_url, cover),
                    date=date,
                    summary=snippet,
                )
            )

        if items:
            return items

        # Fallback for non-article list pages.
        for href, title_html in re.findall(
            r'<a[^>]+href=["\'](https?://[^"\']*madouqu\.com/[^"\']+)["\'][^>]*>(.*?)</a>',
            content or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            url = (href or "").strip()
            if not url or url in seen:
                continue
            if any(part in url for part in ("/tag/", "/category/", "/wp-content/", "/feed")):
                continue
            title = _clean_text(_strip_tags(title_html), max_len=220)
            if not title or len(title) < 2:
                continue
            seen.add(url)
            items.append(
                AVSearchItem(
                    source="madouqu",
                    title=title,
                    code=normalize_av_code(title),
                    url=url,
                )
            )
            if len(items) >= 20:
                break
        return items

    def _parse_dmm_search(self, content: str, *, base_url: str) -> list[AVSearchItem]:
        items: list[AVSearchItem] = []
        seen: set[str] = set()

        blocks = re.split(r'(?=<div class="flex py-1\.5 pl-3">)', content or "")
        for block in blocks:
            href = _extract_first(r'<a[^>]+href=["\'](https?://www\.dmm\.co\.jp/[^"\']*/detail/=/cid=[^"\']+)["\']', block)
            if not href:
                href = _extract_first(r'<a[^>]+href=["\'](/[^"\']*/detail/=/cid=[^"\']+)["\']', block)
            url = _strip_url_query(_abs_url(base_url, href))
            if not url or url in seen:
                continue

            title = _clean_text(
                _strip_tags(
                    _extract_first(
                        r'<p[^>]+class=["\'][^"\']*text-sm[^"\']*font-bold[^"\']*line-clamp-2[^"\']*["\'][^>]*>(.*?)</p>',
                        block,
                    )
                ),
                max_len=300,
            )
            if not title:
                continue

            seen.add(url)
            cover = _extract_first(r'<img[^>]+src=["\'](.*?)["\']', block)
            date = _clean_text(
                _strip_tags(
                    _extract_first(
                        r'<p[^>]+class=["\'][^"\']*text-xs[^"\']*text-gray-500[^"\']*["\'][^>]*>\s*(?:発売日|貸出日|配信開始日)\s*[:：]?\s*(.*?)</p>',
                        block,
                    )
                ),
                max_len=40,
            )
            actor_line = _clean_text(
                _strip_tags(
                    _extract_first(
                        r'<p[^>]+class=["\'][^"\']*text-xs[^"\']*text-gray-500[^"\']*line-clamp-1[^"\']*["\'][^>]*>\s*(?:出演者|シリーズ)\s*[:：]?\s*(.*?)</p>',
                        block,
                    )
                ),
                max_len=220,
            )
            code = self._normalize_dmm_code(self._extract_dmm_cid(url))
            if not code:
                code = self._normalize_dmm_code(title)

            items.append(
                AVSearchItem(
                    source="dmm",
                    title=title,
                    code=code,
                    url=url,
                    cover_url=_abs_url(base_url, cover),
                    date=date,
                    summary=actor_line,
                )
            )
        return items

    def _parse_fc2_search(self, content: str, *, base_url: str) -> list[AVSearchItem]:
        plain = _strip_tags(content)
        if re.search(r"(?:搜索结果|搜寻结果|検索結果)\s*0件", plain):
            return []

        items: list[AVSearchItem] = []
        seen: set[str] = set()
        blocks = re.split(r'(?=<div class="c-cntCard-110-f">)', content or "")
        for block in blocks:
            href = _extract_first(
                r'<a[^>]+class=["\']c-cntCard-110-f_thumb_link["\'][^>]+href=["\'](.*?)["\']',
                block,
            )
            if not href:
                href = _extract_first(
                    r'<a[^>]+class=["\']c-cntCard-110-f_itemName["\'][^>]+href=["\'](.*?)["\']',
                    block,
                )
            url = _strip_url_query(_abs_url(base_url, href))
            if not url or url in seen:
                continue

            title = html.unescape(
                _extract_first(
                    r'<a[^>]+class=["\']c-cntCard-110-f_itemName["\'][^>]+title=["\'](.*?)["\']',
                    block,
                )
            )
            if not title:
                title = _clean_text(
                    _strip_tags(
                        _extract_first(
                            r'<a[^>]+class=["\']c-cntCard-110-f_itemName["\'][^>]*>(.*?)</a>',
                            block,
                        )
                    ),
                    max_len=300,
                )
            title = _clean_text(title, max_len=300)
            if not title:
                continue

            seen.add(url)
            cover = _extract_first(r'<img[^>]+src=["\'](.*?)["\']', block)
            runtime = _clean_text(
                _strip_tags(
                    _extract_first(
                        r'c-cntCard-110-f_thumb_num[^>]*>\s*([^<]+)\s*<',
                        block,
                    )
                ),
                max_len=32,
            )
            seller = _clean_text(
                _strip_tags(
                    _extract_first(r'by\s*</span>\s*<a[^>]*>(.*?)</a>', block)
                ),
                max_len=80,
            )
            price = _clean_text(
                _strip_tags(_extract_first(r'(\d[\d,]*)\s*pt', block)),
                max_len=32,
            )
            summary_parts = [part for part in (runtime, price and f"{price} pt", seller and f"by {seller}") if part]
            article_id = _extract_fc2_article_id(url)
            code = f"FC2-PPV-{article_id}" if article_id else ""

            items.append(
                AVSearchItem(
                    source="fc2",
                    title=title,
                    code=code,
                    url=url,
                    cover_url=_abs_url(base_url, cover),
                    summary=" / ".join(summary_parts),
                )
            )
        return items

    async def _fetch_javbus_detail(self, session: aiohttp.ClientSession, url: str) -> AVDetail | None:
        status, final_url, content = await self._fetch_text(
            session,
            url,
            referer=self.javbus_base,
            cookies={"existmag": "all"},
        )
        if status >= 400 or not content:
            return None

        detail = self._parse_javbus_detail(content, final_url)
        if not detail:
            return None
        star_ids = self._extract_javbus_star_ids(content)
        if star_ids and not detail.actors:
            resolved_actors = await self._resolve_javbus_star_names(
                session,
                star_ids,
                referer=final_url,
            )
            if resolved_actors:
                detail.actors = resolved_actors
        if detail.seeds:
            return detail

        extra_seeds = await self._fetch_javbus_ajax_magnets(
            session=session,
            detail_html=content,
            referer=final_url,
        )
        if extra_seeds:
            detail.seeds = extra_seeds
        return detail

    async def _fetch_javbus_ajax_magnets(
        self,
        *,
        session: aiohttp.ClientSession,
        detail_html: str,
        referer: str,
    ) -> list[AVSeed]:
        gid = _extract_first(r"\bgid\s*=\s*(\d+)\s*;", detail_html)
        uc = _extract_first(r"\buc\s*=\s*(\d+)\s*;", detail_html) or "0"
        img = _extract_first(r'\bimg\s*=\s*["\'](.*?)["\']\s*;', detail_html)
        if not gid:
            return []

        ajax_url = (
            f"{self.javbus_base}/ajax/uncledatoolsbyajax.php?"
            f"gid={quote_plus(gid)}&lang=zh&img={quote_plus(img)}&uc={quote_plus(uc)}&floor=1"
        )
        status, _, content = await self._fetch_text(
            session,
            ajax_url,
            referer=referer,
            cookies={"existmag": "all"},
        )
        if status >= 400 or not content:
            return []
        return self._parse_magnets(content)

    def _parse_javbus_detail(self, content: str, page_url: str) -> AVDetail | None:
        title = _clean_text(_strip_tags(_extract_first(r"<h3[^>]*>(.*?)</h3>", content)), max_len=300)
        if not title:
            title = _clean_text(_strip_tags(_extract_first(r"<title[^>]*>(.*?)</title>", content)), max_len=300)
        if not title:
            return None
        if "404" in title.upper() and "JAVBUS" in title.upper():
            return None

        code = normalize_av_code(
            self._extract_javbus_field_text(
                content,
                [
                    "\u8b58\u5225\u78bc",
                    "\u8bc6\u522b\u7801",
                    "ID",
                    "\u54c1\u756a",
                ],
            )
        )
        if not code:
            code = normalize_av_code(page_url.rsplit("/", 1)[-1])

        release = self._extract_javbus_field_text(
            content,
            [
                "\u767c\u884c\u65e5\u671f",
                "\u53d1\u884c\u65e5\u671f",
                "Release Date",
                "\u914d\u4fe1\u958b\u59cb\u65e5",
            ],
        )
        runtime = self._extract_javbus_field_text(content, ["\u9577\u5ea6", "\u957f\u5ea6", "Length"])
        director = self._extract_javbus_field_text(content, ["\u5c0e\u6f14", "\u5bfc\u6f14", "Director"])
        studio = self._extract_javbus_field_text(content, ["\u88fd\u4f5c\u5546", "\u5236\u4f5c\u5546", "Studio"])
        publisher = self._extract_javbus_field_text(content, ["\u767c\u884c\u5546", "\u53d1\u884c\u5546", "Label"])
        series = self._extract_javbus_field_text(content, ["\u7cfb\u5217", "Series"])
        actors = self._extract_javbus_actors(content)
        if not actors:
            actors = self._extract_javbus_actors_from_meta(
                content=content,
                code=code,
                title=title,
            )
        genres = self._extract_javbus_genres(content)

        score = _clean_text(
            _extract_first(r"(?:\u8a55\u5206|\u8bc4\u5206|Rating)\s*[:\uFF1A]?\s*([0-9.]{1,4})", content),
            max_len=16,
        )
        cover = _extract_first(r'<a[^>]+class=["\']bigImage["\'][^>]+href=["\'](.*?)["\']', content)
        if not cover:
            cover = _extract_first(r'<img[^>]+class=["\'][^"\']*bigImage[^"\']*["\'][^>]+src=["\'](.*?)["\']', content)
        if not cover:
            cover = _extract_meta(content, "og:image")

        summary = _clean_text(_extract_meta(content, "description"), max_len=420)
        seeds = self._parse_magnets(content)

        return AVDetail(
            source="javbus",
            title=title,
            url=page_url,
            code=code,
            cover_url=_abs_url(page_url, cover),
            date=_clean_text(release, max_len=32),
            runtime=_clean_text(runtime, max_len=64),
            score=score,
            actors=[_clean_text(actor, max_len=50) for actor in actors[:10]],
            genres=[_clean_text(genre, max_len=40) for genre in genres[:16]],
            director=_clean_text(director, max_len=80),
            studio=_clean_text(studio, max_len=80),
            publisher=_clean_text(publisher, max_len=80),
            series=_clean_text(series, max_len=80),
            summary=summary,
            seeds=seeds,
        )

    def _extract_javbus_actors(self, content: str) -> list[str]:
        # Primary path: header field.
        primary = self._extract_javbus_field_list(
            content,
            ["\u6f14\u54e1", "\u6f14\u5458", "Actor", "\u5973\u512a", "\u4e3b\u6f14"],
        )
        primary_valid = [x for x in _dedupe_keep_order(primary) if _looks_like_person_name(x)]
        if primary_valid:
            return primary_valid

        candidates: list[str] = []

        # Common javbus actor card block.
        for block in re.findall(
            r'<a[^>]+class=["\'][^"\']*avatar-box[^"\']*["\'][^>]*>(.*?)</a>',
            content or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            span_name = _extract_first(r"<span[^>]*>(.*?)</span>", block)
            text = _clean_text(_strip_tags(span_name or block), max_len=80)
            if text:
                candidates.append(text)

        # Fallback: any anchor linking to /star/ often represents actor pages.
        for anchor_html in re.findall(
            r'(<a[^>]+href=["\'][^"\']*/star/[^"\']*["\'][^>]*>.*?</a>)',
            content or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            inner = _extract_first(r"<a[^>]*>(.*?)</a>", anchor_html)
            text = _clean_text(_strip_tags(inner), max_len=80)
            if _looks_like_person_name(text):
                candidates.append(text)

            # Some anchors only contain actress image; name lives in title/alt.
            title_attr = _extract_first(r'title=["\'](.*?)["\']', anchor_html)
            alt_attr = _extract_first(r'alt=["\'](.*?)["\']', anchor_html)
            img_title = _extract_first(r'<img[^>]+title=["\'](.*?)["\']', anchor_html)
            img_alt = _extract_first(r'<img[^>]+alt=["\'](.*?)["\']', anchor_html)
            for maybe_name in (title_attr, alt_attr, img_title, img_alt):
                value = _clean_text(maybe_name, max_len=80)
                if _looks_like_person_name(value):
                    candidates.append(value)

        # Additional fallback for explicit star-name class.
        for inner in re.findall(
            r'<[^>]+class=["\'][^"\']*star-name[^"\']*["\'][^>]*>(.*?)</[^>]+>',
            content or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            text = _clean_text(_strip_tags(inner), max_len=80)
            if text:
                candidates.append(text)

        return [x for x in _dedupe_keep_order(candidates) if _looks_like_person_name(x)]

    def _extract_javbus_actors_from_meta(
        self,
        *,
        content: str,
        code: str,
        title: str,
    ) -> list[str]:
        sources = [
            title,
            _clean_text(_strip_tags(_extract_first(r"<h3[^>]*>(.*?)</h3>", content)), max_len=300),
            _clean_text(_extract_meta(content, "og:title"), max_len=300),
            _clean_text(_extract_meta(content, "description"), max_len=420),
        ]
        names: list[str] = []
        for source_text in sources:
            guessed = _guess_actor_from_title_text(source_text, code=code)
            if guessed:
                names.append(guessed)
        return [x for x in _dedupe_keep_order(names) if _looks_like_person_name(x)]

    def _extract_javbus_genres(self, content: str) -> list[str]:
        genres = self._extract_javbus_field_list(
            content,
            ["\u985e\u5225", "\u7c7b\u522b", "Genre", "\u30b8\u30e3\u30f3\u30eb"],
        )
        if genres:
            return genres

        # Stable fallback: JAVBUS genre block uses checkbox name="gr_sel".
        by_selector = [
            _clean_text(_strip_tags(inner), max_len=40)
            for inner in re.findall(
                r'name=["\']gr_sel["\'][^>]*>\s*<a[^>]*>(.*?)</a>',
                content or "",
                flags=re.IGNORECASE | re.DOTALL,
            )
        ]
        by_selector = [x for x in by_selector if x]
        if by_selector:
            return _dedupe_keep_order(by_selector)

        block = _extract_first(
            r'<p[^>]+class=["\']header["\'][^>]*>\s*(?:\u985e\u5225|\u7c7b\u522b|Genre)[^<]*</p>\s*<p[^>]*>(.*?)</p>\s*<p[^>]+class=["\']star-show["\']',
            content,
        )
        if not block:
            return []

        out: list[str] = []
        for href, inner in re.findall(
            r'<a[^>]+href=["\'](.*?)["\'][^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            if "/genre/" not in (href or "").lower():
                continue
            text = _clean_text(_strip_tags(inner), max_len=40)
            if not text:
                continue
            out.append(text)
        return _dedupe_keep_order(out)

    @staticmethod
    def _extract_javbus_star_ids(content: str) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for star_id in _STAR_ID_RE.findall(content or ""):
            key = star_id.lower()
            if key in seen:
                continue
            seen.add(key)
            ids.append(star_id)
        return ids

    async def _resolve_javbus_star_name(
        self,
        session: aiohttp.ClientSession,
        star_id: str,
        *,
        referer: str = "",
    ) -> str:
        sid = (star_id or "").strip()
        if not sid:
            return ""
        cached = _STAR_NAME_CACHE.get(sid)
        if cached:
            return cached

        _ = session  # keep signature stable; use clean session to avoid poisoned cookies.
        url = f"{self.javbus_base}/star/{quote_plus(sid)}"

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as clean_session:
                status, _, content = await self._fetch_text(
                    clean_session,
                    url,
                    referer=referer or self.javbus_base,
                    cookies={"existmag": "all"},
                )
        except Exception:
            log.exception("fetch javbus star page failed: %s", sid)
            return ""

        if status >= 400 or not content:
            return ""

        title = _clean_text(_strip_tags(_extract_first(r"<title[^>]*>(.*?)</title>", content)), max_len=180)
        if title:
            # Example: 推川ゆうり - 女優 - 影片
            name = _clean_text(title.split(" - ", 1)[0], max_len=80)
            if _looks_like_person_name(name):
                _cache_star_name(sid, name)
                return name

        og_title = _clean_text(_extract_meta(content, "og:title"), max_len=180)
        if og_title:
            name = _clean_text(og_title.split(" - ", 1)[0], max_len=80)
            if _looks_like_person_name(name):
                _cache_star_name(sid, name)
                return name
        return ""

    async def _resolve_javbus_star_names(
        self,
        session: aiohttp.ClientSession,
        star_ids: list[str],
        *,
        referer: str = "",
    ) -> list[str]:
        names: list[str] = []
        for sid in star_ids[:5]:
            try:
                name = await self._resolve_javbus_star_name(session, sid, referer=referer)
            except Exception:
                log.exception("resolve javbus star failed: %s", sid)
                name = ""
            if _looks_like_person_name(name):
                names.append(name)
        return _dedupe_keep_order(names)

    def _extract_javbus_field_block(self, content: str, labels: list[str]) -> str:
        if not content or not labels:
            return ""
        escaped = "|".join(re.escape(label) for label in labels)
        return _extract_first(
            rf'<span[^>]+class=["\']header["\'][^>]*>\s*(?:{escaped})\s*[:\uFF1A]?\s*</span>\s*(.*?)</p>',
            content,
        )

    def _extract_javbus_field_text(self, content: str, labels: list[str]) -> str:
        block = self._extract_javbus_field_block(content, labels)
        return _clean_text(_strip_tags(block), max_len=240)

    def _extract_javbus_field_list(self, content: str, labels: list[str]) -> list[str]:
        block = self._extract_javbus_field_block(content, labels)
        if not block:
            return []

        linked = [
            _clean_text(_strip_tags(piece), max_len=80)
            for piece in re.findall(r"<a[^>]*>(.*?)</a>", block, flags=re.IGNORECASE | re.DOTALL)
        ]
        linked = [piece for piece in linked if piece]
        if linked:
            return linked

        plain = _clean_text(_strip_tags(block), max_len=240)
        if not plain:
            return []
        return [token for token in re.split(r"[\u3001,/|]", plain) if _clean_text(token)]

    def _parse_magnets(self, content: str) -> list[AVSeed]:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", content or "", flags=re.IGNORECASE | re.DOTALL)
        seeds: list[AVSeed] = []
        seen: set[str] = set()

        for row in rows:
            magnet = _extract_first(r'href=["\'](magnet:\?[^"\']+)["\']', row)
            magnet = html.unescape(magnet).replace("&amp;", "&")
            if not magnet or magnet in seen:
                continue
            seen.add(magnet)

            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.IGNORECASE | re.DOTALL)
            title = ""
            size = ""
            date = ""

            if cells:
                title = _clean_text(
                    _strip_tags(_extract_first(r'href=["\']magnet:\?[^"\']+["\'][^>]*>(.*?)</a>', cells[0]) or cells[0]),
                    max_len=240,
                )
                for cell in cells[1:]:
                    cell_text = _clean_text(_strip_tags(cell), max_len=200)
                    if not size:
                        matched_size = _SIZE_RE.search(cell_text)
                        if matched_size:
                            size = matched_size.group(0)
                    if not date:
                        matched_date = _DATE_RE.search(cell_text)
                        if matched_date:
                            date = matched_date.group(1)

            if not title:
                title = _clean_text(
                    _strip_tags(_extract_first(r'href=["\']magnet:\?[^"\']+["\'][^>]*>(.*?)</a>', row)),
                    max_len=200,
                )

            seeds.append(
                AVSeed(
                    title=title or "Magnet",
                    magnet=magnet,
                    size=size,
                    date=date,
                )
            )

        if seeds:
            return seeds

        # Fallback: parse standalone magnet links from any HTML block.
        for magnet, title_html in re.findall(
            r'href=["\'](magnet:\?[^"\']+)["\'][^>]*>(.*?)</a>',
            content or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            value = html.unescape(magnet).replace("&amp;", "&")
            if not value or value in seen:
                continue
            seen.add(value)
            title = _clean_text(_strip_tags(title_html), max_len=200) or "Magnet"
            seeds.append(AVSeed(title=title, magnet=value))
        return seeds

    async def _fetch_madouqu_detail(self, session: aiohttp.ClientSession, url: str) -> AVDetail | None:
        status, final_url, content = await self._fetch_text(session, url, referer=self.madouqu_base)
        if status >= 400 or not content:
            return None

        title = _clean_text(_strip_tags(_extract_first(r"<h1[^>]*>(.*?)</h1>", content)), max_len=300)
        if not title:
            title = _clean_text(_strip_tags(_extract_first(r"<title[^>]*>(.*?)</title>", content)), max_len=300)
        if not title:
            return None

        date = _clean_text(_extract_first(r'<time[^>]+datetime=["\'](.*?)["\']', content), max_len=40)
        if not date:
            date = _clean_text(_strip_tags(_extract_first(r"<time[^>]*>(.*?)</time>", content)), max_len=40)

        cover = _extract_meta(content, "og:image")
        if not cover:
            cover = _extract_first(r'<img[^>]+(?:data-src|src)=["\'](.*?)["\']', content)

        summary = _extract_meta(content, "description")
        if summary:
            summary = _clean_text(summary, max_len=420)
        else:
            summary = _clean_text(_strip_tags(_extract_first(r"<p[^>]*>(.*?)</p>", content)), max_len=420)

        score = _clean_text(
            _extract_first(r"(?:\u8bc4\u5206|\u8a55\u5206|rating)\s*[:\uFF1A]?\s*([0-9.]{1,4})", content),
            max_len=16,
        )
        actors = self._extract_madouqu_entities(
            content,
            labels=["\u6f14\u5458", "\u6f14\u54e1", "\u5973\u4f18", "\u5973\u512a", "Actor"],
        )
        genres = self._extract_madouqu_entities(
            content,
            labels=["\u5206\u7c7b", "\u985e\u5225", "\u7c7b\u522b", "\u7c7b\u578b", "Genre", "\u30bf\u30b0", "Tag"],
        )

        return AVDetail(
            source="madouqu",
            title=title,
            url=final_url,
            code=normalize_av_code(f"{title} {final_url}"),
            cover_url=_abs_url(final_url, cover),
            date=date,
            score=score,
            actors=actors[:12],
            genres=genres[:16],
            summary=summary,
            seeds=[],
        )

    async def _fetch_dmm_detail(self, session: aiohttp.ClientSession, url: str) -> AVDetail | None:
        status, final_url, content = await self._fetch_text(
            session,
            url,
            referer=self.dmm_base,
            cookies={"ckcy": "1"},
        )
        if status >= 400 or not content:
            return None
        return self._parse_dmm_detail(content, final_url)

    def _parse_dmm_detail(self, content: str, page_url: str) -> AVDetail | None:
        title = _clean_text(_strip_tags(_extract_first(r"<h1[^>]*>(.*?)</h1>", content)), max_len=300)
        if not title:
            title = _clean_text(_strip_tags(_extract_first(r"<title[^>]*>(.*?)</title>", content)), max_len=300)
            if " - " in title:
                title = _clean_text(title.split(" - ", 1)[0], max_len=300)
        if not title:
            return None

        plain = _strip_tags(content)
        release = self._extract_plain_field(
            plain,
            labels=["発売日", "貸出日", "配信開始日"],
            stop_labels=[
                "収録時間",
                "再生時間",
                "出演者",
                "監督",
                "シリーズ",
                "メーカー",
                "レーベル",
                "ジャンル",
                "品番",
                "平均評価",
            ],
            max_len=40,
        )
        runtime = self._extract_plain_field(
            plain,
            labels=["収録時間", "再生時間"],
            stop_labels=[
                "出演者",
                "監督",
                "シリーズ",
                "メーカー",
                "レーベル",
                "ジャンル",
                "品番",
                "平均評価",
            ],
            max_len=40,
        )
        actors_text = self._extract_plain_field(
            plain,
            labels=["出演者"],
            stop_labels=["監督", "シリーズ", "メーカー", "レーベル", "ジャンル", "品番", "平均評価"],
            max_len=240,
        )
        director = self._extract_plain_field(
            plain,
            labels=["監督"],
            stop_labels=["シリーズ", "メーカー", "レーベル", "ジャンル", "品番", "平均評価"],
            max_len=80,
        )
        series = self._extract_plain_field(
            plain,
            labels=["シリーズ"],
            stop_labels=["メーカー", "レーベル", "ジャンル", "品番", "平均評価"],
            max_len=120,
        )
        studio = self._extract_plain_field(
            plain,
            labels=["メーカー"],
            stop_labels=["レーベル", "ジャンル", "品番", "平均評価"],
            max_len=80,
        )
        publisher = self._extract_plain_field(
            plain,
            labels=["レーベル"],
            stop_labels=["ジャンル", "品番", "平均評価"],
            max_len=80,
        )
        genres_text = self._extract_plain_field(
            plain,
            labels=["ジャンル"],
            stop_labels=["品番", "平均評価"],
            max_len=240,
        )
        code_raw = self._extract_plain_field(
            plain,
            labels=["品番"],
            stop_labels=["平均評価"],
            max_len=80,
        )
        code = self._normalize_dmm_code(code_raw or self._extract_dmm_cid(page_url))
        if not code:
            fallback_code = _clean_text(code_raw or self._extract_dmm_cid(page_url), max_len=80)
            code = fallback_code.upper() if fallback_code else ""

        score = _clean_text(_extract_first(r"平均評価[:：]?\s*([0-9.]+)", plain), max_len=16)
        cover = _extract_meta(content, "og:image")
        if not cover:
            cover = _extract_first(r'<img[^>]+src=["\'](https?://pics\.dmm\.co\.jp/[^"\']+)["\']', content)

        summary = _clean_text(_extract_meta(content, "description"), max_len=420)
        actors = [x for x in _split_text_tokens(actors_text, max_len=50) if _looks_like_person_name(x)]
        genres = _split_text_tokens(genres_text, max_len=40)

        return AVDetail(
            source="dmm",
            title=title,
            url=_strip_url_query(page_url),
            code=code,
            cover_url=_abs_url(page_url, cover),
            date=release,
            runtime=runtime,
            score=score,
            actors=actors[:12],
            genres=genres[:16],
            director=director,
            studio=studio,
            publisher=publisher,
            series=series,
            summary=summary,
            seeds=[],
        )

    async def _fetch_fc2_detail(self, session: aiohttp.ClientSession, url: str) -> AVDetail | None:
        status, final_url, content = await self._fetch_text(
            session,
            url,
            referer=self.fc2_base,
            cookies={
                "GDPRCHECK": "true",
                "contents_mode": "digital",
                "contents_func_mode": "buy",
                "wei6H": "1",
            },
        )
        if status >= 400 or not content:
            return None
        return self._parse_fc2_detail(content, final_url)

    def _parse_fc2_detail(self, content: str, page_url: str) -> AVDetail | None:
        title = _clean_text(_strip_tags(_extract_first(r"<title[^>]*>(.*?)</title>", content)), max_len=320)
        if not title:
            title = _clean_text(_strip_tags(_extract_first(r"<h[1-6][^>]*>(.*?)</h[1-6]>", content)), max_len=320)
        if not title:
            return None
        if "|" in title:
            title = _clean_text(title.split("|", 1)[0], max_len=320)
        fc2_code = normalize_fc2_code(title) or normalize_fc2_code(page_url)
        if fc2_code:
            title = _clean_text(re.sub(re.escape(fc2_code), "", title, count=1, flags=re.IGNORECASE), max_len=320)
        article_id = _extract_fc2_article_id(fc2_code or page_url)
        code = f"FC2-PPV-{article_id}" if article_id else fc2_code

        plain = _strip_tags(content)
        date = self._extract_plain_field(
            plain,
            labels=["上架时间", "公開日", "配信開始日"],
            stop_labels=["商品ID", "販賣期間限定", "商品説明", "商品说明"],
            max_len=40,
        )
        studio = _clean_text(
            _extract_first(
                r'\bby\s+(.+?)\s+(?:商品标签|商品タグ|支持的设备|対応デバイス|上架时间|商品ID)',
                plain,
            ),
            max_len=80,
        )
        tags_text = _clean_text(
            _extract_first(
                r"(?:商品标签|商品タグ)\s+(.+?)\s+(?:支持的设备|対応デバイス|上架时间|商品ID)",
                plain,
            ),
            max_len=240,
        )
        score = _clean_text(_extract_first(r"(?:平均评价|平均評価)\s*([0-9.]+)", plain), max_len=16)
        summary = _clean_text(_extract_meta(content, "description"), max_len=420)
        cover = _extract_meta(content, "og:image")
        if not cover:
            cover = _extract_first(r'<img[^>]+src=["\'](https?://[^"\']*contents-thumbnail2\.fc2\.com/[^"\']+)["\']', content)

        if "商品标签" in plain:
            head_plain = plain.split("商品标签", 1)[0]
        elif "商品タグ" in plain:
            head_plain = plain.split("商品タグ", 1)[0]
        else:
            head_plain = plain
        duration_matches = re.findall(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", head_plain)
        runtime = ""
        for matched in duration_matches:
            if len(matched) == 5 and matched.startswith("00:"):
                continue
            runtime = matched
            break

        return AVDetail(
            source="fc2",
            title=title,
            url=_strip_url_query(page_url),
            code=code,
            cover_url=_abs_url(page_url, cover),
            date=date,
            runtime=runtime,
            score=score,
            genres=_split_text_tokens(tags_text, max_len=40)[:16],
            studio=studio,
            summary=summary,
            seeds=[],
        )

    def _extract_madouqu_entities(self, content: str, *, labels: list[str]) -> list[str]:
        if not content:
            return []
        found: list[str] = []
        escaped = "|".join(re.escape(label) for label in labels)
        blocks = re.findall(
            rf"(?:{escaped})\s*[:\uFF1A]\s*(.*?)</(?:p|li|div)>",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for block in blocks[:3]:
            for anchor in re.findall(r"<a[^>]*>(.*?)</a>", block, flags=re.IGNORECASE | re.DOTALL):
                text = _clean_text(_strip_tags(anchor), max_len=50)
                if text and text not in found:
                    found.append(text)
            plain = _clean_text(_strip_tags(block), max_len=180)
            if not plain:
                continue
            for token in re.split(r"[\u3001,/|]", plain):
                text = _clean_text(token, max_len=50)
                if text and text not in found:
                    found.append(text)
        return found

    async def _enrich_with_javbus(
        self,
        session: aiohttp.ClientSession,
        detail: AVDetail,
    ) -> AVDetail:
        std_code = normalize_av_code(detail.code) or _normalize_relaxed_av_code(detail.code)
        if not std_code:
            return detail
        jav_url = f"{self.javbus_base}/{quote(std_code, safe='')}"
        jav_detail = await self._fetch_javbus_detail(session, jav_url)
        if not jav_detail:
            return detail
        if jav_detail.seeds:
            detail.seeds = jav_detail.seeds
        if not detail.cover_url:
            detail.cover_url = jav_detail.cover_url
        if not detail.actors:
            detail.actors = jav_detail.actors
        if not detail.genres:
            detail.genres = jav_detail.genres
        if not detail.date:
            detail.date = jav_detail.date
        if not detail.runtime:
            detail.runtime = jav_detail.runtime
        if not detail.summary:
            detail.summary = jav_detail.summary
        if not detail.studio:
            detail.studio = jav_detail.studio
        if not detail.publisher:
            detail.publisher = jav_detail.publisher
        if not detail.series:
            detail.series = jav_detail.series
        if not detail.director:
            detail.director = jav_detail.director
        if not detail.code:
            detail.code = jav_detail.code
        return detail

    @staticmethod
    def _extract_plain_field(
        text: str,
        *,
        labels: list[str],
        stop_labels: list[str],
        max_len: int,
    ) -> str:
        if not text or not labels:
            return ""
        escaped = "|".join(re.escape(label) for label in labels)
        stop = "|".join(re.escape(label) for label in stop_labels)
        pattern = rf"(?:{escaped})\s*[:：]\s*(.+?)(?=\s*(?:{stop})\s*[:：]|\s*$)"
        return _clean_text(_extract_first(pattern, text), max_len=max_len)

    @staticmethod
    def _extract_dmm_cid(raw_url: str) -> str:
        value = html.unescape((raw_url or "").strip())
        if not value:
            return ""
        matched = re.search(r"/cid=([^/?#]+)/?", value, flags=re.IGNORECASE)
        if matched:
            return matched.group(1)
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        cid = params.get("cid")
        if cid:
            return _clean_text(cid[0], max_len=80)
        return ""

    @staticmethod
    def _normalize_dmm_code(raw: str) -> str:
        return _normalize_relaxed_av_code((raw or "").strip())
