from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Awaitable, Iterable
from typing import Any

from bot.config import load_bootstrap_settings, validate_bootstrap_settings
from bot.db.engine import init_db
from bot.handlers import admin, commands, group, membership
from bot.loader import create_bot, dp
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware
from bot.middlewares.logging_mw import LoggingMiddleware
from bot.middlewares.member_roster import MemberRosterMiddleware
from bot.middlewares.profile_screen import ProfileScreenEnforcementMiddleware
from bot.middlewares.update_dedup import DurableInboxUpdateDedupMiddleware
from bot.middlewares.verification_gate import PendingVerificationGateMiddleware
from bot.services import memory_holder
from bot.services.authz import warm_privileged_operator_cache
from bot.services.join_verification import (
    JoinVerificationSweeper,
    VERIFICATION_PROVIDERS,
    flush_kick_cleanup_tasks,
    verification_service_ready,
    warn_if_bot_cannot_verify,
)
from bot.services.group_permissions import (
    GroupPermissionService,
    init_group_permission_service,
)
from bot.services.llm import LLMService, close_llm_clients, flush_llm_request_tasks
from bot.services.memory import MemoryService
from bot.services.patrol import PatrolService, init_patrol_service
from bot.services.raid_guard import RaidGuardService, init_raid_guard_service
from bot.services.proactive import ProactiveTopicService
from bot.services.privileged_tasks import flush_privileged_tasks
from bot.services.resource_health import (
    run_resource_watchdog,
    start_hard_loop_watchdog,
)
from bot.services.runtime_config import (
    RuntimeConfig,
    RuntimeConfigManager,
    hcaptcha_key_configuration_issue,
    turnstile_key_configuration_issue,
)
from bot.services.scheduled_messages import ScheduledMessageService
from bot.services.skills.service import flush_skill_execution_tasks
from bot.services.telegram_cleanup import TelegramCleanupScheduler
from bot.services.vote_ban import flush_vote_ban_tasks, restore_vote_ban_tasks
from bot.services.update_delivery import (
    flush_update_delivery_tasks,
    resolve_webhook_config,
    run_update_delivery,
)
from bot.utils.bot_identity import set_bot_identity
from bot.utils.logging_setup import configure_logging
from bot.utils.telegram import (
    configure_telegram_cleanup_scheduler,
    flush_telegram_background_tasks,
)

configure_logging()
log = logging.getLogger(__name__)

_BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_CLEANUP_TIMEOUT_SECONDS = 20.0
# The reply-batch budget is configurable up to 120 seconds.  Leave enough
# room for its own bounded failure notification/cleanup instead of cancelling
# a healthy flush at the old 30-second outer limit.
_PENDING_FLUSH_TIMEOUT_SECONDS = 35.0
_WEB_SERVER_CLEANUP_TIMEOUT_SECONDS = 20.0
_CANCELLATION_GRACE_SECONDS = 1.0
_FINAL_LOOP_DRAIN_SECONDS = 2.0
_FORCED_PROCESS_EXIT_SECONDS = 5.0
_ORDERED_SHUTDOWN_HARD_LIMIT_SECONDS = 110.0


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        log.error(
            "Detached task finished after shutdown | task=%s error=%s",
            task.get_name(),
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


async def _cancel_tasks_bounded(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    label: str,
    timeout: float = _BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    all_tasks = list(tasks)
    pending_tasks = [task for task in all_tasks if not task.done()]
    for task in pending_tasks:
        task.cancel()
    if not all_tasks:
        return
    done, pending = await asyncio.wait(all_tasks, timeout=timeout)
    for task in done:
        _consume_task_result(task)
    if pending:
        log.error(
            "%s shutdown exceeded %.1fs; tasks still running=%s",
            label,
            timeout,
            ",".join(sorted(task.get_name() for task in pending)),
        )
        for task in pending:
            task.add_done_callback(_consume_task_result)


async def _await_cleanup_bounded(
    awaitable: Awaitable[Any],
    *,
    label: str,
    timeout: float = _CLEANUP_TIMEOUT_SECONDS,
) -> None:
    task = asyncio.create_task(awaitable, name=f"shutdown-{label}")
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        try:
            await task
        except asyncio.CancelledError:
            log.error("%s cleanup was unexpectedly cancelled", label)
        except Exception:
            log.exception("%s cleanup failed", label)
        return

    log.error("%s cleanup exceeded %.1fs; cancelling", label, timeout)
    task.cancel()
    done, _pending = await asyncio.wait(
        {task},
        timeout=_CANCELLATION_GRACE_SECONDS,
    )
    if task in done:
        _consume_task_result(task)
    else:
        task.add_done_callback(_consume_task_result)


async def _run_with_background_supervision(
    delivery: Awaitable[str],
    *,
    background_tasks: list[asyncio.Task[Any]],
) -> str:
    """Fail the process when a supposedly permanent service exits."""
    delivery_task = asyncio.create_task(delivery, name="telegram-update-delivery")
    try:
        done, _pending = await asyncio.wait(
            {delivery_task, *background_tasks},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if delivery_task in done:
            return await delivery_task

        failed = next(task for task in done if task is not delivery_task)
        if failed.cancelled():
            detail = "was unexpectedly cancelled"
            error: BaseException | None = None
        else:
            error = failed.exception()
            detail = (
                f"failed: {error}" if error is not None else "exited unexpectedly"
            )
        await _cancel_tasks_bounded(
            [delivery_task],
            label="update delivery after background failure",
        )
        failure = RuntimeError(
            f"background service {failed.get_name()} {detail}"
        )
        if error is not None:
            raise failure from error
        raise failure
    finally:
        if not delivery_task.done():
            await _cancel_tasks_bounded(
                [delivery_task],
                label="update delivery",
            )


async def _drain_final_loop_tasks(*, timeout_seconds: float) -> None:
    current = asyncio.current_task()
    tasks = {
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    }
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
    for task in done:
        _consume_task_result(task)
    if pending:
        log.critical(
            "event loop closing with %d cancellation-resistant task(s): %s",
            len(pending),
            ",".join(sorted(task.get_name() for task in pending)),
        )
        # Loop.close() must not emit a warning for tasks we deliberately abandon
        # after all shared Bot/DB resources have already been closed.
        for task in pending:
            setattr(task, "_log_destroy_pending", False)


def _arm_forced_exit_watchdog(
    timeout_seconds: float = _FORCED_PROCESS_EXIT_SECONDS,
) -> threading.Event:
    stopped = threading.Event()

    def _force_exit() -> None:
        if not stopped.wait(max(0.1, float(timeout_seconds))):
            os._exit(1)

    threading.Thread(
        target=_force_exit,
        name="bot-shutdown-watchdog",
        daemon=True,
    ).start()
    return stopped


def run_async_entrypoint(
    awaitable: Awaitable[Any],
    *,
    force_exit_watchdog: bool = False,
) -> Any:
    """Run the process loop without asyncio.run's unbounded final gather."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hard_loop_watchdog = start_hard_loop_watchdog(loop)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        if force_exit_watchdog:
            # main() already ran all ordered resource cleanup. If interpreter
            # shutdown (typically a stuck executor/provider) still cannot finish,
            # guarantee the service manager can restart this process.
            _arm_forced_exit_watchdog()
        try:
            loop.run_until_complete(
                _drain_final_loop_tasks(timeout_seconds=_FINAL_LOOP_DRAIN_SECONDS)
            )
        finally:
            asyncio.set_event_loop(None)
            try:
                loop.close()
            finally:
                hard_loop_watchdog.set()


def run_bot() -> None:
    # SQLite WAL files, runtime logs and any future secret-bearing artifacts
    # created by the service should default to owner-only permissions. Explicit
    # chmods still cover files opened during import and existing deployments.
    os.umask(0o077)
    run_async_entrypoint(main(), force_exit_watchdog=True)


async def _initialize_runtime_services(
    *,
    settings: Any,
    session_factory: Any,
) -> tuple[RuntimeConfigManager, LLMService, MemoryService]:
    runtime_config = RuntimeConfigManager(
        session_factory=session_factory,
        settings=settings,
    )
    await runtime_config.initialize()
    configure_logging(force=True, config=runtime_config.config.logging)

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
    if main_candidates and all(
        llm.chat_configuration_issue(item) for item in main_candidates
    ):
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
    return runtime_config, llm, memory


async def main() -> None:
    settings = load_bootstrap_settings()
    validate_bootstrap_settings(settings)

    engine, session_factory = await init_db(settings.database_url)
    try:
        runtime_config, llm, memory = await _initialize_runtime_services(
            settings=settings,
            session_factory=session_factory,
        )
    except BaseException:
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after runtime bootstrap failure",
        )
        raise

    dp["settings"] = settings
    dp["session_factory"] = session_factory
    dp["runtime_config"] = runtime_config
    warmed_operators = await warm_privileged_operator_cache(session_factory)
    if warmed_operators:
        log.info("Preloaded %d delegated privileged operator scope(s)", warmed_operators)

    dp.update.outer_middleware(DurableInboxUpdateDedupMiddleware(session_factory))
    dp.message.outer_middleware(GlobalBanEnforcementMiddleware(session_factory))
    # A member racing the join-verification mute must not get messages through
    # (or consume profile-screening LLM capacity) before the challenge exists.
    dp.message.outer_middleware(PendingVerificationGateMiddleware(session_factory))
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
    dp.my_chat_member.middleware(DbSessionMiddleware(session_factory))

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(membership.router)
    dp.include_router(group.router)

    try:
        bot = create_bot(settings)
    except BaseException:
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after Bot construction failure",
        )
        raise
    telegram_cleanup: TelegramCleanupScheduler | None = None
    try:
        me = await bot.me()
        set_bot_identity(
            user_id=me.id,
            username=me.username or "",
            display_name=me.full_name or me.first_name or "",
        )
        log.info("Bot identity resolved: @%s (%s)", me.username, me.full_name)
        telegram_cleanup = TelegramCleanupScheduler(
            bot=bot,
            session_factory=session_factory,
        )
        configure_telegram_cleanup_scheduler(telegram_cleanup)
        await telegram_cleanup.start()
        await restore_vote_ban_tasks(
            session_factory=session_factory,
            bot=bot,
            settings=settings,
        )
    except BaseException:
        configure_telegram_cleanup_scheduler(None)
        if telegram_cleanup is not None:
            await _await_cleanup_bounded(
                telegram_cleanup.stop(),
                label="Telegram cleanup after identity/bootstrap failure",
            )
        await _await_cleanup_bounded(
            bot.session.close(),
            label="Bot HTTP session after identity/bootstrap failure",
        )
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after identity/bootstrap failure",
        )
        raise
    background_tasks: list[asyncio.Task[Any]] = []
    try:
        proactive = ProactiveTopicService(
            settings=settings,
            bot=bot,
            memory=memory,
            session_factory=session_factory,
            llm=llm,
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=session_factory,
            check_interval_seconds=settings.join_verification_check_interval_seconds,
            settings=settings,
        )
        patrol = PatrolService(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
        )
        init_patrol_service(patrol)
        scheduled_messages = ScheduledMessageService(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
        )
        group_permissions = GroupPermissionService(
            bot=bot,
            session_factory=session_factory,
        )
        init_group_permission_service(group_permissions)
        # Event-driven (no background loop): joins feed the detector from the
        # membership handler; challenge deadlines ride the shared sweeper.
        raid_guard = RaidGuardService(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
        )
        init_raid_guard_service(raid_guard)
        logging_config_snapshot = runtime_config.config.logging.model_dump(mode="json")

        async def apply_runtime_update(_config: RuntimeConfig) -> None:
            nonlocal logging_config_snapshot
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
            next_logging_config = runtime_config.config.logging.model_dump(mode="json")
            if next_logging_config != logging_config_snapshot:
                configure_logging(force=True, config=runtime_config.config.logging)
                logging_config_snapshot = next_logging_config

        runtime_config.set_apply_callback(apply_runtime_update)

        from bot.services.verify_web import VerifyWebServer

        webhook, webhook_fallback_reason = resolve_webhook_config(settings)
        verify_web = VerifyWebServer(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
            runtime_config=runtime_config,
            # The durable inbox must remain consumable even when this boot
            # starts directly in polling mode (for example after a previous
            # webhook ACK but before its handler completed).
            webhook_dispatcher=dp,
            webhook_path=webhook.path if webhook is not None else "",
            webhook_secret=webhook.secret if webhook is not None else "",
        )
    except BaseException:
        # No runner has been scheduled yet, so construction failure cleanup is
        # limited to the shared resources that are already live.
        configure_telegram_cleanup_scheduler(None)
        await _await_cleanup_bounded(
            telegram_cleanup.stop(),
            label="Telegram cleanup after service construction failure",
        )
        await _await_cleanup_bounded(
            bot.session.close(),
            label="Bot HTTP session after service construction failure",
        )
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after service construction failure",
        )
        raise
    try:
        await raid_guard.restore_manual_lockdowns()
        await verify_web.start()

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

        # Do not let permanent runners execute while startup recovery or the
        # web listener is only partially initialized.  If either step above
        # fails, ``background_tasks`` stays empty and the common cleanup path
        # closes every resource without a runner racing it.
        background_tasks = [
            asyncio.create_task(
                telegram_cleanup.monitor(),
                name="telegram-cleanup-monitor",
            ),
            asyncio.create_task(
                proactive.run_forever(),
                name="proactive-topic-runner",
            ),
            # Persisted deadlines are drained while verification is healthy. If
            # the shared CAPTCHA config is unavailable, the sweeper preserves
            # records instead of punishing users.
            asyncio.create_task(
                sweeper.run_forever(),
                name="verification-sweeper",
            ),
            asyncio.create_task(
                patrol.run_forever(),
                name="profile-patrol-runner",
            ),
            asyncio.create_task(
                scheduled_messages.run_forever(),
                name="scheduled-message-runner",
            ),
            asyncio.create_task(
                group_permissions.run_forever(),
                name="group-permission-runner",
            ),
            asyncio.create_task(
                run_resource_watchdog(),
                name="resource-health-watchdog",
            ),
        ]

        await _run_with_background_supervision(
            run_update_delivery(
                bot=bot,
                dispatcher=dp,
                settings=settings,
                webhook=webhook,
                fallback_reason=webhook_fallback_reason,
                enable_webhook_route=verify_web.enable_webhook_route,
                mark_webhook_active=verify_web.mark_webhook_active,
                disable_webhook_route=verify_web.disable_webhook_route,
                mark_polling_active=verify_web.mark_polling_active,
                webhook_runtime_failure=verify_web.webhook_runtime_failure,
                start_update_processor=getattr(
                    verify_web,
                    "start_update_processor",
                    None,
                ),
                durable_update_ingest=getattr(
                    verify_web,
                    "accept_polling_update",
                    None,
                ),
            ),
            background_tasks=background_tasks,
        )
    finally:
        ordered_shutdown_watchdog = _arm_forced_exit_watchdog(
            _ORDERED_SHUTDOWN_HARD_LIMIT_SECONDS
        )
        await _cancel_tasks_bounded(
            background_tasks,
            label="background services",
        )
        await _await_cleanup_bounded(
            flush_update_delivery_tasks(),
            label="update delivery control tasks",
        )
        # Transport delivery has stopped. Quiesce recovery/dispatch first, but
        # keep durable finalizers alive until every explicit detached owner has
        # finished; otherwise a graceful deploy can strand a live lease for the
        # full recovery interval.
        await _await_cleanup_bounded(
            verify_web.quiesce_update_processor(),
            label="Telegram update processor quiesce",
        )
        await _await_cleanup_bounded(
            flush_privileged_tasks(),
            label="privileged security tasks",
        )
        await _await_cleanup_bounded(
            raid_guard.shutdown(),
            label="raid guard timer tasks",
        )
        await _await_cleanup_bounded(
            patrol.shutdown(),
            label="patrol manual tasks",
        )
        # Update delivery has stopped accepting Telegram updates at this point,
        # while the shared Bot HTTP session is deliberately still open for any
        # final replies produced by the bounded pending-batch flush.
        await _await_cleanup_bounded(
            group.flush_pending_inbound_batches(),
            label="pending inbound batches",
            timeout=_PENDING_FLUSH_TIMEOUT_SECONDS,
        )
        memory_shutdown = getattr(memory, "shutdown", None)
        if callable(memory_shutdown):
            await _await_cleanup_bounded(
                memory_shutdown(),
                label="memory maintenance tasks",
            )
        await _await_cleanup_bounded(
            flush_skill_execution_tasks(),
            label="skill execution tasks",
        )
        await _await_cleanup_bounded(
            flush_llm_request_tasks(),
            label="LLM request tasks",
        )
        await _await_cleanup_bounded(
            close_llm_clients(),
            label="LiteLLM HTTP clients",
        )
        await _await_cleanup_bounded(
            flush_vote_ban_tasks(),
            label="vote-ban tasks",
        )
        await _await_cleanup_bounded(
            flush_kick_cleanup_tasks(),
            label="kick cleanup tasks",
        )
        await _await_cleanup_bounded(
            verify_web.stop_update_processor(),
            label="Telegram update processor finalizers",
        )
        await _await_cleanup_bounded(
            verify_web.stop(),
            label="Mini App web server",
            timeout=_WEB_SERVER_CLEANUP_TIMEOUT_SECONDS,
        )
        # Pending-reply flushing above may still enqueue final cleanup jobs.
        # Persist every accepted request and stop the single scheduler before
        # either its Telegram client or database engine is closed.
        await _await_cleanup_bounded(
            telegram_cleanup.stop(),
            label="Telegram cleanup scheduler",
        )
        configure_telegram_cleanup_scheduler(None)
        await _await_cleanup_bounded(
            flush_telegram_background_tasks(),
            label="Telegram delayed cleanup tasks",
        )
        await _await_cleanup_bounded(
            bot.session.close(),
            label="Bot HTTP session",
        )
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine",
        )
        ordered_shutdown_watchdog.set()


if __name__ == "__main__":
    run_bot()
