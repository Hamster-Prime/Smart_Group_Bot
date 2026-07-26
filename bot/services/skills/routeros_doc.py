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
from urllib.parse import unquote, urljoin, urlparse
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

_BASE_URL = "https://manual.mikrotik.com/"
_DOC_HOST = "manual.mikrotik.com"
_ALLOWED_HOSTS = (_DOC_HOST,)
_CACHE_TTL_SECONDS = 1800.0
_INDEX_URL = f"{_BASE_URL}search-doc.json"
_SITEMAP_URL = f"{_BASE_URL}sitemap.xml"
_CLI_PREFIX = "docs/cli-reference"
_CHANGELOG_RE = re.compile(r"^changelog/changelog-(\d{4}-\d{2}-\d{2})$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\ufeff]")
_LOCATION_SEGMENT_RE = re.compile(r"[-A-Za-z0-9._~%!$&'()*+,;=:@]+")
_CACHE: dict[str, tuple[float, str, str]] = {}
_CACHE_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Lock],
] = weakref.WeakKeyDictionary()


class _RouterOSDocError(RuntimeError):
    pass


class _RouterOSHttpError(_RouterOSDocError):
    def __init__(self, status: int) -> None:
        self.status = int(status)
        super().__init__(f"http_{self.status}")


class _MarkdownConverter(HTMLParser):
    """Small Docusaurus-oriented HTML to Markdown converter."""

    _SKIP = {
        "script",
        "style",
        "head",
        "noscript",
        "nav",
        "footer",
        "svg",
        "button",
        "form",
        "label",
        "input",
        "select",
        "option",
    }
    _BLOCK = {
        "div",
        "section",
        "article",
        "blockquote",
        "dl",
        "dt",
        "dd",
        "figure",
        "details",
        "summary",
    }

    def __init__(self, *, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.skip_depth = 0
        self.hash_link_depth = 0
        self.pre_depth = 0
        self.pre_opened = False
        self.pre_language = ""
        self.code_inline = False
        self.list_stack: list[int | None] = []
        self.in_table = False
        self.table_rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.link_targets: list[str] = []

    def _emit(self, value: str) -> None:
        if self.in_table and self.current_cell is not None:
            self.current_cell.append(value)
        else:
            self.out.append(value)

    @staticmethod
    def _language(classes: str) -> str:
        match = re.search(r"(?:^|\s)language-([\w+-]+)(?:\s|$)", classes or "")
        language = match.group(1).lower() if match else ""
        return "" if language in {"text", "none", "plain", "plaintext"} else language

    def _open_pre(self) -> None:
        if self.pre_opened:
            return
        self._emit(f"\n```{self.pre_language}\n")
        self.pre_opened = True

    def _link_target(self, href: str) -> str:
        raw = (href or "").strip()
        if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
            return ""
        resolved = urljoin(self.base_url or _BASE_URL, raw)
        try:
            parsed = urlparse(resolved)
        except ValueError:
            return ""
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return ""
        return resolved.replace(")", "%29")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = attributes.get("class", "")
        if self.skip_depth:
            self.skip_depth += 1
            return
        if self.hash_link_depth:
            self.hash_link_depth += 1
            return
        if tag in self._SKIP or "admonitionHeading" in classes:
            self.skip_depth = 1
            return
        if tag == "a" and "hash-link" in classes.split():
            self.hash_link_depth = 1
            return
        if tag == "pre":
            self.pre_depth += 1
            self.pre_opened = False
            self.pre_language = self._language(classes)
            return
        if self.pre_depth:
            if tag == "code" and not self.pre_language:
                self.pre_language = self._language(classes)
            if tag == "br":
                self._open_pre()
                self._emit("\n")
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
            self._emit("<br>" if self.in_table else "\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")
        elif tag == "img":
            alt = clean_text(attributes.get("alt", ""), max_len=120)
            self._emit(f"[image{': ' + alt if alt else ''}]")
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
        elif tag == "div" and "theme-admonition" in classes.split():
            semantic = re.search(
                r"(?:^|\s)theme-admonition-(note|tip|info|warning|danger|caution)(?:\s|$)",
                classes,
                re.IGNORECASE,
            )
            kind = semantic.group(1).upper() if semantic else "NOTE"
            self._emit(f"\n\n> **{kind}:** ")
        elif tag in self._BLOCK:
            self._emit("\n\n" if tag in {"p", "blockquote"} else "\n")
        elif tag == "p":
            self._emit("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if self.hash_link_depth:
            self.hash_link_depth -= 1
            return
        if tag == "pre":
            self._open_pre()
            self.pre_depth = max(0, self.pre_depth - 1)
            self._emit("\n```\n")
            self.pre_opened = False
            self.pre_language = ""
            return
        if self.pre_depth:
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
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth or self.hash_link_depth:
            return
        if self.pre_depth:
            self._open_pre()
            self._emit(data)
            return
        if data.strip() or " " in data:
            self._emit(re.sub(r"[ \t]+", " ", data.replace("\n", " ")))

    def result(self) -> str:
        text = _ZERO_WIDTH_RE.sub("", "".join(self.out))
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"```\n\s*```\n", "", text)
        return text.strip()


def _html_to_markdown(raw_html: str, *, base_url: str = "") -> str:
    source = raw_html or ""
    article = re.search(r"<article\b[^>]*>(.*?)</article>", source, re.DOTALL | re.IGNORECASE)
    article_body = article.group(1) if article else source
    # Docusaurus puts a malformed/minified breadcrumb tree before the actual
    # document.  Start at the theme-doc-markdown container so unclosed
    # breadcrumb tags cannot leave the converter's skip depth stuck for the
    # entire page.  Keep the opening container in the fragment so nested
    # document blocks remain balanced enough for HTMLParser.
    themed = re.search(
        r'<div\b[^>]*class=["\'][^"\']*\btheme-doc-markdown\b[^"\']*["\'][^>]*>',
        article_body,
        re.DOTALL | re.IGNORECASE,
    )
    body = article_body[themed.start() :] if themed else article_body
    converter = _MarkdownConverter(base_url=base_url)
    converter.feed(body)
    converter.close()
    return converter.result()


class _PlainTextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if self.skip_depth:
            self.skip_depth += 1
        elif tag in self._SKIP:
            self.skip_depth = 1
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
        return clean_multiline_text(_ZERO_WIDTH_RE.sub("", "".join(self.out)), max_len=max_len)


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
    marker = "\n\n…[内容已截断，可缩小范围或提高 max_chars 后重试]"
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


def _normalize_location(value: str, *, allow_empty: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise ValueError("empty_location")
    if (
        len(raw) > 500
        or any(ord(char) <= 32 or ord(char) == 127 for char in raw)
        or "\\" in raw
        or raw.startswith("//")
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

    raw = raw.strip("/")
    if not raw:
        if allow_empty:
            return ""
        raise ValueError("empty_location")
    if "//" in raw:
        raise ValueError("invalid_location")

    segments = raw.split("/")
    for segment in segments:
        if not segment or not _LOCATION_SEGMENT_RE.fullmatch(segment):
            raise ValueError("invalid_location")
        if re.search(r"%(?![0-9A-Fa-f]{2})", segment):
            raise ValueError("invalid_location")
        decoded = unquote(segment)
        if (
            decoded in {"", ".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or "?" in decoded
            or "#" in decoded
            or any(ord(char) <= 32 or ord(char) == 127 for char in decoded)
        ):
            raise ValueError("invalid_location")
    return "/".join(segments)


def _url_for_location(location: str) -> str:
    return f"{_BASE_URL}{location.strip('/')}/"


def _parse_sitemap(raw: str) -> list[str]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise _RouterOSDocError("invalid_sitemap") from exc

    locations: set[str] = set()
    for element in root.iter():
        if not str(element.tag).endswith("loc") or not element.text:
            continue
        try:
            location = _normalize_location(element.text, allow_empty=True)
        except ValueError:
            continue
        if location:
            locations.add(location)
    if not locations:
        raise _RouterOSDocError("empty_sitemap")
    return sorted(locations)


def _parse_index(raw: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _RouterOSDocError("invalid_search_index") from exc
    docs = payload.get("searchDocs") if isinstance(payload, dict) else None
    if not isinstance(docs, list):
        raise _RouterOSDocError("invalid_search_index")
    return [item for item in docs if isinstance(item, dict)]


def _is_doc_location(location: str) -> bool:
    return (location == "docs" or location.startswith("docs/")) and not (
        location == "docs/tags" or location.startswith("docs/tags/")
    )


def _index_location(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty_location")
    try:
        fragment = urlparse(raw).fragment if "://" in raw else raw.partition("#")[2]
    except ValueError as exc:
        raise ValueError("invalid_location") from exc
    location = _normalize_location(raw)
    anchor = clean_text(f"#{fragment}" if fragment else "", max_len=180)
    return location, anchor


def _section_anchor(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return ""
    if "#" in raw:
        raw = raw.rsplit("#", 1)[-1]
    raw = raw.lstrip("#")
    if not raw or len(raw) > 170 or not re.fullmatch(r"[-A-Za-z0-9._~:%]+", raw):
        return ""
    return f"#{raw}"


def _title_from_markdown(content: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", content or "")
    if not match:
        return fallback
    heading = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", match.group(1))
    heading = heading.replace("`", "").replace("**", "").strip()
    return clean_text(heading, max_len=200) or fallback


class RouterOSDocSkill:
    name = "routeros_doc"
    description = (
        "实时查询 MikroTik RouterOS 官方手册。凡涉及 RouterOS/MikroTik 配置、CLI 命令、"
        "防火墙/NAT/mangle、路由、VPN、Bridge/VLAN、DHCP/DNS、无线、队列、脚本、容器、"
        "更新日志或故障排查，必须先调用此工具，不得凭模型记忆补参数。先 search 定位，再 "
        "page/section 读取正文；编写或审查命令时还要用 cli 核对精确语法，并引用 source_url。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "page", "section", "toc", "cli", "changelog"],
                "description": (
                    "search 搜索；page 读单页；section 读路径下多页；toc 浏览目录；"
                    "cli 查询终端命令参考；changelog 列出近期更新记录。"
                ),
            },
            "query": {
                "type": "string",
                "maxLength": 160,
                "description": "search 的英文关键词，建议使用 1-2 个具体术语。",
            },
            "location": {
                "type": "string",
                "maxLength": 500,
                "description": "page 的路径，如 docs/virtual-private-networks/wireguard。",
            },
            "prefix": {
                "type": "string",
                "maxLength": 500,
                "description": "section 的路径前缀，如 docs/firewall-and-quality-of-service。",
            },
            "path": {
                "type": "string",
                "maxLength": 400,
                "description": "cli 的菜单路径，如 ip/firewall/nat；留空时列出 CLI 参考页。",
            },
            "filter": {
                "type": "string",
                "maxLength": 100,
                "description": "toc 或 cli 列表的可选路径/标题过滤词。",
            },
            "include_all": {
                "type": "boolean",
                "default": False,
                "description": "toc 是否包含 blog、changelog 等非 docs 页面。",
            },
            "fresh": {
                "type": "boolean",
                "default": False,
                "description": "是否绕过 sitemap/search 索引的 30 分钟缓存。页面正文始终实时获取。",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 8,
                "description": "search/toc/cli 列表最多返回的条目数。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 15,
                "description": "changelog 最多返回的条目数。",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "default": 4,
                "description": "section 本次最多读取的页面数。",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000,
                "default": 0,
                "description": "search/section/toc/cli/changelog 的分页偏移量。",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 20000,
                "description": "page/section/cli 正文的字符预算。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    @staticmethod
    def execution_timeout_seconds(arguments: dict[str, Any]) -> float:
        action = str(arguments.get("action") or "").strip().lower()
        if action == "section":
            return 90.0
        if action == "cli" and str(arguments.get("path") or "").strip():
            return 50.0
        return {
            "search": 45.0,
            "page": 45.0,
            "toc": 50.0,
            "cli": 50.0,
            "changelog": 50.0,
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

    @staticmethod
    def _bool_argument(arguments: dict[str, Any], name: str, default: bool = False) -> bool:
        value = arguments.get(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

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
                "User-Agent": "SmartGroupBot/1.0 routeros-doc",
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
            raise _RouterOSHttpError(status)
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
            or parsed_final.username is not None
            or parsed_final.password is not None
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

    async def _fetch_page(self, location: str) -> tuple[str, str, str]:
        raw, final_url, content_type = await self._fetch(
            _url_for_location(location),
            allowed_content_types=("text/html", "application/xhtml+xml", "text/plain"),
            max_response_bytes=1024 * 1024,
            max_decoded_bytes=2 * 1024 * 1024,
        )
        return _html_to_markdown(raw, base_url=final_url), final_url, content_type

    async def _run_search(self, arguments: dict[str, Any]) -> SkillRunResult:
        query = clean_text(str(arguments.get("query") or ""), max_len=160)
        if not query:
            return SkillRunResult(False, self.name, "RouterOS 文档搜索词为空", error="empty_query")
        fresh = self._bool_argument(arguments, "fresh")
        limit = self._int_argument(arguments, "max_results", 8, 1, 20)
        offset = self._int_argument(arguments, "offset", 0, 0, 1000)
        docs = await self._index(fresh=fresh)
        terms = [term.lower() for term in query.split() if term.strip()] or [query.lower()]

        pages: dict[str, dict[str, Any]] = {}
        known_titles: dict[str, str] = {}
        for document in docs:
            try:
                location, anchor = _index_location(str(document.get("url") or ""))
            except ValueError:
                continue
            if not anchor:
                anchor = _section_anchor(document.get("sectionRef"))
            if not _is_doc_location(location):
                continue
            title = clean_text(
                _html_fragment_to_text(str(document.get("title") or ""), max_len=200),
                max_len=200,
            )
            is_page = str(document.get("type", "")) == "0" or not anchor
            if is_page and title:
                known_titles[location] = title
            body = _html_fragment_to_text(str(document.get("content") or ""), max_len=60000)
            searchable = f"{title} {body}".lower()
            hits = [searchable.count(term) for term in terms]
            if not any(hits):
                continue
            score = sum(hits) + sum(25 for term in terms if term in title.lower())
            if len(terms) > 1 and not all(hits):
                score //= 4
                if not score:
                    continue

            page = pages.setdefault(
                location,
                {"score": 0, "title": "", "snippet": "", "sections": []},
            )
            page["score"] += score
            if is_page and title:
                page["title"] = title
            elif title:
                page["sections"].append(
                    {"anchor": anchor, "title": title, "score": score}
                )
            if not page["snippet"] and body:
                lowered = body.lower()
                positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
                position = min(positions, default=0)
                start = max(0, position - 60)
                excerpt = body[start : start + 360].strip()
                page["snippet"] = (
                    ("…" if start else "")
                    + excerpt
                    + ("…" if start + 360 < len(body) else "")
                )

        ranked = sorted(pages.items(), key=lambda item: (-int(item[1]["score"]), item[0]))
        selected = ranked[offset : offset + limit]
        results: list[dict[str, Any]] = []
        for location, page in selected:
            sections: list[dict[str, str]] = []
            seen_sections: set[tuple[str, str]] = set()
            for section in sorted(page["sections"], key=lambda item: -int(item["score"])):
                key = (str(section.get("anchor") or ""), str(section.get("title") or ""))
                if key in seen_sections:
                    continue
                seen_sections.add(key)
                sections.append({"anchor": key[0], "title": key[1]})
                if len(sections) >= 4:
                    break
            results.append(
                {
                    "title": page["title"] or known_titles.get(location) or location,
                    "location": location,
                    "url": _url_for_location(location),
                    "score": int(page["score"]),
                    "sections": sections,
                    "snippet": clean_multiline_text(page["snippet"], max_len=420),
                }
            )

        if not results:
            return SkillRunResult(
                False,
                self.name,
                "RouterOS 官方文档中未找到相关结果，可换更短的英文关键词重试",
                payload={
                    "action": "search",
                    "query": query,
                    "offset": offset,
                    "source_url": _INDEX_URL,
                    "fetched_at": _cache_fetched_at("search_index") or _fetched_at(),
                    "results": [],
                },
                error="no_results",
            )
        next_offset = offset + len(results)
        has_more = next_offset < len(ranked)
        return SkillRunResult(
            True,
            self.name,
            f"找到 {len(results)} 条 RouterOS 官方文档结果",
            payload={
                "action": "search",
                "query": query,
                "offset": offset,
                "source_url": _INDEX_URL,
                "fetched_at": _cache_fetched_at("search_index") or _fetched_at(),
                "total_matches": len(ranked),
                "results": results,
                "truncated": has_more,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "next_step": "选择最相关的 location，再用 action=page 读取正文；宽泛主题可用 action=section。",
            },
        )

    async def _run_page(self, arguments: dict[str, Any]) -> SkillRunResult:
        try:
            location = _normalize_location(str(arguments.get("location") or ""))
        except ValueError as exc:
            return SkillRunResult(False, self.name, "RouterOS 文档路径无效", error=str(exc))
        max_chars = self._int_argument(arguments, "max_chars", 16000, 1000, 20000)
        markdown, final_url, content_type = await self._fetch_page(location)
        content, truncated = _bounded_document_text(markdown, max_chars=max_chars)
        if not content:
            return SkillRunResult(False, self.name, "RouterOS 文档页面为空或无法解析", error="empty_content")
        title = _title_from_markdown(content, location)
        return SkillRunResult(
            True,
            self.name,
            f"已读取 RouterOS 官方文档：{title}",
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
            return SkillRunResult(False, self.name, "RouterOS 文档章节路径无效", error=str(exc))
        fresh = self._bool_argument(arguments, "fresh")
        max_pages = self._int_argument(arguments, "max_pages", 4, 1, 6)
        max_chars = self._int_argument(arguments, "max_chars", 16000, 2000, 18000)
        offset = self._int_argument(arguments, "offset", 0, 0, 1000)
        locations = [
            location
            for location in await self._sitemap(fresh=fresh)
            if (location == prefix or location.startswith(prefix + "/"))
            and (_is_doc_location(location) if prefix.startswith("docs") else True)
        ]
        if not locations:
            return SkillRunResult(
                False,
                self.name,
                "该路径下没有 RouterOS 官方文档页面",
                payload={"action": "section", "prefix": prefix},
                error="section_not_found",
            )
        if offset >= len(locations):
            return SkillRunResult(
                False,
                self.name,
                "RouterOS 文档章节偏移量超出范围",
                payload={
                    "action": "section",
                    "prefix": prefix,
                    "offset": offset,
                    "total_pages": len(locations),
                },
                error="offset_out_of_range",
            )

        selected = locations[offset : offset + max_pages]
        pages: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        remaining = max_chars
        budget_truncated = False
        consumed = 0
        for batch_start in range(0, len(selected), 3):
            batch = selected[batch_start : batch_start + 3]
            fetched = await asyncio.gather(
                *(self._fetch_page(location) for location in batch),
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
                markdown, source_url, _content_type = item
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

        next_offset = offset + consumed
        has_more = next_offset < len(locations)
        retry_locations = [error["location"] for error in errors]
        if not pages:
            retry_preview = "、".join(retry_locations[:3])
            retry_suffix = f"；可用 action=page 逐页重试：{retry_preview}" if retry_preview else ""
            return SkillRunResult(
                False,
                self.name,
                "RouterOS 官方文档章节读取失败" + retry_suffix,
                payload={
                    "action": "section",
                    "prefix": prefix,
                    "offset": offset,
                    "total_pages": len(locations),
                    "errors": errors,
                    "retry_locations": retry_locations,
                    "truncated": True,
                    "has_more": has_more,
                    "next_offset": next_offset if has_more else None,
                    "next_locations": locations[next_offset : next_offset + 6],
                },
                error="section_fetch_failed",
            )
        truncated = budget_truncated or has_more or bool(errors)
        return SkillRunResult(
            True,
            self.name,
            f"已读取 RouterOS 官方文档章节，共 {len(pages)} 页",
            payload={
                "action": "section",
                "prefix": prefix,
                "offset": offset,
                "fetched_at": _fetched_at(),
                "total_pages": len(locations),
                "returned_pages": len(pages),
                "pages": pages,
                "errors": errors,
                "retry_locations": retry_locations,
                "truncated": truncated,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "next_locations": locations[next_offset : next_offset + 6],
            },
        )

    async def _title_map(self, *, fresh: bool) -> dict[str, str]:
        try:
            documents = await self._index(fresh=fresh)
        except Exception as exc:
            log.warning("routeros_doc could not load titles: %s", exc)
            return {}
        titles: dict[str, str] = {}
        for document in documents:
            if str(document.get("type", "")) != "0":
                continue
            try:
                location, anchor = _index_location(str(document.get("url") or ""))
            except ValueError:
                continue
            if anchor:
                continue
            title = clean_text(
                _html_fragment_to_text(str(document.get("title") or ""), max_len=200),
                max_len=200,
            )
            if title:
                titles[location] = title
        return titles

    async def _run_toc(self, arguments: dict[str, Any]) -> SkillRunResult:
        fresh = self._bool_argument(arguments, "fresh")
        include_all = self._bool_argument(arguments, "include_all") or self._bool_argument(
            arguments, "all"
        )
        filter_text = clean_text(str(arguments.get("filter") or ""), max_len=100).lower()
        limit = self._int_argument(arguments, "max_results", 20, 1, 20)
        offset = self._int_argument(arguments, "offset", 0, 0, 1000)
        sitemap_result, title_result = await asyncio.gather(
            self._sitemap(fresh=fresh),
            self._title_map(fresh=fresh),
            return_exceptions=True,
        )
        if isinstance(sitemap_result, BaseException):
            raise sitemap_result
        titles = {} if isinstance(title_result, BaseException) else title_result

        matches: list[dict[str, str]] = []
        for location in sitemap_result:
            if not include_all and not _is_doc_location(location):
                continue
            title = titles.get(location, "")
            if filter_text and filter_text not in f"{location} {title}".lower():
                continue
            matches.append(
                {
                    "location": location,
                    "title": title or location,
                    "url": _url_for_location(location),
                }
            )
        results = matches[offset : offset + limit]
        if not results:
            return SkillRunResult(
                False,
                self.name,
                "RouterOS 官方文档目录中没有匹配页面",
                payload={
                    "action": "toc",
                    "filter": filter_text,
                    "include_all": include_all,
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
            f"列出 {len(results)} 个 RouterOS 官方文档页面",
            payload={
                "action": "toc",
                "filter": filter_text,
                "include_all": include_all,
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

    @staticmethod
    def _raw_cli_path(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return "/".join(str(part or "").strip(" /") for part in value if str(part or "").strip())
        return str(value or "").strip()

    @classmethod
    def _cli_location(cls, value: Any) -> tuple[str, str]:
        raw = cls._raw_cli_path(value)
        if not raw:
            return "", ""
        if "://" in raw:
            location = _normalize_location(raw)
            if not location.startswith(_CLI_PREFIX + "/"):
                raise ValueError("invalid_cli_path")
            return location[len(_CLI_PREFIX) + 1 :], location
        raw = raw.strip("/")
        if raw == _CLI_PREFIX:
            return "", ""
        if raw.startswith(_CLI_PREFIX + "/"):
            raw = raw[len(_CLI_PREFIX) + 1 :]
        location = _normalize_location(f"{_CLI_PREFIX}/{raw}")
        return location[len(_CLI_PREFIX) + 1 :], location

    async def _run_cli(self, arguments: dict[str, Any]) -> SkillRunResult:
        try:
            path, location = self._cli_location(arguments.get("path"))
        except ValueError as exc:
            return SkillRunResult(False, self.name, "RouterOS CLI 路径无效", error=str(exc))
        fresh = self._bool_argument(arguments, "fresh")
        locations = [
            item
            for item in await self._sitemap(fresh=fresh)
            if item.startswith(_CLI_PREFIX + "/")
        ]
        if not path:
            filter_text = clean_text(str(arguments.get("filter") or ""), max_len=100).lower()
            if filter_text:
                locations = [item for item in locations if filter_text in item.lower()]
            offset = self._int_argument(arguments, "offset", 0, 0, 1000)
            limit = self._int_argument(arguments, "max_results", 20, 1, 20)
            selected = locations[offset : offset + limit]
            if not selected:
                return SkillRunResult(
                    False,
                    self.name,
                    "RouterOS CLI 参考目录中没有匹配页面",
                    payload={
                        "action": "cli",
                        "mode": "list",
                        "filter": filter_text,
                        "offset": offset,
                        "total_matches": len(locations),
                        "results": [],
                    },
                    error="no_results",
                )
            results = [
                {
                    "title": "/" + item[len(_CLI_PREFIX) + 1 :],
                    "path": "/" + item[len(_CLI_PREFIX) + 1 :],
                    "location": item,
                    "url": _url_for_location(item),
                }
                for item in selected
            ]
            next_offset = offset + len(results)
            has_more = next_offset < len(locations)
            return SkillRunResult(
                True,
                self.name,
                f"列出 {len(results)} 个 RouterOS 官方文档页面",
                payload={
                    "action": "cli",
                    "mode": "list",
                    "filter": filter_text,
                    "offset": offset,
                    "source_url": _SITEMAP_URL,
                    "fetched_at": _cache_fetched_at("sitemap") or _fetched_at(),
                    "total_matches": len(locations),
                    "results": results,
                    "truncated": has_more,
                    "has_more": has_more,
                    "next_offset": next_offset if has_more else None,
                },
            )

        if location not in set(locations):
            leaf = path.rsplit("/", 1)[-1].lower()
            close = [
                item
                for item in locations
                if leaf and leaf in item[len(_CLI_PREFIX) + 1 :].lower()
            ][:15]
            return SkillRunResult(
                False,
                self.name,
                "未找到精确的 RouterOS CLI 参考页面",
                payload={
                    "action": "cli",
                    "mode": "page",
                    "path": path,
                    "location": location,
                    "close_matches": [
                        {
                            "title": "/" + item[len(_CLI_PREFIX) + 1 :],
                            "path": "/" + item[len(_CLI_PREFIX) + 1 :],
                            "location": item,
                            "url": _url_for_location(item),
                        }
                        for item in close
                    ],
                },
                error="cli_not_found",
            )

        max_chars = self._int_argument(arguments, "max_chars", 16000, 1000, 20000)
        markdown, final_url, content_type = await self._fetch_page(location)
        content, truncated = _bounded_document_text(markdown, max_chars=max_chars)
        if not content:
            return SkillRunResult(
                False,
                self.name,
                "RouterOS CLI 参考页面为空或无法解析",
                error="empty_content",
            )
        title = _title_from_markdown(content, "/" + path)
        return SkillRunResult(
            True,
            self.name,
            f"已读取 RouterOS 官方文档：CLI {title}",
            payload={
                "action": "cli",
                "mode": "page",
                "path": path,
                "location": location,
                "title": title,
                "source_url": final_url,
                "fetched_at": _fetched_at(),
                "content_type": content_type,
                "content": content,
                "truncated": truncated,
            },
        )

    async def _run_changelog(self, arguments: dict[str, Any]) -> SkillRunResult:
        fresh = self._bool_argument(arguments, "fresh")
        if "limit" in arguments:
            limit = self._int_argument(arguments, "limit", 15, 1, 20)
        else:
            limit = self._int_argument(arguments, "max_results", 15, 1, 20)
        offset = self._int_argument(arguments, "offset", 0, 0, 1000)
        entries: list[tuple[str, str]] = []
        for location in await self._sitemap(fresh=fresh):
            match = _CHANGELOG_RE.fullmatch(location)
            if match:
                entries.append((location, match.group(1)))
        entries.sort(key=lambda item: item[1], reverse=True)
        selected = entries[offset : offset + limit]
        if not selected:
            return SkillRunResult(
                False,
                self.name,
                "RouterOS 官方更新记录中没有匹配条目",
                payload={
                    "action": "changelog",
                    "offset": offset,
                    "total_matches": len(entries),
                    "results": [],
                },
                error="no_results",
            )
        results = [
            {
                "date": date,
                "title": f"RouterOS changelog {date}",
                "location": location,
                "url": _url_for_location(location),
            }
            for location, date in selected
        ]
        next_offset = offset + len(results)
        has_more = next_offset < len(entries)
        return SkillRunResult(
            True,
            self.name,
            f"列出 {len(results)} 条 RouterOS 更新记录",
            payload={
                "action": "changelog",
                "offset": offset,
                "source_url": _SITEMAP_URL,
                "fetched_at": _cache_fetched_at("sitemap") or _fetched_at(),
                "total_matches": len(entries),
                "results": results,
                "truncated": has_more,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "next_step": "选择 location 后用 action=page 读取具体更新内容。",
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
            if action == "cli":
                return await self._run_cli(arguments)
            if action == "changelog":
                return await self._run_changelog(arguments)
            return SkillRunResult(False, self.name, "未知的 RouterOS 文档操作", error="invalid_action")
        except _RouterOSHttpError as exc:
            return SkillRunResult(
                False,
                self.name,
                f"RouterOS 官方文档请求失败：HTTP {exc.status}",
                error=f"http_{exc.status}",
            )
        except UnsafeUrlError as exc:
            log.warning("routeros_doc rejected unsafe URL: %s", exc)
            return SkillRunResult(False, self.name, "RouterOS 文档地址安全校验失败", error=str(exc))
        except ResponseTooLargeError as exc:
            return SkillRunResult(False, self.name, "RouterOS 官方文档响应过大", error=str(exc))
        except UnsupportedContentTypeError as exc:
            return SkillRunResult(
                False,
                self.name,
                "RouterOS 官方文档返回了不支持的内容类型",
                error=str(exc),
            )
        except TimeoutError:
            return SkillRunResult(False, self.name, "RouterOS 官方文档请求超时", error="timeout")
        except _RouterOSDocError as exc:
            log.warning("routeros_doc invalid upstream data: %s", exc)
            return SkillRunResult(False, self.name, "RouterOS 官方文档数据格式异常", error=str(exc))
        except Exception as exc:
            log.exception("routeros_doc failed | action=%s", action)
            return SkillRunResult(False, self.name, "RouterOS 官方文档查询失败", error=str(exc))
