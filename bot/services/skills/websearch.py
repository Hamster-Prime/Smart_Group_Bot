from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_text

log = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_DATE_RE = re.compile(
    r"(?:\d{4}[年/\-.]\d{1,2}[月/\-.]\d{1,2}日?|\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月|\b\d{4}-\d{1,2}-\d{1,2}\b)"
)
_LEADING_NOISE_RE = re.compile(
    r"^(?:请问?|麻烦|帮我|帮忙|帮|给我|查询|查一下|查下|查一查|查|搜索|搜一下|搜|看看|看下|想知道|请你)+"
)
_WEATHER_RE = re.compile(r"(?:天气|气温|温度|降雨|下雨|晴|阴|风|湿度|空气)")
_NEWS_RE = re.compile(r"(?:新闻|资讯|头条|热点|快讯|要闻|日报|晚报)")
_WEATHER_CITY_RE = re.compile(
    r"([\u4e00-\u9fff]{2,8})(?:市|县|区|州|盟|旗|省)?(?=\s*(?:天气|气温|温度|降雨|下雨|晴|阴|风|空气|湿度))"
)
_NEWS_NOISE_RE = re.compile(
    r"(?:今天|今日|昨天|明天|最新|新闻|资讯|头条|热点|快讯|汇总|合集|盘点|查询|搜索|搜一下|查一下|查|搜|帮我|请|一下|给我|看看|看下|关于)"
)


