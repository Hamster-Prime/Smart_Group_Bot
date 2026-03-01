from __future__ import annotations

import logging

from bot.services.knowledge import KnowledgeService
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_text

log = logging.getLogger(__name__)


class KBSearchSkill:
    name = "kb_search"
    description = "检索当前群聊的本地知识库，返回最相关的条目内容。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要检索的关键词或问题"},
            "max_results": {"type": "integer", "description": "返回数量(1-10)", "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, knowledge: KnowledgeService) -> None:
        self.knowledge = knowledge

    @staticmethod
    def _split_document(title: str, document: str) -> tuple[str, str]:
        doc = str(document or "")
        parts = doc.split("\n", 1)
        parsed_title = str(title or "").strip()
        if not parsed_title:
            parsed_title = parts[0] if parts else ""
        parsed_content = parts[1] if len(parts) > 1 else doc
        return (
            clean_text(parsed_title, max_len=120),
            clean_text(parsed_content, max_len=1500),
        )

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        session = context.session
        message = context.message
        if not session or not message:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前上下文无法访问知识库",
                error="missing_context",
            )

        query = clean_text(str(arguments.get("query", "")), max_len=400)
        try:
            max_results = int(arguments.get("max_results", 5))
        except Exception:
            max_results = 5
        max_results = max(1, min(max_results, 10))

        if not query:
            return SkillRunResult(ok=False, skill=self.name, summary="检索词为空", error="empty_query")

        try:
            rows = await self.knowledge.search(session, message.chat.id, query)
        except Exception as exc:
            log.exception("kb_search failed")
            return SkillRunResult(ok=False, skill=self.name, summary="知识库检索失败", error=str(exc))

        if not rows:
            return SkillRunResult(ok=False, skill=self.name, summary="知识库没有匹配内容", error="empty_result")

        results: list[dict] = []
        for item in rows[:max_results]:
            metadata = item.get("metadata", {}) or {}
            title, content = self._split_document(
                str(metadata.get("title", "")),
                str(item.get("document", "")),
            )
            score_raw = item.get("score", 0.0)
            try:
                score = float(score_raw)
            except Exception:
                score = 0.0
            results.append(
                {
                    "title": title,
                    "content": content,
                    "score": round(score, 4),
                    "fallback": bool(metadata.get("fallback", False)),
                }
            )

        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"知识库命中 {len(results)} 条",
            payload={"query": query, "results": results},
        )
