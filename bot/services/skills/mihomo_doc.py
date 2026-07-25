from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import time
import weakref
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import (
    ResponseTooLargeError,
    UnsafeUrlError,
    UnsupportedContentTypeError,
    fetch_text,
)
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_BASE_URL = "https://wiki.metacubex.one/"
_DOC_HOST = "wiki.metacubex.one"
_ALLOWED_HOSTS = (_DOC_HOST,)
_CACHE_TTL_SECONDS = 1800.0
_INDEX_URL = f"{_BASE_URL}search/search_index.json"
_SITEMAP_URL = f"{_BASE_URL}sitemap.xml"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
_CACHE: dict[str, tuple[float, str, str]] = {}
_CACHE_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Lock],
] = weakref.WeakKeyDictionary()


class _MihomoDocError(RuntimeError):
    pass


class _MihomoHttpError(_MihomoDocError):
    def __init__(self, status: int) -> None:
        self.status = int(status)
        super().__init__(f"http_{self.status}")


class _MarkdownConverter(HTMLParser):
    """Small MkDocs-oriented HTML to Markdown converter.

    Config examples must keep their indentation, so this deliberately avoids
    the generic whitespace cleaners used by ordinary web summaries.
    """

    _SKIP = {
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "svg",
        "button",
        "form",
        "label",
        "input",
    }
    _BLOCK = {"div", "section", "article", "blockquote", "dl", "dt", "dd"}

    def __init__(self, *, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.skip_depth = 0
        self.pre_depth = 0
        self.code_inline = False
        self.list_stack: list[int | None] = []
        self.in_table = False
        self.table_rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.skip_header_link = False
        self.admonition_title = False
        self.link_targets: list[str] = []

    def _emit(self, value: str) -> None:
        if self.in_table and self.current_cell is not None:
            self.current_cell.append(value)
        else:
            self.out.append(value)

    def _link_target(self, href: str) -> str:
        raw = (href or "").strip()
        if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
            return ""
        resolved = urljoin(self.base_url or _BASE_URL, raw)
        try:
            parsed = urlparse(resolved)
        except ValueError:
            return ""
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        return resolved.replace(")", "%29")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = attributes.get("class", "")
        if tag in self._SKIP or self.skip_depth:
            self.skip_depth += 1
            return
        if tag == "pre":
            self.pre_depth += 1
            self._emit("\n```\n")
            return
        if self.pre_depth:
            return
        if tag == "a" and "headerlink" in classes:
            self.skip_header_link = True
            return
        if tag == "a":
            target = self._link_target(attributes.get("href", ""))
            self.link_targets.append(target)
            if target:
                self._emit("[")
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "code":
            self.code_inline = True
            self._emit("`")
        elif tag in {"strong", "b"}:
            self._emit("**")
        elif tag in {"em", "i"}:
            self._emit("*")
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")
        elif tag == "ul":
            self.list_stack.append(None)
        elif tag == "ol":
            self.list_stack.append(1)
        elif tag == "li":
            depth = max(len(self.list_stack) - 1, 0)
            indent = "  " * depth
            if self.list_stack and self.list_stack[-1] is not None:
                number = int(self.list_stack[-1] or 1)
                self._emit(f"\n{indent}{number}. ")
                self.list_stack[-1] = number + 1
            else:
                self._emit(f"\n{indent}- ")
        elif tag == "table":
            self.in_table = True
            self.table_rows = []
        elif tag == "tr" and self.in_table:
            self.current_row = []
        elif tag in {"td", "th"} and self.in_table:
            self.current_cell = []
        elif tag == "p":
            if "admonition-title" in classes:
                self.admonition_title = True
                self._emit("\n\n> **")
            else:
                self._emit("\n\n")
        elif tag == "div" and "admonition" in classes.split():
            self._emit("\n")
        elif tag in self._BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "pre":
            self.pre_depth = max(0, self.pre_depth - 1)
            self._emit("\n```\n")
            return
        if self.pre_depth:
            return
        if tag == "a" and self.skip_header_link:
            self.skip_header_link = False
            return
        if tag == "a":
            target = self.link_targets.pop() if self.link_targets else ""
            if target:
                self._emit(f"]({target})")
            return
        if tag == "code" and self.code_inline:
            self.code_inline = False
            self._emit("`")
        elif tag in {"strong", "b"}:
            self._emit("**")
        elif tag in {"em", "i"}:
            self._emit("*")
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._emit("\n")
        elif tag in {"td", "th"} and self.in_table:
            if self.current_row is not None and self.current_cell is not None:
                cell = re.sub(r"\s+", " ", "".join(self.current_cell)).strip()
                self.current_row.append(cell.replace("|", r"\|"))
            self.current_cell = None
        elif tag == "tr" and self.in_table:
            if self.current_row:
                self.table_rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self.in_table = False
            if self.table_rows:
                width = max(len(row) for row in self.table_rows)
                rows = [row + [""] * (width - len(row)) for row in self.table_rows]
                lines = [
                    "| " + " | ".join(rows[0]) + " |",
                    "|" + "---|" * width,
                ]
                lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
                self.out.append("\n\n" + "\n".join(lines) + "\n\n")
        elif tag == "p" and self.admonition_title:
            self.admonition_title = False
            self._emit("**\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth or self.skip_header_link:
            return
        if self.pre_depth:
            self._emit(data)
            return
        if data.strip() or " " in data:
            self._emit(re.sub(r"[ \t]+", " ", data.replace("\n", " ")))

    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"```\n\s*```\n", "", text)
        return text.strip()


def _html_to_markdown(raw_html: str, *, base_url: str = "") -> str:
    article = re.search(r"<article[^>]*>(.*?)</article>", raw_html or "", re.DOTALL | re.IGNORECASE)
    body = article.group(1) if article else raw_html
    body = re.split(r'<nav class="md-footer', body, maxsplit=1)[0]
    body = re.sub(r'<td class="linenos">.*?</td>', "", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(
        r'<table class="highlighttable">(.*?)</table>',
        r"\1",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    converter = _MarkdownConverter(base_url=base_url)
    converter.feed(body)
    converter.close()
    return converter.result()


class _PlainTextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag in self._SKIP or self.skip_depth:
            self.skip_depth += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.out.append(data)

    def result(self, *, max_len: int) -> str:
        return clean_multiline_text("".join(self.out), max_len=max_len)


def _html_fragment_to_text(value: str, *, max_len: int) -> str:
    extractor = _PlainTextExtractor()
    try:
        extractor.feed(value or "")
        extractor.close()
    except Exception:
        return clean_multiline_text(html_lib.unescape(value or ""), max_len=max_len)
    return extractor.result(max_len=max_len)


def _bounded_document_text(value: str, *, max_chars: int) -> tuple[str, bool]:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub(" ", text).strip()
    if len(text) <= max_chars:
        return text, False
    marker = "\n\n…[内容已截断，可缩小范围后重试]"
    return text[: max(1, max_chars - len(marker))].rstrip() + marker, True


def _fetched_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_lock(name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _CACHE_LOCKS.setdefault(loop, {})
    return locks.setdefault(name, asyncio.Lock())


def _cached_raw(name: str, *, fresh: bool) -> str | None:
    if fresh:
        return None
    cached = _CACHE.get(name)
    if not cached or time.monotonic() - cached[0] >= _CACHE_TTL_SECONDS:
        return None
    return cached[1]


def _cache_fetched_at(name: str) -> str:
    cached = _CACHE.get(name)
    return cached[2] if cached else ""


def _parse_sitemap(raw: str) -> list[str]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise _MihomoDocError("invalid_sitemap") from exc

    locations: list[str] = []
    seen: set[str] = set()
    for element in root.iter():
        if not str(element.tag).endswith("loc") or not element.text:
            continue
        try:
            location = _normalize_location(element.text, allow_empty=True)
        except ValueError:
            continue
        if not location or location in seen:
            continue
        seen.add(location)
        locations.append(location)
    if not locations:
        raise _MihomoDocError("empty_sitemap")
    return locations


def _parse_index(raw: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _MihomoDocError("invalid_search_index") from exc
    docs = payload.get("docs") if isinstance(payload, dict) else None
    if not isinstance(docs, list):
        raise _MihomoDocError("invalid_search_index")
    return [item for item in docs if isinstance(item, dict)]


def _normalize_location(value: str, *, allow_empty: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise ValueError("empty_location")
    if (
        len(raw) > 300
        or any(ord(char) <= 32 or ord(char) == 127 for char in raw)
        or "\\" in raw
    ):
        raise ValueError("invalid_location")

    if "://" in raw:
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid_location") from exc
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower().rstrip(".") != _DOC_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
        ):
            raise ValueError("invalid_location")
        raw = parsed.path
    else:
        raw = raw.split("#", 1)[0]
        if "?" in raw:
            raise ValueError("invalid_location")

    raw = raw.lstrip("/")
    if raw.startswith("None"):
        raw = raw[4:]
    raw = re.sub(r"/+", "/", raw).strip("/")
    if not raw:
        if allow_empty:
            return ""
        raise ValueError("empty_location")
    segments = raw.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("invalid_location")
    if not re.fullmatch(r"[A-Za-z0-9%._~/-]+", raw):
        raise ValueError("invalid_location")
    return raw + "/"


def _language_of(location: str) -> str:
    if location.startswith("en/"):
        return "en"
    if location.startswith("ru/"):
        return "ru"
    return "zh"


def _title_from_markdown(content: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", content or "")
    if not match:
        return fallback
    heading = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", match.group(1))
    heading = heading.replace("`", "").replace("**", "").strip()
    return clean_text(heading, max_len=200) or fallback


class MihomoDocSkill:
    name = "mihomo_doc"
    description = (
        "实时查询 mihomo（Clash Meta）官方 Wiki。凡涉及 mihomo/Clash Meta 配置字段、"
        "代理协议、代理组、规则、DNS、TUN、入站、嗅探、API、报错排查，或编写/审查 "
        "config.yaml，必须先调用此工具，不得凭模型记忆回答。先 search 定位，再 page/section "
        "读取官方原文；回答时引用 payload 中的 source_url。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "page", "section", "toc"],
                "description": "search 搜索索引；page 读取单页；section 读取某路径下多页；toc 浏览目录。",
            },
            "query": {
                "type": "string",
                "maxLength": 160,
                "description": "search 的关键词。中文关键词通常更完整。",
            },
            "location": {
                "type": "string",
                "maxLength": 300,
                "description": "page 的文档路径，如 config/proxies/vless/。",
            },
            "prefix": {
                "type": "string",
                "maxLength": 300,
                "description": "section 的路径前缀，如 config/dns/。",
            },
            "filter": {
                "type": "string",
                "maxLength": 100,
                "description": "toc 的可选标题/路径过滤词。",
            },
            "lang": {
                "type": "string",
                "enum": ["zh", "en", "ru"],
                "default": "zh",
                "description": "search/toc 的文档语言；默认中文。",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 8,
                "description": "search 返回 1-10 条；toc 返回 1-20 条。",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "default": 4,
                "description": "section 最多读取的页面数。",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000,
                "default": 0,
                "description": "section/toc 从第几个匹配页面开始，用于继续读取后续页面。",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 20000,
                "description": "page/section 返回的正文字符预算。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    @staticmethod
    def execution_timeout_seconds(arguments: dict[str, Any]) -> float:
        action = str(arguments.get("action") or "").strip().lower()
        return {
            "search": 45.0,
            "page": 45.0,
            "toc": 50.0,
            "section": 90.0,
        }.get(action, 25.0)

    @staticmethod
    def _int_argument(
        arguments: dict[str, Any],
        name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(arguments.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    async def _fetch(
        self,
        url: str,
        *,
        allowed_content_types: tuple[str, ...],
        max_response_bytes: int,
        max_decoded_bytes: int,
    ) -> tuple[str, str, str]:
        status, raw, final_url, content_type = await fetch_text(
            url,
            headers={
                "User-Agent": "SmartGroupBot/1.0 mihomo-doc",
                "Accept": "text/html,application/json,application/xml,text/xml,text/plain;q=0.9",
            },
            timeout_sec=18.0,
            allow_redirects=False,
            allowed_hosts=_ALLOWED_HOSTS,
            allowed_content_types=allowed_content_types,
            max_response_bytes=max_response_bytes,
            max_decoded_bytes=max_decoded_bytes,
            max_redirects=3,
        )
        if status >= 300:
            raise _MihomoHttpError(status)
        try:
            parsed_final = urlparse(final_url)
            final_host = (parsed_final.hostname or "").lower().rstrip(".")
            final_port = parsed_final.port
        except ValueError as exc:
            raise UnsafeUrlError("invalid_final_url") from exc
        if (
            parsed_final.scheme.lower() != "https"
            or final_host != _DOC_HOST
            or final_port not in {None, 443}
        ):
            raise UnsafeUrlError("unexpected_final_host")
        return raw, final_url, content_type

    async def _sitemap(self, *, fresh: bool) -> list[str]:
        raw = _cached_raw("sitemap", fresh=fresh)
        if raw is not None:
            return _parse_sitemap(raw)

        async with _cache_lock("sitemap"):
            raw = _cached_raw("sitemap", fresh=fresh)
            if raw is not None:
                return _parse_sitemap(raw)
            raw, _, _ = await self._fetch(
                _SITEMAP_URL,
                allowed_content_types=("application/xml", "text/xml", "text/plain"),
                max_response_bytes=2 * 1024 * 1024,
                max_decoded_bytes=4 * 1024 * 1024,
            )
            locations = _parse_sitemap(raw)
            _CACHE["sitemap"] = (time.monotonic(), raw, _fetched_at())
            return locations

    async def _index(self, *, fresh: bool) -> list[dict[str, Any]]:
        raw = _cached_raw("search_index", fresh=fresh)
        if raw is not None:
            return _parse_index(raw)

        async with _cache_lock("search_index"):
            raw = _cached_raw("search_index", fresh=fresh)
            if raw is not None:
                return _parse_index(raw)
            raw, _, _ = await self._fetch(
                _INDEX_URL,
                allowed_content_types=("application/json", "text/json", "text/plain"),
                max_response_bytes=8 * 1024 * 1024,
                max_decoded_bytes=12 * 1024 * 1024,
            )
            docs = _parse_index(raw)
            _CACHE["search_index"] = (time.monotonic(), raw, _fetched_at())
            return docs

    async def _run_search(self, arguments: dict[str, Any]) -> SkillRunResult:
        query = clean_text(str(arguments.get("query") or ""), max_len=160)
        if not query:
            return SkillRunResult(False, self.name, "Mihomo 文档搜索词为空", error="empty_query")
        lang = str(arguments.get("lang") or "zh").strip().lower()
        if lang not in {"zh", "en", "ru"}:
            lang = "zh"
        limit = self._int_argument(arguments, "max_results", 8, 1, 10)
        docs = await self._index(fresh=False)
        terms = [term.lower() for term in query.split() if term.strip()]
        if not terms:
            terms = [query.lower()]

        pages: dict[str, dict[str, Any]] = {}
        for document in docs:
            raw_location = str(document.get("location") or "").strip()
            if not raw_location:
                continue
            anchor = "#" + raw_location.split("#", 1)[1] if "#" in raw_location else ""
            try:
                location = _normalize_location(raw_location, allow_empty=True)
            except ValueError:
                continue
            if not location or _language_of(location) != lang:
                continue
            title = clean_text(
                _html_fragment_to_text(str(document.get("title") or ""), max_len=200),
                max_len=200,
            )
            body = _html_fragment_to_text(
                str(document.get("text") or ""),
                max_len=60000,
            )
            searchable = f"{title} {body}".lower()
            hits = [searchable.count(term) for term in terms]
            if not any(hits):
                continue
            score = sum(hits) + sum(20 for term in terms if term in title.lower())
            if len(terms) > 1 and not all(hits):
                score = max(1, score // 4)

            page = pages.setdefault(
                location,
                {
                    "score": 0,
                    "title": "",
                    "snippet": "",
                    "sections": [],
                },
            )
            page["score"] += score
            if not anchor and title:
                page["title"] = title
            elif anchor:
                page["sections"].append(
                    {
                        "anchor": clean_text(anchor, max_len=160),
                        "title": title,
                        "score": score,
                    }
                )
            if not page["snippet"] and body:
                lowered = body.lower()
                positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
                position = min(positions, default=0)
                start = max(0, position - 60)
                excerpt = body[start : start + 360].strip()
                page["snippet"] = ("…" if start else "") + excerpt + ("…" if start + 360 < len(body) else "")

        ranked = sorted(pages.items(), key=lambda item: (-int(item[1]["score"]), item[0]))[:limit]
        results: list[dict[str, Any]] = []
        for location, page in ranked:
            sections = sorted(page["sections"], key=lambda item: -int(item["score"]))[:4]
            for section in sections:
                section.pop("score", None)
            results.append(
                {
                    "title": page["title"] or location,
                    "location": location,
                    "url": _BASE_URL + location,
                    "score": int(page["score"]),
                    "sections": sections,
                    "snippet": clean_multiline_text(page["snippet"], max_len=420),
                }
            )

        if not results:
            return SkillRunResult(
                False,
                self.name,
                "Mihomo 官方文档中未找到相关结果，可换更短或中文关键词重试",
                payload={"action": "search", "query": query, "lang": lang, "results": []},
                error="no_results",
            )
        return SkillRunResult(
            True,
            self.name,
            f"找到 {len(results)} 条 Mihomo 官方文档结果",
            payload={
                "action": "search",
                "query": query,
                "lang": lang,
                "source_url": _INDEX_URL,
                "fetched_at": _cache_fetched_at("search_index") or _fetched_at(),
                "results": results,
                "next_step": "选择最相关的 location，再用 action=page 读取原文；宽泛主题可用 action=section。",
            },
        )

    async def _run_page(self, arguments: dict[str, Any]) -> SkillRunResult:
        try:
            location = _normalize_location(str(arguments.get("location") or ""))
        except ValueError as exc:
            return SkillRunResult(False, self.name, "Mihomo 文档路径无效", error=str(exc))
        max_chars = self._int_argument(arguments, "max_chars", 16000, 1000, 20000)
        raw, final_url, content_type = await self._fetch(
            _BASE_URL + location,
            allowed_content_types=("text/html", "application/xhtml+xml", "text/plain"),
            max_response_bytes=1024 * 1024,
            max_decoded_bytes=2 * 1024 * 1024,
        )
        content, truncated = _bounded_document_text(
            _html_to_markdown(raw, base_url=final_url),
            max_chars=max_chars,
        )
        if not content:
            return SkillRunResult(False, self.name, "Mihomo 文档页面为空或无法解析", error="empty_content")
        title = _title_from_markdown(content, location)
        return SkillRunResult(
            True,
            self.name,
            f"已读取 Mihomo 官方文档：{title}",
            payload={
                "action": "page",
                "location": location,
                "title": title,
                "source_url": final_url,
                "fetched_at": _fetched_at(),
                "content_type": content_type,
                "content": content,
                "truncated": truncated,
            },
        )

    async def _run_section(self, arguments: dict[str, Any]) -> SkillRunResult:
        try:
            prefix = _normalize_location(str(arguments.get("prefix") or ""))
        except ValueError as exc:
            return SkillRunResult(False, self.name, "Mihomo 文档章节路径无效", error=str(exc))
        max_pages = self._int_argument(arguments, "max_pages", 4, 1, 6)
        max_chars = self._int_argument(arguments, "max_chars", 16000, 2000, 18000)
        offset = self._int_argument(arguments, "offset", 0, 0, 1000)
        locations = sorted(
            location
            for location in await self._sitemap(fresh=False)
            if location.startswith(prefix)
        )
        if not locations:
            return SkillRunResult(
                False,
                self.name,
                "该路径下没有 Mihomo 官方文档页面",
                payload={"action": "section", "prefix": prefix},
                error="section_not_found",
            )

        if offset >= len(locations):
            return SkillRunResult(
                False,
                self.name,
                "Mihomo 文档章节偏移量超出范围",
                payload={
                    "action": "section",
                    "prefix": prefix,
                    "offset": offset,
                    "total_pages": len(locations),
                },
                error="offset_out_of_range",
            )

        selected = locations[offset : offset + max_pages]

        async def fetch_page(location: str) -> tuple[str, str, str]:
            raw, final_url, _ = await self._fetch(
                _BASE_URL + location,
                allowed_content_types=("text/html", "application/xhtml+xml", "text/plain"),
                max_response_bytes=1024 * 1024,
                max_decoded_bytes=2 * 1024 * 1024,
            )
            return location, final_url, _html_to_markdown(raw, base_url=final_url)

        pages: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        remaining = max_chars
        budget_truncated = False
        consumed = 0
        for batch_start in range(0, len(selected), 3):
            batch = selected[batch_start : batch_start + 3]
            fetched = await asyncio.gather(
                *(fetch_page(location) for location in batch),
                return_exceptions=True,
            )
            for location, item in zip(batch, fetched, strict=True):
                if remaining <= 0:
                    budget_truncated = True
                    break
                consumed += 1
                if isinstance(item, BaseException):
                    errors.append(
                        {
                            "location": location,
                            "error": clean_text(str(item) or item.__class__.__name__, max_len=160),
                        }
                    )
                    continue
                _, source_url, markdown = item
                content, truncated = _bounded_document_text(markdown, max_chars=remaining)
                if not content:
                    errors.append({"location": location, "error": "empty_content"})
                    continue
                pages.append(
                    {
                        "location": location,
                        "title": _title_from_markdown(content, location),
                        "source_url": source_url,
                        "content": content,
                        "truncated": truncated,
                    }
                )
                remaining -= len(content)
                if truncated:
                    budget_truncated = True
                    break
            if budget_truncated:
                break

        if not pages:
            return SkillRunResult(
                False,
                self.name,
                "Mihomo 官方文档章节读取失败",
                payload={"action": "section", "prefix": prefix, "errors": errors},
                error="section_fetch_failed",
            )
        next_offset = offset + consumed
        has_more = next_offset < len(locations)
        truncated = budget_truncated or has_more or bool(errors)
        return SkillRunResult(
            True,
            self.name,
            f"已读取 Mihomo 官方文档章节，共 {len(pages)} 页",
            payload={
                "action": "section",
                "prefix": prefix,
                "offset": offset,
                "fetched_at": _fetched_at(),
                "total_pages": len(locations),
                "returned_pages": len(pages),
                "pages": pages,
                "errors": errors,
                "truncated": truncated,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "next_locations": locations[next_offset : next_offset + 6],
            },
        )

    async def _run_toc(self, arguments: dict[str, Any]) -> SkillRunResult:
        lang = str(arguments.get("lang") or "zh").strip().lower()
        if lang not in {"zh", "en", "ru"}:
            lang = "zh"
        filter_text = clean_text(str(arguments.get("filter") or ""), max_len=100).lower()
        limit = self._int_argument(arguments, "max_results", 20, 1, 20)
        offset = self._int_argument(arguments, "offset", 0, 0, 1000)
        sitemap_result, index_result = await asyncio.gather(
            self._sitemap(fresh=False),
            self._index(fresh=False),
            return_exceptions=True,
        )
        if isinstance(sitemap_result, BaseException):
            raise sitemap_result
        locations = sitemap_result
        titles: dict[str, str] = {}
        if isinstance(index_result, BaseException):
            log.warning("mihomo_doc toc could not load titles: %s", index_result)
        else:
            for document in index_result:
                raw_location = str(document.get("location") or "")
                if "#" in raw_location:
                    continue
                try:
                    location = _normalize_location(raw_location, allow_empty=True)
                except ValueError:
                    continue
                if location:
                    titles[location] = clean_text(
                        _html_fragment_to_text(str(document.get("title") or ""), max_len=200),
                        max_len=200,
                    )

        matches: list[dict[str, str]] = []
        for location in locations:
            if _language_of(location) != lang:
                continue
            title = titles.get(location, "")
            if filter_text and filter_text not in f"{location} {title}".lower():
                continue
            matches.append({"location": location, "title": title or location, "url": _BASE_URL + location})
        results = matches[offset : offset + limit]
        if not results:
            return SkillRunResult(
                False,
                self.name,
                "Mihomo 官方文档目录中没有匹配页面",
                payload={
                    "action": "toc",
                    "lang": lang,
                    "filter": filter_text,
                    "offset": offset,
                    "total_matches": len(matches),
                    "results": [],
                },
                error="no_results",
            )
        next_offset = offset + len(results)
        has_more = next_offset < len(matches)
        return SkillRunResult(
            True,
            self.name,
            f"列出 {len(results)} 个 Mihomo 官方文档页面",
            payload={
                "action": "toc",
                "lang": lang,
                "filter": filter_text,
                "offset": offset,
                "source_url": _SITEMAP_URL,
                "fetched_at": _cache_fetched_at("sitemap") or _fetched_at(),
                "total_matches": len(matches),
                "results": results,
                "truncated": has_more,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
            },
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context
        action = clean_text(str(arguments.get("action") or ""), max_len=24).lower()
        try:
            if action == "search":
                return await self._run_search(arguments)
            if action == "page":
                return await self._run_page(arguments)
            if action == "section":
                return await self._run_section(arguments)
            if action == "toc":
                return await self._run_toc(arguments)
            return SkillRunResult(False, self.name, "未知的 Mihomo 文档操作", error="invalid_action")
        except _MihomoHttpError as exc:
            return SkillRunResult(
                False,
                self.name,
                f"Mihomo 官方文档请求失败：HTTP {exc.status}",
                error=f"http_{exc.status}",
            )
        except UnsafeUrlError as exc:
            log.warning("mihomo_doc rejected unsafe URL: %s", exc)
            return SkillRunResult(False, self.name, "Mihomo 文档地址安全校验失败", error=str(exc))
        except ResponseTooLargeError as exc:
            return SkillRunResult(False, self.name, "Mihomo 官方文档响应过大", error=str(exc))
        except UnsupportedContentTypeError as exc:
            return SkillRunResult(False, self.name, "Mihomo 官方文档返回了不支持的内容类型", error=str(exc))
        except TimeoutError:
            return SkillRunResult(False, self.name, "Mihomo 官方文档请求超时", error="timeout")
        except _MihomoDocError as exc:
            log.warning("mihomo_doc invalid upstream data: %s", exc)
            return SkillRunResult(False, self.name, "Mihomo 官方文档数据格式异常", error=str(exc))
        except Exception as exc:
            log.exception("mihomo_doc failed | action=%s", action)
            return SkillRunResult(False, self.name, "Mihomo 官方文档查询失败", error=str(exc))
