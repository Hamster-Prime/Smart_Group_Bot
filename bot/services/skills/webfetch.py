from __future__ import annotations

import logging
from urllib.parse import urlparse

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import fetch_text, parse_html_summary
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)


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
        summary = parse_html_summary(raw_html, max_content_len=6000)
        title = clean_text(summary.get("title") or "", max_len=200)
        content = clean_multiline_text(
            str(summary.get("description") or summary.get("content") or ""),
            max_len=6000,
        )
        return title, content

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        _ = context  # Unused for this skill.
        u = str(arguments.get("url", "")).strip()
        if not self._valid_url(u):
            return SkillRunResult(ok=False, skill=self.name, summary="URL 非法", error="invalid_url")

        headers = {
            "User-Agent": "SmartGroupBot/1.0 (+https://example.local)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        }

        try:
            status, raw, final_url, ctype = await fetch_text(
                u,
                headers=headers,
                timeout_sec=15.0,
                allow_redirects=True,
            )
            if status >= 400:
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary=f"网页请求失败: HTTP {status}",
                    error=f"http_{status}",
                )

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
