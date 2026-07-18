from __future__ import annotations

import asyncio
import logging

from bot.config import load_bootstrap_settings
from bot.db.engine import init_db
from bot.handlers import admin, commands, group, membership
from bot.loader import create_bot, dp
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware
from bot.middlewares.logging_mw import LoggingMiddleware
from bot.middlewares.member_roster import MemberRosterMiddleware
from bot.middlewares.profile_screen import ProfileScreenEnforcementMiddleware
from bot.services import memory_holder
from bot.services.join_verification import (
    JoinVerificationSweeper,
    VERIFICATION_PROVIDERS,
    verification_service_ready,
    warn_if_bot_cannot_verify,
)
from bot.services.group_permissions import (
    GroupPermissionService,
    init_group_permission_service,
)
from bot.services.llm import LLMService
from bot.services.memory import MemoryService
from bot.services.patrol import PatrolService, init_patrol_service
from bot.services.raid_guard import RaidGuardService, init_raid_guard_service
from bot.services.proactive import ProactiveTopicService
from bot.services.runtime_config import RuntimeConfig, RuntimeConfigManager
from bot.services.runtime_config import (
    hcaptcha_key_configuration_issue,
    turnstile_key_configuration_issue,
)
from bot.services.scheduled_messages import ScheduledMessageService
from bot.services.vote_ban import restore_vote_ban_tasks
from bot.services.update_delivery import resolve_webhook_config, run_update_delivery
from bot.utils.bot_identity import set_bot_identity
from bot.utils.logging_setup import configure_logging

configure_logging()
log = logging.getLogger(__name__)


