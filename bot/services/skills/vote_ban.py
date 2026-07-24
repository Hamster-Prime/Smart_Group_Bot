"""AI tool for explicitly requested democratic vote-ban polls."""
from __future__ import annotations

import re

from bot.config import Settings
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.vote_ban import start_vote_ban
from bot.utils.security import clean_multiline_text
from bot.utils.telegram import confirm_telegram_delivery

_EXPLICIT_VOTE_REQUEST_RE = re.compile(
    r"(?:/voteban\b|(?:发起|开启|开始|开个|组织|来(?:一场|个)?)[^\n]{0,10}(?:民主|骚扰)?投票|"
    r"(?:民主|骚扰)?投票[^\n]{0,10}(?:封禁|封他|封她|处理|踢|赶走)|投出去)",
    re.IGNORECASE,
)


def is_explicit_vote_ban_request(text: str) -> bool:
    """Return whether one concrete Telegram message explicitly requests it."""
    cleaned = clean_multiline_text(str(text or ""), max_len=1200)
    return bool(_EXPLICIT_VOTE_REQUEST_RE.search(cleaned))


class VoteBanSkill:
    name = "vote_ban"
    description = (
        "Start a democratic vote-ban in the current Telegram group only when the current sender "
        "explicitly asks for a vote and their message replies to the target user's message. "
        "The target and starter are read from trusted Telegram context, never from model-provided IDs. "
        "This action enforces the same per-user quota as /voteban. If quota is exhausted, do not retry "
        "or suggest /voteban as a workaround; refuse using the returned reason and retry time."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Optional concise report reason supplied by the current sender. Do not invent facts; omit to use only the replied message as evidence.",
                "maxLength": 500,
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        if context.session is None or context.message is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前缺少群聊消息上下文，无法发起民主投票。",
                error="missing_context",
            )
        message = context.message
        sender = getattr(message, "from_user", None)
        sender_id = int(getattr(sender, "id", 0) or 0)
        if sender_id <= 0 or sender_id != int(context.sender_user_id or 0):
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="无法确认当前发起人身份，因此不能发起投票。",
                error="sender_identity_mismatch",
            )

        if not context.is_direct_request:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="这不是一条直接、明确的投票请求，因此我不会自行发起处罚流程。",
                error="not_direct_request",
            )

        # Bind the high-impact intent to the exact Telegram message that also
        # supplies reply_to_message.  Batched text from an earlier message may
        # inform the model, but must never authorize a later reply target.
        request_text = clean_multiline_text(
            str(getattr(message, "text", None) or getattr(message, "caption", None) or ""),
            max_len=1200,
        )
        if not is_explicit_vote_ban_request(request_text):
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="你需要明确说要发起民主投票，我不会仅凭闲聊自行处罚群成员。",
                error="not_explicit_request",
            )

        reason = clean_multiline_text(str(arguments.get("reason", "") or ""), max_len=500).strip()

        def _confirm_poll_delivery() -> None:
            # The poll itself is the reply. Record delivery at Telegram
            # acceptance time rather than after message-id persistence, which
            # may fail even though users can already see the poll.
            context.handled = True
            context.embedded_reply_sent = True
            context.embedded_reply_text = "民主投票已经发起。"
            context.suppress_followup_text = True
            confirm_telegram_delivery(context.delivery_callback)

        result = await start_vote_ban(
            message,
            context.session,
            self.settings,
            reason_override=reason,
            trigger_source="skill",
            session_factory=context.session_factory,
            on_delivery=_confirm_poll_delivery,
        )
        if not result.ok:
            payload = result.payload()
            payload["telegram_text"] = result.telegram_text
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=result.summary,
                error=result.code,
                payload=payload,
            )

        # Keep the richer confirmed summary for conversation memory after the
        # normal persistence path completes.
        context.embedded_reply_text = result.summary
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=result.summary,
            payload=result.payload(),
        )