class WebSearchSkill:
    name = "websearch"
    description = "联网搜索公开网页信息，返回标题、链接和摘要。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "返回数量(1-10)", "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return bool(_CJK_RE.search(text or ""))

    @staticmethod
    def _is_weather_query(text: str) -> bool:
        return bool(_WEATHER_RE.search(text or ""))

    @staticmethod
    def _is_news_query(text: str) -> bool:
        return bool(_NEWS_RE.search(text or ""))

    @staticmethod
    def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            s = (item or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    @staticmethod
    def _strip_date_tokens(text: str) -> str:
        return _DATE_RE.sub(" ", text or "")

    def _normalize_query(self, query: str) -> str:
        q = clean_text(query, max_len=300).replace("\u3000", " ").strip()
        q = self._strip_date_tokens(q)
        q = _LEADING_NOISE_RE.sub("", q)
        q = re.sub(r"\s+", " ", q).strip(" \t\r\n,，。！？?!；;：:")
        return q

    def _extract_weather_city(self, query: str) -> str:
        q = self._normalize_query(query)
        m = _WEATHER_CITY_RE.search(q)
        if m:
            city = m.group(1).strip()
            city = re.sub(r"^(?:今天|今日|明天|昨天|后天|现在)", "", city)
            if 1 < len(city) <= 8:
                return city

        for token in re.split(r"\s+", q):
            t = token.strip()
            if not t:
                continue
            t = re.sub(r"(?:天气|气温|温度|降雨|下雨|晴|阴|风|空气|湿度)$", "", t)
            t = re.sub(r"^(?:今天|今日|明天|昨天|后天|现在)", "", t)
            t = t.strip()
            if 1 < len(t) <= 8 and self._contains_cjk(t):
                return t
        return ""

    def _extract_news_topic(self, query: str) -> str:
        q = self._normalize_query(query)
        q = _NEWS_NOISE_RE.sub(" ", q)
        q = re.sub(r"\s+", " ", q).strip(" \t\r\n,，。！？?!；;：:")
        return q

    def _build_query_candidates(self, query: str) -> list[str]:
        original = clean_text(query, max_len=300).strip()
        normalized = self._normalize_query(original)
        candidates: list[str] = [original]

        if normalized and normalized != original:
            candidates.append(normalized)

        if self._is_weather_query(original):
            city = self._extract_weather_city(original) or self._extract_weather_city(normalized)
            if city:
                candidates.extend([f"{city} 天气", f"{city} 天气 预报", f"{city} 实时天气"])
            elif normalized and "天气" not in normalized:
                candidates.append(f"{normalized} 天气")

        if self._is_news_query(original):
            topic = self._extract_news_topic(original)
            if topic:
                candidates.extend([f"{topic} 最新新闻", f"{topic} 新闻"])
            candidates.append("今日 新闻 热点")

        return self._dedupe_keep_order(candidates)[:5]

    @staticmethod
    def _build_attempts(
        query: str,
        *,
        candidates: list[str],
        prefer_cn: bool,
        is_news: bool,
    ) -> list[tuple[str, str, str, str]]:
        primary_region = "cn-zh" if prefer_cn else "us-en"
        secondary_region = "wt-wt"

        attempts: list[tuple[str, str, str, str]] = []
        text_backend_main = "duckduckgo,google,yahoo,yandex,brave,mojeek"
        news_backend_main = "duckduckgo,bing,yahoo"

        for c in candidates[:3]:
            attempts.append(("text", c, primary_region, text_backend_main))

        if candidates:
            attempts.append(("text", candidates[0], secondary_region, text_backend_main))

        if is_news and candidates:
            attempts.insert(1, ("news", candidates[0], primary_region, news_backend_main))
            attempts.insert(2, ("news", candidates[0], secondary_region, news_backend_main))
            attempts.append(("news", candidates[0], primary_region, "auto"))

        attempts.append(("text", query, primary_region, "auto"))
        attempts.append(("text", query, secondary_region, "auto"))

        deduped: list[tuple[str, str, str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for attempt in attempts:
            if attempt in seen:
                continue
            seen.add(attempt)
            deduped.append(attempt)
        return deduped[:8]

    @staticmethod
    def _search_once(
        *,
        kind: str,
        query: str,
        max_results: int,
        region: str,
        backend: str,
    ) -> list[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            if kind == "news":
                return list(
                    ddgs.news(
                        query,
                        max_results=max_results,
                        region=region,
                        safesearch="off",
                        backend=backend,
                    )
                )
            return list(
                ddgs.text(
                    query,
                    max_results=max_results,
                    region=region,
                    safesearch="off",
                    backend=backend,
                )
            )

    @staticmethod
    def _parse_rows(rows: list[dict], *, max_results: int) -> list[dict]:
        results: list[dict] = []
        seen_urls: set[str] = set()

        for row in rows:
            if len(results) >= max_results:
                break
            title = clean_text(str(row.get("title") or row.get("headline") or ""), max_len=200)
            href = str(row.get("href") or row.get("url") or row.get("link") or "").strip()
            body = clean_text(
                str(
                    row.get("body")
                    or row.get("snippet")
                    or row.get("description")
                    or row.get("excerpt")
                    or row.get("content")
                    or row.get("abstract")
                    or ""
                ),
                max_len=300,
            )
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            results.append({"title": title or href, "url": href, "snippet": body})

        return results

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        _ = context  # Unused for this skill.
        q = clean_text(str(arguments.get("query", "")), max_len=300)
        try:
            max_results = int(arguments.get("max_results", 5))
        except Exception:
            max_results = 5
        max_results = max(1, min(max_results, 10))

        if not q:
            return SkillRunResult(ok=False, skill=self.name, summary="搜索词为空", error="empty_query")

        candidates = self._build_query_candidates(q)
        attempts = self._build_attempts(
            q,
            candidates=candidates,
            prefer_cn=self._contains_cjk(q),
            is_news=self._is_news_query(q),
        )
        last_error = "empty_result"

        for idx, (kind, query, region, backend) in enumerate(attempts, start=1):
            log.info(
                "websearch try=%d/%d kind=%s region=%s backend=%s query=%s",
                idx,
                len(attempts),
                kind,
                region,
                backend,
                query,
            )
            try:
                rows = await asyncio.to_thread(
                    self._search_once,
                    kind=kind,
                    query=query,
                    max_results=max_results,
                    region=region,
                    backend=backend,
                )
            except ModuleNotFoundError:
                log.exception("websearch failed: ddgs not installed")
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="未安装 ddgs 依赖",
                    error="ddgs_not_installed",
                )
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                log.warning(
                    "websearch attempt failed: kind=%s region=%s backend=%s query=%s error=%s",
                    kind,
                    region,
                    backend,
                    query,
                    exc,
                )
                continue

            results = self._parse_rows(rows, max_results=max_results)
            if results:
                return SkillRunResult(
                    ok=True,
                    skill=self.name,
                    summary=f"找到 {len(results)} 条搜索结果",
                    payload={
                        "query": q,
                        "effective_query": query,
                        "search_kind": kind,
                        "region": region,
                        "results": results,
                    },
                )

        summary = "没有找到搜索结果"
        if last_error and last_error != "empty_result":
            summary = f"没有找到搜索结果（{clean_text(last_error, max_len=80)}）"
        return SkillRunResult(ok=False, skill=self.name, summary=summary, error=last_error)