async def main() -> None:
    settings = load_bootstrap_settings()
    if not settings.bot.token:
        raise ValueError("BOT_TOKEN is required")

    engine, session_factory = await init_db(settings.database_url)

    runtime_config = RuntimeConfigManager(
        session_factory=session_factory,
        settings=settings,
    )
    await runtime_config.initialize()
    configure_logging(force=True, config=runtime_config.config.logging)

    dp["settings"] = settings
    dp["session_factory"] = session_factory
    dp["runtime_config"] = runtime_config

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
    main_candidates = llm._chat_candidates(settings.bot.main_model)
    if main_candidates and all(llm.chat_configuration_issue(item) for item in main_candidates):
        log.warning(
            "AI 模型尚不可用：主模型及其回退均缺少所需凭据；"
            "请由最高管理员私聊 bot 发送 /settings 完成模型配置。"
        )
    if settings.bot.max_context_tokens <= 4096:
        log.warning(
            "当前最大上下文仅 %d Token，可能小于内置提示词；"
            "请在 /settings 的 Bot 行为中调高上下文预算。",
            settings.bot.max_context_tokens,
        )
    memory = MemoryService(
        settings.bot,
        llm,
        session_factory=session_factory,
    )
    await memory.bootstrap()
    memory_holder.init(memory)

    dp.message.outer_middleware(GlobalBanEnforcementMiddleware(session_factory))
    # Outer so it also covers matched commands (/help), videos, and empty-text
    # media that the group handler bypasses.
    dp.message.outer_middleware(ProfileScreenEnforcementMiddleware(session_factory))
    # Roster tracking feeds the profile patrol; outer so every sender is seen.
    dp.message.outer_middleware(MemberRosterMiddleware(session_factory))
    dp.message.middleware(LoggingMiddleware())
    # No throttle middleware: it silently drops rapid consecutive messages,
    # which breaks inbound batch merging and lets a fast second violating
    # message skip moderation. Burst smoothing is handled by the pending-reply
    # debounce in bot.handlers.group instead.
    dp.message.middleware(DbSessionMiddleware(session_factory))
    dp.callback_query.middleware(DbSessionMiddleware(session_factory))
    dp.chat_member.middleware(DbSessionMiddleware(session_factory))

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(membership.router)
    dp.include_router(group.router)

    bot = create_bot(settings)
    me = await bot.me()
    set_bot_identity(
        user_id=me.id,
        username=me.username or "",
        display_name=me.full_name or me.first_name or "",
    )
    log.info("Bot identity resolved: @%s (%s)", me.username, me.full_name)
    await restore_vote_ban_tasks(
        session_factory=session_factory,
        bot=bot,
        settings=settings,
    )
    proactive = ProactiveTopicService(
        settings=settings,
        bot=bot,
        memory=memory,
        session_factory=session_factory,
        llm=llm,
    )
    proactive_runner = asyncio.create_task(
        proactive.run_forever(),
        name="proactive-topic-runner",
    )
    sweeper = JoinVerificationSweeper(
        bot=bot,
        session_factory=session_factory,
        check_interval_seconds=settings.join_verification_check_interval_seconds,
        settings=settings,
    )
    # Persisted deadlines are drained while verification is healthy. If the
    # shared Turnstile config is unavailable, the sweeper preserves records
    # and refreshes their grace window instead of punishing users.
    verification_runner = asyncio.create_task(
        sweeper.run_forever(),
        name="verification-sweeper",
    )
    patrol = PatrolService(
        bot=bot,
        settings=settings,
        session_factory=session_factory,
    )
    init_patrol_service(patrol)
    patrol_runner = asyncio.create_task(
        patrol.run_forever(),
        name="profile-patrol-runner",
    )
    scheduled_messages = ScheduledMessageService(
        bot=bot,
        settings=settings,
        session_factory=session_factory,
    )
    scheduled_message_runner = asyncio.create_task(
        scheduled_messages.run_forever(),
        name="scheduled-message-runner",
    )
    group_permissions = GroupPermissionService(
        bot=bot,
        session_factory=session_factory,
    )
    init_group_permission_service(group_permissions)
    group_permission_runner = asyncio.create_task(
        group_permissions.run_forever(),
        name="group-permission-runner",
    )
    # Event-driven (no background loop): joins feed the detector from the
    # membership handler; challenge deadlines ride the shared sweeper. Manual
    # lockdowns are restored before update delivery starts so no join can slip
    # through the restart window.
    raid_guard = RaidGuardService(
        bot=bot,
        settings=settings,
        session_factory=session_factory,
    )
    init_raid_guard_service(raid_guard)

    async def apply_runtime_update(_config: RuntimeConfig) -> None:
        llm.reconfigure(
            settings.bot.main_model,
            settings.bot.decision_model,
            settings.bot.compress_model,
            moderation=settings.bot.moderation_model,
            vision=settings.bot.vision_model,
            embed=settings.bot.embed_model,
            max_context_tokens=settings.bot.max_context_tokens,
        )
        memory.reconfigure(settings.bot)
        sweeper.check_interval_seconds = max(
            5.0,
            float(settings.join_verification_check_interval_seconds),
        )
        configure_logging(force=True, config=runtime_config.config.logging)

    runtime_config.set_apply_callback(apply_runtime_update)

    from bot.services.verify_web import VerifyWebServer

    webhook, webhook_fallback_reason = resolve_webhook_config(settings)
    verify_web = VerifyWebServer(
        bot=bot,
        settings=settings,
        session_factory=session_factory,
        runtime_config=runtime_config,
        webhook_dispatcher=dp if webhook is not None else None,
        webhook_path=webhook.path if webhook is not None else "",
        webhook_secret=webhook.secret if webhook is not None else "",
    )
    try:
        await raid_guard.restore_manual_lockdowns()
        await verify_web.start()
    except Exception:
        proactive_runner.cancel()
        verification_runner.cancel()
        patrol_runner.cancel()
        scheduled_message_runner.cancel()
        group_permission_runner.cancel()
        await asyncio.gather(
            proactive_runner,
            verification_runner,
            patrol_runner,
            scheduled_message_runner,
            group_permission_runner,
            return_exceptions=True,
        )
        raid_guard.reset()
        try:
            await bot.session.close()
        except Exception:
            log.exception("Bot HTTP session cleanup failed after Mini App startup error")
        await engine.dispose()
        raise
    if verify_web.webhook_route_error is not None:
        webhook_fallback_reason = verify_web.webhook_route_error
        webhook = None
    verification_issues = {
        "turnstile": turnstile_key_configuration_issue(
            settings.join_verification_turnstile_site_key,
            settings.join_verification_turnstile_secret_key,
        ),
        "hcaptcha": hcaptcha_key_configuration_issue(
            settings.join_verification_hcaptcha_site_key,
            settings.join_verification_hcaptcha_secret_key,
        ),
    }
    for provider, issue in verification_issues.items():
        if issue:
            log.error(
                "%s；已暂停使用 %s 签发真人质询，请在 /settings 更正后重试。",
                issue,
                provider,
            )
    if any(
        verification_service_ready(settings, provider)
        for provider in VERIFICATION_PROVIDERS
    ):
        await warn_if_bot_cannot_verify(bot, settings, session_factory)
    elif settings.moderation.enabled or settings.join_verification_enabled:
        log.warning(
            "真人验证密钥或公网验证地址不完整；入群验证不会签发，"
            "低置信度审核也将回退到原群规动作。"
        )
    log.info("Bot starting...")

    try:
        await run_update_delivery(
            bot=bot,
            dispatcher=dp,
            settings=settings,
            webhook=webhook,
            fallback_reason=webhook_fallback_reason,
            enable_webhook_route=verify_web.enable_webhook_route,
            disable_webhook_route=verify_web.disable_webhook_route,
        )
    finally:
        proactive_runner.cancel()
        await asyncio.gather(proactive_runner, return_exceptions=True)
        verification_runner.cancel()
        await asyncio.gather(verification_runner, return_exceptions=True)
        patrol_runner.cancel()
        await asyncio.gather(patrol_runner, return_exceptions=True)
        scheduled_message_runner.cancel()
        await asyncio.gather(scheduled_message_runner, return_exceptions=True)
        group_permission_runner.cancel()
        await asyncio.gather(group_permission_runner, return_exceptions=True)
        raid_guard.reset()
        try:
            await patrol.shutdown()
        except Exception:
            log.exception("patrol manual-task shutdown failed")
        try:
            await verify_web.stop()
        except Exception:
            log.exception("Mini App web server shutdown failed")
        try:
            await group.flush_pending_inbound_batches()
        except Exception:
            log.exception("pending inbound batch flush failed")
        try:
            await bot.session.close()
        except Exception:
            log.exception("Bot HTTP session shutdown failed")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
