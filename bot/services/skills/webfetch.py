from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse

import aiohttp

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_text

log = logging.getLogger(__name__)

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\\1>")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


class WebFetchSkill:
    name = "webfetch"
    description = "抓取并提取指定 URL 的网页正文内容。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的网页 URL（http/https）"},
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    @staticmethod
    def _valid_url(url: str) -> bool:
        try:
            p = urlparse(url)
            return p.scheme in {"http", "https"} and bool(p.netloc)
        except Exception:
            return False

    @staticmethod
    def _html_to_text(raw_html: str) -> tuple[str, str]:
        m = _TITLE_RE.search(raw_html)
        title = clean_text(html.unescape(m.group(1) if m else ""), max_len=200)

        body = _SCRIPT_STYLE_RE.sub(" ", raw_html)
        body = _TAG_RE.sub(" ", body)
        body = html.unescape(body)
        body = clean_text(body, max_len=6000)
        return title, body

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        _ = context  # Unused for this skill.
        u = str(arguments.get("url", "")).strip()
        if not self._valid_url(u):
            return SkillRunResult(ok=False, skill=self.name, summary="URL 非法", error="invalid_url")

        headers = {
            "User-Agent": "SmartGroupBot/1.0 (+https://example.local)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
                async with sess.get(u, allow_redirects=True) as resp:
                    status = resp.status
                    final_url = str(resp.url)
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if status >= 400:
                        return SkillRunResult(
                            ok=False,
                            skill=self.name,
                            summary=f"网页请求失败: HTTP {status}",
                            error=f"http_{status}",
                        )

                    raw = await resp.text(errors="ignore")
                    title, content = self._html_to_text(raw)
                    if not content:
                        return SkillRunResult(
                            ok=False,
                            skill=self.name,
                            summary="网页内容为空或不可解析",
                            error="empty_content",
                        )

                    return SkillRunResult(
                        ok=True,
                        skill=self.name,
                        summary="网页抓取成功",
                        payload={
                            "url": u,
                            "final_url": final_url,
                            "status": status,
                            "content_type": ctype,
                            "title": title,
                            "content": content,
                        },
                    )
        except Exception as e:
            log.exception("webfetch failed")
            return SkillRunResult(ok=False, skill=self.name, summary="网页抓取失败", error=str(e))
