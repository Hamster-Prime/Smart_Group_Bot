from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable
from urllib.parse import quote

import aiohttp

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import ResponseTooLargeError, _read_limited_raw_body
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_TMDB_API_BASE = "https://api.themoviedb.org/3"
_TMDB_WEB_BASE = "https://www.themoviedb.org/movie"
_TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w500"
_TMDB_ATTRIBUTION = (
    "This product uses the TMDB API but is not endorsed or certified by TMDB."
)

_IMDB_DATA_EXCHANGE_ENDPOINT = (
    "https://api-fulfill.dataexchange.us-east-1.amazonaws.com/v1"
)
_IMDB_DATA_EXCHANGE_HOST = "api-fulfill.dataexchange.us-east-1.amazonaws.com"
_IMDB_GRAPHQL_PATH = "/v1"
_IMDB_WEB_BASE = "https://www.imdb.com/title"
_AWS_REGION = "us-east-1"
_AWS_SERVICE = "dataexchange"

_ACTIONS = frozenset({"search", "details", "trending", "now_playing", "upcoming"})
_SOURCES = frozenset({"auto", "tmdb", "imdb", "both"})
_IMDB_ID_RE = re.compile(r"^tt\d{7,12}$", re.IGNORECASE)
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
_REGION_RE = re.compile(r"^[A-Za-z]{2}$")

_TMDB_GENRES = {
    12: "Adventure",
    14: "Fantasy",
    16: "Animation",
    18: "Drama",
    27: "Horror",
    28: "Action",
    35: "Comedy",
    36: "History",
    37: "Western",
    53: "Thriller",
    80: "Crime",
    99: "Documentary",
    878: "Science Fiction",
    9648: "Mystery",
    10402: "Music",
    10749: "Romance",
    10751: "Family",
    10752: "War",
    10770: "TV Movie",
}
_TMDB_RELEASE_TYPE_PRIORITY = {
    2: 0,  # Theatrical (limited)
    3: 0,  # Theatrical
    1: 1,  # Premiere
    4: 2,  # Digital
    5: 3,  # Physical
    6: 4,  # TV
}

_IMDB_MOVIE_FRAGMENT = """
fragment MovieInfoFields on Title {
  id
  titleText { text }
  originalTitleText { text }
  releaseDate { day month year }
  releaseYear { year }
  titleType { id text }
  primaryImage { url }
  plots(first: 1) { edges { node { plotText { plainText } } } }
  titleGenres { genres { genre { text } } }
  runtime { seconds }
  ratingsSummary { aggregateRating voteCount }
}
""".strip()

_IMDB_SEARCH_QUERY = (
    """
query SearchMovies($searchTerm: String!, $first: Int!) {
  mainSearch(
    options: {
      searchTerm: $searchTerm
      type: TITLE
      includeAdult: false
      titleSearchOptions: { type: MOVIE }
    }
    first: $first
  ) {
    edges { node { entity { ...MovieInfoFields } } }
  }
}
""".strip()
    + "\n"
    + _IMDB_MOVIE_FRAGMENT
)

_IMDB_DETAILS_QUERY = (
    """
query MovieDetails($id: ID!) {
  title(id: $id) { ...MovieInfoFields }
}
""".strip()
    + "\n"
    + _IMDB_MOVIE_FRAGMENT
)


class _ProviderError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = clean_text(code, max_len=80) or "provider_error"
        self.detail = clean_text(detail, max_len=180)

    @property
    def public_error(self) -> str:
        if self.detail and self.detail != self.code:
            return f"{self.code}: {self.detail}"
        return self.code


@dataclass(slots=True)
class _ProviderResponse:
    rows: list[dict[str, Any]]
    disclaimer: str = ""


