"""Keyword auto replies: Markdown templates triggered by per-group rules.

Rules live in the keyword_replies table and are managed from the Mini App.
Matching runs inside the group message handler after moderation, so a
violating message never triggers a canned reply. A hit sends the configured
text (optionally pinning it and opting it into the "keyword" auto-delete
category) and suppresses the AI reply pipeline for that message.
"""
from __future__ import annotations

import logging

import regex

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import KeywordReply
from bot.services.message_templates import (
    build_template_keyboard,
    render_markdown_html,
    render_plain_template,
)
from bot.utils.telegram import (
    answer_with_auto_delete,
    configured_auto_delete_seconds,
)

log = logging.getLogger(__name__)

KEYWORD_MATCH_TYPES = ("contains", "exact", "regex")
# The regex engine timeout keeps a catastrophic-backtracking pattern from
# stalling the event loop; the stdlib re module has no such guard.
REGEX_MATCH_TIMEOUT_SECONDS = 0.05


def keyword_rule_matches(rule: KeywordReply, text: str) -> bool:
    keyword = str(rule.keyword or "").strip()
    subject = str(text or "")
    if not keyword or not subject:
        return False
    match_type = str(rule.match_type or "contains")
    if match_type == "exact":
        return subject.strip().casefold() == keyword.casefold()
    if match_type == "regex":
        try:
            return (
                regex.search(keyword, subject, timeout=REGEX_MATCH_TIMEOUT_SECONDS)
                is not None
            )
        except (regex.error, TimeoutError):
            log.warning(
                "keyword regex skipped (invalid or too slow) | group=%s rule=%s",
                rule.group_id,
                rule.id,
            )
            return False
    return keyword.casefold() in subject.casefold()


async def find_keyword_reply(
    session: AsyncSession,
    group_id: int,
    text: str,
) -> KeywordReply | None:
    """Return the first enabled rule matching the message text, if any."""
    result = await session.scalars(
        select(KeywordReply)
        .where(
            KeywordReply.group_id == int(group_id),
            KeywordReply.enabled.is_(True),
        )
        .order_by(KeywordReply.id)
    )
    for rule in result.all():
        if keyword_rule_matches(rule, text):
            # Detach so a later rollback on this session (memory/style
            # bookkeeping) cannot expire the row before the reply is sent.
            expunge = getattr(session, "expunge", None)
            if callable(expunge):
                expunge(rule)
            return rule
    return None


async def send_keyword_reply(
    message: Message,
    rule: KeywordReply,
    settings: Settings,
) -> bool:
    """Send one keyword reply; returns False if Telegram rejected the send."""
    auto_delete_seconds = (
        configured_auto_delete_seconds(settings, "keyword") if rule.auto_delete else 0
    )
    source_text = str(rule.reply_text or "")
    rendered_text = render_markdown_html(source_text)
    keyboard = build_template_keyboard(getattr(rule, "buttons", None))
    try:
        sent = await answer_with_auto_delete(
            message,
            rendered_text,
            auto_delete_seconds=auto_delete_seconds,
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
            reply_markup=keyboard,
            plain_text_fallback=render_plain_template(source_text),
            sanitize_mentions=False,
            drop_invalid_reply_markup=True,
        )
    except Exception:
        log.exception(
            "keyword reply send failed | group=%s rule=%s",
            rule.group_id,
            rule.id,
        )
        return False
    if rule.pin_message and sent is not None:
        try:
            await message.bot.pin_chat_message(
                chat_id=message.chat.id,
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception:
            log.warning(
                "keyword reply pin failed | group=%s rule=%s",
                rule.group_id,
                rule.id,
            )
    return True