class MovieInfoSkill:
    name = "movie_info"
    description = (
        "查询实时电影信息的权威工具，仅用于电影，不用于电视剧。支持按片名搜索、按 TMDB/IMDb ID "
        "查询详情，以及热门、正在上映和即将上映榜单。结果会分别保留 TMDB 与 IMDb 的评分和票数；"
        "用户询问电影实时资料、上映状态、评分或榜单时，应优先使用本技能而不是普通网页搜索。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "details", "trending", "now_playing", "upcoming"],
                "description": "search=片名搜索；details=详情；其余为电影榜单。",
            },
            "source": {
                "type": "string",
                "enum": ["auto", "tmdb", "imdb", "both"],
                "default": "auto",
                "description": "auto 会使用已配置且支持该操作的来源；both 明确请求两端聚合。",
            },
            "query": {
                "type": "string",
                "description": "action=search 时必填的电影名或关键词。",
            },
            "tmdb_id": {
                "type": "integer",
                "minimum": 1,
                "description": "action=details 时可传的 TMDB movie id。",
            },
            "imdb_id": {
                "type": "string",
                "pattern": "^tt[0-9]{7,12}$",
                "description": "action=details 时可传的 IMDb title id。",
            },
            "year": {
                "type": "integer",
                "minimum": 1870,
                "maximum": 2100,
                "description": "搜索时可选的首映年份。",
            },
            "language": {
                "type": "string",
                "description": "TMDB 返回语言，例如 zh-CN；默认取运行时配置。",
            },
            "region": {
                "type": "string",
                "description": "两字母地区代码，例如 CN、US；默认取运行时配置。",
            },
            "time_window": {
                "type": "string",
                "enum": ["day", "week"],
                "default": "day",
                "description": "action=trending 时的时间窗口。",
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 1,
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "最多返回多少部电影。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Any | None = None) -> None:
        self.enabled = bool(getattr(settings, "movie_info_enabled", False))
        self.http_timeout_sec = self._bounded_float(
            getattr(settings, "movie_info_http_timeout_sec", 6.0),
            default=6.0,
            minimum=1.0,
            maximum=6.0,
        )
        self.max_results = self._bounded_int(
            getattr(settings, "movie_info_max_results", 6),
            default=6,
            minimum=1,
            maximum=20,
        )
        self.default_language = self._normalize_language(
            getattr(settings, "movie_info_default_language", "zh-CN"),
            fallback="zh-CN",
        )
        self.default_region = self._normalize_region(
            getattr(settings, "movie_info_default_region", "CN"),
            fallback="CN",
        )
        self.tmdb_read_access_token = clean_text(
            str(getattr(settings, "movie_info_tmdb_read_access_token", "") or ""),
            max_len=2048,
        )
        self.imdb_data_set_id = clean_text(
            str(getattr(settings, "movie_info_imdb_data_set_id", "") or ""),
            max_len=255,
        )
        self.imdb_revision_id = clean_text(
            str(getattr(settings, "movie_info_imdb_revision_id", "") or ""),
            max_len=255,
        )
        self.imdb_asset_id = clean_text(
            str(getattr(settings, "movie_info_imdb_asset_id", "") or ""),
            max_len=255,
        )
        self.imdb_api_key = clean_text(
            str(getattr(settings, "movie_info_imdb_api_key", "") or ""),
            max_len=2048,
        )
        self.imdb_aws_access_key_id = clean_text(
            str(getattr(settings, "movie_info_imdb_aws_access_key_id", "") or ""),
            max_len=255,
        )
        self.imdb_aws_secret_access_key = clean_text(
            str(getattr(settings, "movie_info_imdb_aws_secret_access_key", "") or ""),
            max_len=2048,
        )
        self.imdb_aws_session_token = clean_text(
            str(getattr(settings, "movie_info_imdb_aws_session_token", "") or ""),
            max_len=4096,
        )

    @property
    def tmdb_available(self) -> bool:
        return bool(self.tmdb_read_access_token)

    @property
    def imdb_available(self) -> bool:
        return all(
            (
                self.imdb_data_set_id,
                self.imdb_revision_id,
                self.imdb_asset_id,
                self.imdb_api_key,
                self.imdb_aws_access_key_id,
                self.imdb_aws_secret_access_key,
            )
        )

    @property
    def available(self) -> bool:
        return self.enabled and (self.tmdb_available or self.imdb_available)

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _normalize_language(value: Any, *, fallback: str) -> str:
        text = clean_text(str(value or ""), max_len=16)
        if not _LANGUAGE_RE.fullmatch(text):
            return fallback
        parts = text.split("-", 1)
        return parts[0].lower() + (f"-{parts[1].upper()}" if len(parts) == 2 else "")

    @staticmethod
    def _normalize_region(value: Any, *, fallback: str) -> str:
        text = clean_text(str(value or ""), max_len=8)
        return text.upper() if _REGION_RE.fullmatch(text) else fallback

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _base_payload(
        self,
        action: str,
        *,
        query: str = "",
        provider_errors: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": action,
            "providers_used": [],
            "provider_errors": dict(provider_errors or {}),
            "fetched_at": self._utc_now(),
        }
        if query:
            payload["query"] = query
        if action == "details":
            payload["entry"] = None
        else:
            payload["results"] = []
        return payload

    def _requested_providers(
        self,
        *,
        action: str,
        source: str,
    ) -> tuple[list[str], dict[str, str]]:
        if source == "tmdb":
            requested = ["tmdb"]
        elif source == "imdb":
            requested = ["imdb"]
        elif source == "both":
            requested = ["tmdb", "imdb"]
        elif action in {"trending", "now_playing", "upcoming"} and self.tmdb_available:
            requested = ["tmdb"]
        else:
            requested = [
                provider
                for provider, available in (
                    ("tmdb", self.tmdb_available),
                    ("imdb", self.imdb_available),
                )
                if available
            ]

        errors: dict[str, str] = {}
        usable: list[str] = []
        for provider in requested:
            if provider == "tmdb" and not self.tmdb_available:
                errors[provider] = "not_configured"
            elif provider == "imdb" and not self.imdb_available:
                errors[provider] = "not_configured"
            elif provider == "imdb" and action in {"trending", "now_playing", "upcoming"}:
                errors[provider] = "unsupported_action"
            else:
                usable.append(provider)
        return usable, errors

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context
        action = clean_text(str(arguments.get("action", "")), max_len=32).lower()
        source = clean_text(str(arguments.get("source", "auto")), max_len=16).lower() or "auto"
        if action not in _ACTIONS:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="未知的电影查询操作",
                payload=self._base_payload(action or "unknown"),
                error="unknown_action",
            )
        if source not in _SOURCES:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="未知的电影数据来源",
                payload=self._base_payload(action),
                error="invalid_source",
            )
        if not self.enabled:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="电影信息查询技能当前已关闭",
                payload=self._base_payload(action),
                error="disabled",
            )

        providers, provider_errors = self._requested_providers(action=action, source=source)
        if not providers:
            payload = self._base_payload(action, provider_errors=provider_errors)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="电影信息查询服务尚未配置可用的数据源",
                payload=payload,
                error=(
                    "unsupported_action"
                    if provider_errors and set(provider_errors.values()) == {"unsupported_action"}
                    else "not_configured"
                ),
            )

        max_results = self._bounded_int(
            arguments.get("max_results", self.max_results),
            default=self.max_results,
            minimum=1,
            maximum=self.max_results,
        )
        language = self._normalize_language(
            arguments.get("language", self.default_language),
            fallback=self.default_language,
        )
        region = self._normalize_region(
            arguments.get("region", self.default_region),
            fallback=self.default_region,
        )
        page = self._bounded_int(arguments.get("page", 1), default=1, minimum=1, maximum=500)

        if action == "search":
            query = clean_text(str(arguments.get("query", "")), max_len=180)
            if not query:
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="电影搜索词为空",
                    payload=self._base_payload(action, provider_errors=provider_errors),
                    error="empty_query",
                )
            year = self._bounded_int(
                arguments.get("year", 0), default=0, minimum=0, maximum=2100
            )
            if year and year < 1870:
                year = 0
            return await self._run_search(
                query=query,
                year=year,
                providers=providers,
                provider_errors=provider_errors,
                language=language,
                region=region,
                page=page,
                max_results=max_results,
            )

        if action == "details":
            tmdb_id = self._bounded_int(
                arguments.get("tmdb_id", 0), default=0, minimum=0, maximum=2_147_483_647
            )
            raw_imdb_id = clean_text(str(arguments.get("imdb_id", "")), max_len=24).lower()
            imdb_id = raw_imdb_id if _IMDB_ID_RE.fullmatch(raw_imdb_id) else ""
            if raw_imdb_id and not imdb_id:
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="IMDb ID 格式无效，应为 tt 加数字",
                    payload=self._base_payload(action, provider_errors=provider_errors),
                    error="invalid_imdb_id",
                )
            if not tmdb_id and not imdb_id:
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="查询电影详情需要 tmdb_id 或 imdb_id",
                    payload=self._base_payload(action, provider_errors=provider_errors),
                    error="missing_movie_id",
                )
            return await self._run_details(
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                providers=providers,
                provider_errors=provider_errors,
                language=language,
                region=region,
            )

        time_window = clean_text(str(arguments.get("time_window", "day")), max_len=8).lower()
        if time_window not in {"day", "week"}:
            time_window = "day"
        return await self._run_listing(
            action=action,
            providers=providers,
            provider_errors=provider_errors,
            language=language,
            region=region,
            page=page,
            max_results=max_results,
            time_window=time_window,
        )

    async def _run_provider_calls(
        self,
        calls: dict[str, Awaitable[_ProviderResponse]],
    ) -> tuple[dict[str, _ProviderResponse], dict[str, str]]:
        async def invoke(
            provider: str,
            awaitable: Awaitable[_ProviderResponse],
        ) -> tuple[str, _ProviderResponse | None, str]:
            try:
                return provider, await awaitable, ""
            except asyncio.CancelledError:
                raise
            except _ProviderError as exc:
                log.warning("movie provider failed | provider=%s error=%s", provider, exc.code)
                return provider, None, exc.public_error
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
                log.warning(
                    "movie provider network failure | provider=%s error=%s",
                    provider,
                    exc.__class__.__name__,
                )
                return provider, None, "network_error"
            except Exception as exc:
                log.warning(
                    "movie provider unexpected failure | provider=%s error=%s",
                    provider,
                    exc.__class__.__name__,
                )
                return provider, None, "provider_error"

        completed = await asyncio.gather(
            *(invoke(provider, awaitable) for provider, awaitable in calls.items())
        )
        responses: dict[str, _ProviderResponse] = {}
        errors: dict[str, str] = {}
        for provider, response, error in completed:
            if response is not None:
                responses[provider] = response
            elif error:
                errors[provider] = error
        return responses, errors

    async def _run_search(
        self,
        *,
        query: str,
        year: int,
        providers: list[str],
        provider_errors: dict[str, str],
        language: str,
        region: str,
        page: int,
        max_results: int,
    ) -> SkillRunResult:
        calls: dict[str, Awaitable[_ProviderResponse]] = {}
        if "tmdb" in providers:
            calls["tmdb"] = self._tmdb_search(
                query=query,
                year=year,
                language=language,
                region=region,
                page=page,
                max_results=max_results,
            )
        if "imdb" in providers:
            calls["imdb"] = self._imdb_search(
                query=query,
                year=year,
                page=page,
                max_results=max_results,
            )

        responses, call_errors = await self._run_provider_calls(calls)
        provider_errors.update(call_errors)
        rows = self._merge_rows(self._interleaved_rows(responses), limit=max_results)
        payload = self._result_payload(
            action="search",
            query=query,
            responses=responses,
            provider_errors=provider_errors,
            rows=rows,
        )
        ok = bool(rows)
        return SkillRunResult(
            ok=ok,
            skill=self.name,
            summary=(f"找到 {len(rows)} 部相关电影" if rows else "没有找到相关电影"),
            payload=payload,
            error="" if ok else ("no_results" if responses else "provider_failed"),
        )

    async def _run_listing(
        self,
        *,
        action: str,
        providers: list[str],
        provider_errors: dict[str, str],
        language: str,
        region: str,
        page: int,
        max_results: int,
        time_window: str,
    ) -> SkillRunResult:
        calls: dict[str, Awaitable[_ProviderResponse]] = {}
        if "tmdb" in providers:
            calls["tmdb"] = self._tmdb_listing(
                action=action,
                language=language,
                region=region,
                page=page,
                max_results=max_results,
                time_window=time_window,
            )
        if "imdb" in providers:
            provider_errors["imdb"] = "unsupported_action"

        responses, call_errors = await self._run_provider_calls(calls)
        provider_errors.update(call_errors)
        rows = self._merge_rows(self._interleaved_rows(responses), limit=max_results)
        payload = self._result_payload(
            action=action,
            responses=responses,
            provider_errors=provider_errors,
            rows=rows,
        )
        label = {
            "trending": "热门",
            "now_playing": "正在上映",
            "upcoming": "即将上映",
        }[action]
        ok = bool(rows)
        return SkillRunResult(
            ok=ok,
            skill=self.name,
            summary=f"查到 {len(rows)} 部{label}电影" if ok else f"{label}电影查询失败",
            payload=payload,
            error="" if ok else ("no_results" if responses else "provider_failed"),
        )

    async def _run_details(
        self,
        *,
        tmdb_id: int,
        imdb_id: str,
        providers: list[str],
        provider_errors: dict[str, str],
        language: str,
        region: str,
    ) -> SkillRunResult:
        responses: dict[str, _ProviderResponse] = {}

        # When both stable identifiers are already known, the two providers are independent.
        parallel_calls: dict[str, Awaitable[_ProviderResponse]] = {}
        if "tmdb" in providers and tmdb_id:
            parallel_calls["tmdb"] = self._tmdb_details(
                tmdb_id=tmdb_id,
                language=language,
                region=region,
            )
        elif "tmdb" in providers and imdb_id:
            parallel_calls["tmdb"] = self._tmdb_details_by_imdb_id(
                imdb_id=imdb_id,
                language=language,
                region=region,
            )
        if "imdb" in providers and imdb_id:
            parallel_calls["imdb"] = self._imdb_details(imdb_id=imdb_id)

        first_responses, first_errors = await self._run_provider_calls(parallel_calls)
        responses.update(first_responses)
        provider_errors.update(first_errors)

        if tmdb_id and imdb_id and "tmdb" in responses and "imdb" in responses:
            tmdb_rows = responses["tmdb"].rows
            tmdb_imdb_id = clean_text(
                str((tmdb_rows[0].get("ids") or {}).get("imdb") or "")
                if tmdb_rows
                else "",
                max_len=24,
            ).lower()
            if tmdb_imdb_id and tmdb_imdb_id != imdb_id:
                provider_errors["imdb"] = "id_mismatch"
                responses.pop("imdb", None)

        resolved_imdb_id = imdb_id
        tmdb_rows = responses.get("tmdb", _ProviderResponse([])).rows
        if not resolved_imdb_id and tmdb_rows:
            resolved_imdb_id = clean_text(
                str(tmdb_rows[0].get("ids", {}).get("imdb") or ""),
                max_len=24,
            ).lower()

        if "imdb" in providers and "imdb" not in responses and "imdb" not in provider_errors:
            if resolved_imdb_id and _IMDB_ID_RE.fullmatch(resolved_imdb_id):
                more_responses, more_errors = await self._run_provider_calls(
                    {"imdb": self._imdb_details(imdb_id=resolved_imdb_id)}
                )
                responses.update(more_responses)
                provider_errors.update(more_errors)
            else:
                provider_errors["imdb"] = "imdb_id_unavailable"

        rows = [
            row
            for provider in ("tmdb", "imdb")
            for row in responses.get(provider, _ProviderResponse([])).rows[:1]
        ]
        merged = self._merge_rows(rows, limit=1)
        entry = merged[0] if merged else None
        payload = self._result_payload(
            action="details",
            responses=responses,
            provider_errors=provider_errors,
            entry=entry,
        )
        ok = entry is not None
        title = clean_text(str((entry or {}).get("title") or ""), max_len=120)
        return SkillRunResult(
            ok=ok,
            skill=self.name,
            summary=(f"已查询《{title}》的电影信息" if title else "没有查到这部电影"),
            payload=payload,
            error="" if ok else ("no_results" if responses else "provider_failed"),
        )

    def _result_payload(
        self,
        *,
        action: str,
        responses: dict[str, _ProviderResponse],
        provider_errors: dict[str, str],
        query: str = "",
        rows: list[dict[str, Any]] | None = None,
        entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._base_payload(action, query=query, provider_errors=provider_errors)
        payload["providers_used"] = [
            provider for provider in ("tmdb", "imdb") if provider in responses
        ]
        if action == "details":
            payload["entry"] = entry
            data_rows = [entry] if entry else []
        else:
            payload["results"] = list(rows or [])
            data_rows = list(rows or [])

        if any(row and row.get("source") in {"tmdb", "both"} for row in data_rows):
            payload["attribution"] = _TMDB_ATTRIBUTION
        imdb_response = responses.get("imdb")
        if imdb_response and imdb_response.disclaimer:
            payload["imdb_disclaimer"] = clean_text(
                imdb_response.disclaimer,
                max_len=500,
            )
        return payload

    async def _tmdb_get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        if not path.startswith("/") or ".." in path:
            raise _ProviderError("invalid_path")
        status, data = await self._request_json(
            method="GET",
            url=f"{_TMDB_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {self.tmdb_read_access_token}",
                "Accept": "application/json",
            },
            params=params,
        )
        if status != 200:
            detail = ""
            if isinstance(data, dict):
                detail = clean_text(str(data.get("status_message") or ""), max_len=120)
            raise _ProviderError(f"http_{status}", detail)
        if not isinstance(data, dict):
            raise _ProviderError("invalid_response")
        return data

    async def _tmdb_search(
        self,
        *,
        query: str,
        year: int,
        language: str,
        region: str,
        page: int,
        max_results: int,
    ) -> _ProviderResponse:
        params: dict[str, Any] = {
            "query": query,
            "include_adult": "false",
            "language": language,
            "region": region,
            "page": page,
        }
        if year:
            params["year"] = year
        data = await self._tmdb_get("/search/movie", params=params)
        rows = data.get("results") if isinstance(data.get("results"), list) else []
        return _ProviderResponse(
            [self._normalize_tmdb_movie(row) for row in rows[:max_results] if isinstance(row, dict)]
        )

    async def _tmdb_listing(
        self,
        *,
        action: str,
        language: str,
        region: str,
        page: int,
        max_results: int,
        time_window: str,
    ) -> _ProviderResponse:
        if action == "trending":
            path = f"/trending/movie/{time_window}"
            params: dict[str, Any] = {"language": language, "page": page}
        else:
            path = f"/movie/{action}"
            params = {"language": language, "region": region, "page": page}
        data = await self._tmdb_get(path, params=params)
        rows = data.get("results") if isinstance(data.get("results"), list) else []
        return _ProviderResponse(
            [self._normalize_tmdb_movie(row) for row in rows[:max_results] if isinstance(row, dict)]
        )

    async def _tmdb_details(
        self,
        *,
        tmdb_id: int,
        language: str,
        region: str,
    ) -> _ProviderResponse:
        data = await self._tmdb_get(
            f"/movie/{tmdb_id}",
            params={
                "language": language,
                "append_to_response": "external_ids,release_dates",
            },
        )
        return _ProviderResponse([self._normalize_tmdb_movie(data, region=region)])

    async def _tmdb_details_by_imdb_id(
        self,
        *,
        imdb_id: str,
        language: str,
        region: str,
    ) -> _ProviderResponse:
        found = await self._tmdb_get(
            f"/find/{quote(imdb_id, safe='')}",
            params={"external_source": "imdb_id", "language": language},
        )
        rows = found.get("movie_results")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return _ProviderResponse([])
        tmdb_id = self._as_int(rows[0].get("id"))
        if tmdb_id <= 0:
            return _ProviderResponse([])
        return await self._tmdb_details(
            tmdb_id=tmdb_id,
            language=language,
            region=region,
        )

    async def _imdb_graphql(
        self,
        *,
        query: str,
        variables: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        body = json.dumps(
            {"query": query, "variables": variables},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = self._imdb_signed_headers(body)
        status, response = await self._request_json(
            method="POST",
            url=_IMDB_DATA_EXCHANGE_ENDPOINT,
            headers=headers,
            body=body,
        )
        if status != 200:
            raise _ProviderError(f"http_{status}")
        if not isinstance(response, dict):
            raise _ProviderError("invalid_response")
        errors = response.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            detail = clean_text(str(first.get("message") or "GraphQL error"), max_len=160)
            raise _ProviderError("graphql_error", detail)
        data = response.get("data")
        if not isinstance(data, dict):
            raise _ProviderError("invalid_response")
        extensions = response.get("extensions")
        disclaimer = ""
        if isinstance(extensions, dict):
            disclaimer = clean_text(str(extensions.get("disclaimer") or ""), max_len=500)
        return data, disclaimer

    async def _imdb_search(
        self,
        *,
        query: str,
        year: int,
        page: int,
        max_results: int,
    ) -> _ProviderResponse:
        if page > 1:
            raise _ProviderError("pagination_unsupported")
        requested = min(20, max_results * 3)
        data, disclaimer = await self._imdb_graphql(
            query=_IMDB_SEARCH_QUERY,
            variables={"searchTerm": query, "first": requested},
        )
        raw_rows = self._graphql_rows(data.get("mainSearch"))
        rows = [self._normalize_imdb_movie(row) for row in raw_rows]
        normalized = [row for row in rows if row is not None]
        if year:
            normalized = [row for row in normalized if row.get("year") == year]
        return _ProviderResponse(
            normalized[:max_results],
            disclaimer=disclaimer,
        )

    async def _imdb_details(self, *, imdb_id: str) -> _ProviderResponse:
        data, disclaimer = await self._imdb_graphql(
            query=_IMDB_DETAILS_QUERY,
            variables={"id": imdb_id},
        )
        raw = data.get("title") or data.get("titleById") or data.get("mainTitle")
        row = self._normalize_imdb_movie(raw) if isinstance(raw, dict) else None
        return _ProviderResponse([row] if row else [], disclaimer=disclaimer)

    def _imdb_signed_headers(
        self,
        body: bytes,
        *,
        now: datetime | None = None,
    ) -> dict[str, str]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        amz_date = current.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = current.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()

        headers = {
            "content-type": "application/json",
            "host": _IMDB_DATA_EXCHANGE_HOST,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "x-amzn-dataexchange-asset-id": self.imdb_asset_id,
            "x-amzn-dataexchange-data-set-id": self.imdb_data_set_id,
            "x-amzn-dataexchange-header-content-type": "application/json",
            "x-amzn-dataexchange-header-x-api-key": self.imdb_api_key,
            "x-amzn-dataexchange-http-method": "POST",
            "x-amzn-dataexchange-path": _IMDB_GRAPHQL_PATH,
            "x-amzn-dataexchange-revision-id": self.imdb_revision_id,
        }
        if self.imdb_aws_session_token:
            headers["x-amz-security-token"] = self.imdb_aws_session_token

        canonical_names = sorted(headers)
        canonical_headers = "".join(
            f"{name}:{' '.join(headers[name].strip().split())}\n" for name in canonical_names
        )
        signed_headers = ";".join(canonical_names)
        canonical_request = "\n".join(
            (
                "POST",
                "/v1",
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            )
        )
        credential_scope = f"{date_stamp}/{_AWS_REGION}/{_AWS_SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        signing_key = self._aws_signing_key(
            self.imdb_aws_secret_access_key,
            date_stamp=date_stamp,
            region=_AWS_REGION,
            service=_AWS_SERVICE,
        )
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.imdb_aws_access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    @staticmethod
    def _aws_signing_key(
        secret: str,
        *,
        date_stamp: str,
        region: str,
        service: str,
    ) -> bytes:
        date_key = hmac.new(
            f"AWS4{secret}".encode("utf-8"),
            date_stamp.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
        service_key = hmac.new(region_key, service.encode("utf-8"), hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()

    async def _request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, Any]:
        normalized_method = method.upper()
        if normalized_method == "GET":
            if not url.startswith(f"{_TMDB_API_BASE}/"):
                raise _ProviderError("untrusted_endpoint")
        elif normalized_method == "POST":
            if url != _IMDB_DATA_EXCHANGE_ENDPOINT:
                raise _ProviderError("untrusted_endpoint")
        else:
            raise _ProviderError("unsupported_http_method")

        timeout = aiohttp.ClientTimeout(
            total=self.http_timeout_sec,
            connect=min(5.0, self.http_timeout_sec),
            sock_read=min(10.0, self.http_timeout_sec),
        )
        async with aiohttp.ClientSession(
            timeout=timeout,
            auto_decompress=True,
            trust_env=False,
        ) as session:
            async with session.request(
                normalized_method,
                url,
                headers=headers,
                params=params,
                data=body,
                allow_redirects=False,
            ) as response:
                try:
                    raw = await _read_limited_raw_body(response, 2 * 1024 * 1024)
                except ResponseTooLargeError as exc:
                    raise _ProviderError("response_too_large") from exc
                if not raw:
                    return response.status, None
                try:
                    return response.status, json.loads(raw.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _ProviderError("invalid_json") from exc

    @classmethod
    def _normalize_tmdb_movie(
        cls,
        raw: dict[str, Any],
        *,
        region: str = "",
    ) -> dict[str, Any]:
        tmdb_id = cls._as_int(raw.get("id")) or None
        external_ids = raw.get("external_ids") if isinstance(raw.get("external_ids"), dict) else {}
        imdb_id = clean_text(
            str(raw.get("imdb_id") or external_ids.get("imdb_id") or ""),
            max_len=24,
        ).lower()
        if not _IMDB_ID_RE.fullmatch(imdb_id):
            imdb_id = ""
        release_date = cls._normalize_date(raw.get("release_date"))
        overview = clean_multiline_text(str(raw.get("overview") or ""), max_len=1200)
        if isinstance(raw.get("genres"), list):
            genres = [
                clean_text(str(item.get("name") or ""), max_len=60)
                for item in raw["genres"]
                if isinstance(item, dict) and item.get("name")
            ]
        else:
            genres = [
                _TMDB_GENRES[genre_id]
                for genre_id in raw.get("genre_ids", [])
                if isinstance(genre_id, int) and genre_id in _TMDB_GENRES
            ]
        tmdb_url = f"{_TMDB_WEB_BASE}/{tmdb_id}" if tmdb_id else ""
        imdb_url = f"{_IMDB_WEB_BASE}/{imdb_id}/" if imdb_id else ""
        poster_path = clean_text(str(raw.get("poster_path") or ""), max_len=255)
        tmdb_vote_count = max(0, cls._as_int(raw.get("vote_count")))
        tmdb_score = cls._as_float(raw.get("vote_average")) if tmdb_vote_count else None
        row = {
            "source": "tmdb",
            "ids": {"tmdb": tmdb_id, "imdb": imdb_id or None},
            "title": clean_text(str(raw.get("title") or ""), max_len=240),
            "original_title": clean_text(str(raw.get("original_title") or ""), max_len=240),
            "release_date": release_date,
            "year": cls._year_from_date(release_date),
            "overview": overview,
            "content": overview,
            "genres": cls._dedupe_text(genres, limit=12),
            "runtime_minutes": cls._as_int(raw.get("runtime")) or None,
            "status": clean_text(str(raw.get("status") or ""), max_len=80),
            "ratings": {
                "tmdb": {
                    "score": tmdb_score,
                    "vote_count": tmdb_vote_count,
                },
                "imdb": {"score": None, "vote_count": None},
            },
            "urls": {"tmdb": tmdb_url, "imdb": imdb_url},
            "url": tmdb_url or imdb_url,
            "poster_url": f"{_TMDB_POSTER_BASE}{poster_path}" if poster_path.startswith("/") else "",
        }
        if region:
            release_info = cls._regional_release(raw.get("release_dates"), region=region)
            if release_info:
                row["regional_release"] = release_info
        return row

    @classmethod
    def _regional_release(cls, raw: Any, *, region: str) -> dict[str, Any]:
        container = raw if isinstance(raw, dict) else {}
        entries = container.get("results")
        if not isinstance(entries, list):
            return {}
        target = next(
            (
                item
                for item in entries
                if isinstance(item, dict)
                and clean_text(str(item.get("iso_3166_1") or ""), max_len=4).upper()
                == region.upper()
            ),
            None,
        )
        dates = target.get("release_dates") if isinstance(target, dict) else None
        if not isinstance(dates, list):
            return {}
        normalized: list[dict[str, Any]] = []
        for item in dates:
            if not isinstance(item, dict):
                continue
            date = cls._normalize_date(item.get("release_date"))
            if not date:
                continue
            normalized.append(
                {
                    "date": date,
                    "certification": clean_text(str(item.get("certification") or ""), max_len=32),
                    "type": cls._as_int(item.get("type")) or None,
                    "note": clean_multiline_text(str(item.get("note") or ""), max_len=160),
                }
            )
        if not normalized:
            return {}
        normalized.sort(
            key=lambda item: (
                _TMDB_RELEASE_TYPE_PRIORITY.get(cls._as_int(item.get("type")), 5),
                item["date"],
            )
        )
        selected = normalized[0]
        today = datetime.now(timezone.utc).date().isoformat()
        selected["status"] = "released" if selected["date"] <= today else "upcoming"
        selected["region"] = region.upper()
        return selected

    @classmethod
    def _normalize_imdb_movie(cls, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        title_type = raw.get("titleType") if isinstance(raw.get("titleType"), dict) else {}
        type_id = clean_text(str(title_type.get("id") or ""), max_len=40).lower()
        if type_id and type_id not in {"movie", "tvmovie"}:
            return None

        imdb_id = clean_text(str(raw.get("id") or raw.get("imdbId") or ""), max_len=24).lower()
        if not _IMDB_ID_RE.fullmatch(imdb_id):
            return None
        title = cls._nested_text(raw.get("titleText")) or clean_text(
            str(raw.get("title") or ""), max_len=240
        )
        original_title = cls._nested_text(raw.get("originalTitleText"))
        release_date = cls._imdb_release_date(raw)
        overview = cls._imdb_overview(raw)
        genres_container = raw.get("titleGenres")
        if isinstance(genres_container, dict):
            genres_raw = genres_container.get("genres")
        else:
            genres_raw = genres_container
        genres = [
            cls._nested_text(item.get("genre") if isinstance(item, dict) else item)
            for item in (genres_raw if isinstance(genres_raw, list) else [])
        ]
        runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
        seconds = cls._as_int(runtime.get("seconds"))
        ratings = raw.get("ratingsSummary") if isinstance(raw.get("ratingsSummary"), dict) else {}
        imdb_vote_count = max(0, cls._as_int(ratings.get("voteCount")))
        imdb_score = cls._as_float(ratings.get("aggregateRating")) if imdb_vote_count else None
        image = raw.get("primaryImage") if isinstance(raw.get("primaryImage"), dict) else {}
        imdb_url = f"{_IMDB_WEB_BASE}/{imdb_id}/"
        return {
            "source": "imdb",
            "ids": {"tmdb": None, "imdb": imdb_id},
            "title": title,
            "original_title": original_title,
            "release_date": release_date,
            "year": cls._year_from_date(release_date) or cls._imdb_release_year(raw),
            "overview": overview,
            "content": overview,
            "genres": cls._dedupe_text(genres, limit=12),
            "runtime_minutes": round(seconds / 60) if seconds > 0 else None,
            "status": "",
            "ratings": {
                "tmdb": {"score": None, "vote_count": None},
                "imdb": {
                    "score": imdb_score,
                    "vote_count": imdb_vote_count,
                },
            },
            "urls": {"tmdb": "", "imdb": imdb_url},
            "url": imdb_url,
            "poster_url": clean_text(str(image.get("url") or ""), max_len=500),
        }

    @staticmethod
    def _graphql_rows(container: Any) -> list[dict[str, Any]]:
        if isinstance(container, list):
            candidates = container
        elif isinstance(container, dict):
            candidates = next(
                (
                    value
                    for key in ("edges", "results", "items", "titles")
                    if isinstance((value := container.get(key)), list)
                ),
                [],
            )
        else:
            candidates = []
        rows: list[dict[str, Any]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            node = item.get("node") if isinstance(item.get("node"), dict) else item
            entity = node.get("entity") if isinstance(node.get("entity"), dict) else node
            title = entity.get("title") if isinstance(entity.get("title"), dict) else entity
            if isinstance(title, dict):
                rows.append(title)
        return rows

    @classmethod
    def _merge_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            match_index = next(
                (
                    index
                    for index, existing in enumerate(merged)
                    if cls._same_movie(existing, row)
                ),
                -1,
            )
            if match_index < 0:
                merged.append(row)
            else:
                merged[match_index] = cls._merge_movie(merged[match_index], row)
        return merged[:limit]

    @staticmethod
    def _interleaved_rows(
        responses: dict[str, _ProviderResponse],
    ) -> list[dict[str, Any]]:
        provider_rows = [
            responses.get(provider, _ProviderResponse([])).rows
            for provider in ("tmdb", "imdb")
        ]
        rows: list[dict[str, Any]] = []
        for index in range(max((len(items) for items in provider_rows), default=0)):
            for items in provider_rows:
                if index < len(items):
                    rows.append(items[index])
        return rows

    @classmethod
    def _same_movie(cls, left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_ids = left.get("ids") if isinstance(left.get("ids"), dict) else {}
        right_ids = right.get("ids") if isinstance(right.get("ids"), dict) else {}
        for key in ("tmdb", "imdb"):
            if left_ids.get(key) and left_ids.get(key) == right_ids.get(key):
                return True
        left_titles = {
            value
            for value in (
                cls._title_key(left.get("title")),
                cls._title_key(left.get("original_title")),
            )
            if value
        }
        right_titles = {
            value
            for value in (
                cls._title_key(right.get("title")),
                cls._title_key(right.get("original_title")),
            )
            if value
        }
        if not left_titles.intersection(right_titles):
            return False
        left_year = cls._as_int(left.get("year"))
        right_year = cls._as_int(right.get("year"))
        return bool(left_year and right_year and left_year == right_year)

    @classmethod
    def _merge_movie(cls, primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
        merged = dict(primary)
        for key in (
            "title",
            "original_title",
            "release_date",
            "year",
            "overview",
            "content",
            "runtime_minutes",
            "status",
            "poster_url",
            "regional_release",
        ):
            if not merged.get(key) and secondary.get(key):
                merged[key] = secondary[key]

        primary_ids = primary.get("ids") if isinstance(primary.get("ids"), dict) else {}
        secondary_ids = secondary.get("ids") if isinstance(secondary.get("ids"), dict) else {}
        merged["ids"] = {
            provider: primary_ids.get(provider) or secondary_ids.get(provider)
            for provider in ("tmdb", "imdb")
        }
        primary_urls = primary.get("urls") if isinstance(primary.get("urls"), dict) else {}
        secondary_urls = secondary.get("urls") if isinstance(secondary.get("urls"), dict) else {}
        merged["urls"] = {
            provider: primary_urls.get(provider) or secondary_urls.get(provider) or ""
            for provider in ("tmdb", "imdb")
        }
        primary_ratings = (
            primary.get("ratings") if isinstance(primary.get("ratings"), dict) else {}
        )
        secondary_ratings = (
            secondary.get("ratings") if isinstance(secondary.get("ratings"), dict) else {}
        )
        merged["ratings"] = {}
        for provider in ("tmdb", "imdb"):
            first = (
                primary_ratings.get(provider)
                if isinstance(primary_ratings.get(provider), dict)
                else {}
            )
            second = (
                secondary_ratings.get(provider)
                if isinstance(secondary_ratings.get(provider), dict)
                else {}
            )
            merged["ratings"][provider] = {
                "score": first.get("score") if first.get("score") is not None else second.get("score"),
                "vote_count": (
                    first.get("vote_count")
                    if first.get("vote_count") is not None
                    else second.get("vote_count")
                ),
            }
        merged["genres"] = cls._dedupe_text(
            [*(primary.get("genres") or []), *(secondary.get("genres") or [])],
            limit=12,
        )
        sources = {str(primary.get("source") or ""), str(secondary.get("source") or "")}
        merged["source"] = "both" if {"tmdb", "imdb"}.issubset(sources) or "both" in sources else next(
            (source for source in ("tmdb", "imdb") if source in sources),
            "",
        )
        merged["url"] = merged["urls"].get("tmdb") or merged["urls"].get("imdb") or ""
        return merged

    @staticmethod
    def _title_key(value: Any) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())

    @staticmethod
    def _as_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if isinstance(value, bool) or value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_date(value: Any) -> str:
        text = clean_text(str(value or ""), max_len=20)
        match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:T|$)", text)
        if not match:
            return ""
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return ""

    @staticmethod
    def _year_from_date(value: Any) -> int | None:
        text = str(value or "")
        return int(text[:4]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None

    @classmethod
    def _imdb_release_date(cls, raw: dict[str, Any]) -> str:
        release = raw.get("releaseDate") if isinstance(raw.get("releaseDate"), dict) else {}
        year = cls._as_int(release.get("year"))
        month = cls._as_int(release.get("month"))
        day = cls._as_int(release.get("day"))
        if year and month and day:
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                return ""
        return ""

    @classmethod
    def _imdb_release_year(cls, raw: dict[str, Any]) -> int | None:
        release = raw.get("releaseYear") if isinstance(raw.get("releaseYear"), dict) else {}
        year = cls._as_int(release.get("year"))
        return year or None

    @classmethod
    def _imdb_overview(cls, raw: dict[str, Any]) -> str:
        plot = raw.get("plot") if isinstance(raw.get("plot"), dict) else {}
        plot_text = plot.get("plotText") if isinstance(plot.get("plotText"), dict) else {}
        value = plot_text.get("plainText") or plot_text.get("text")
        if value:
            return clean_multiline_text(str(value), max_len=1200)
        plots = raw.get("plots") if isinstance(raw.get("plots"), dict) else {}
        rows = cls._graphql_rows(plots)
        if rows:
            nested = rows[0].get("plotText") if isinstance(rows[0].get("plotText"), dict) else {}
            return clean_multiline_text(
                str(nested.get("plainText") or nested.get("text") or ""),
                max_len=1200,
            )
        return ""

    @staticmethod
    def _nested_text(value: Any) -> str:
        if isinstance(value, str):
            return clean_text(value, max_len=240)
        if not isinstance(value, dict):
            return ""
        return clean_text(
            str(value.get("text") or value.get("plainText") or value.get("name") or ""),
            max_len=240,
        )

    @staticmethod
    def _dedupe_text(values: list[Any], *, limit: int) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = clean_text(str(value or ""), max_len=80)
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            output.append(text)
            if len(output) >= limit:
                break
        return output
