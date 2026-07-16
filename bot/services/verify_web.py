"""Built-in HTTP server for settings and human-verification Mini Apps.

The shared server listens on MINIAPP_LISTEN_HOST:PORT and is exposed through
MINIAPP_PUBLIC_BASE_URL. It hosts the administrator settings application plus
the join/moderation verification challenge.

- GET  /verify: the Mini App page embedding the selected provider widget. Opened
  inside Telegram via a web_app button — the URL is never shown to the user.
- POST /verify: receives the widget token plus Telegram's signed initData.
  The initData signature is validated against the bot token (aiogram's
  safe_parse_webapp_init_data), which authenticates the user without any
  link token; the challenge token is validated against the selected provider's
  siteverify API. Both must pass before the member's restriction is lifted.
- GET /settings and /api/v1/*: the super-admin configuration application and
  authenticated database-backed settings API.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiogram.utils.web_app import safe_parse_webapp_init_data
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import JoinVerification, UserWarning
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_PATROL,
    claim_join_verification,
    clear_turnstile_configuration_unavailable,
    delete_join_verification,
    extend_pending_verification_deadlines,
    get_join_verification,
    get_pending_verification_by_id_for_user,
    get_pending_verification_for_user,
    get_sole_pending_verification_for_user,
    mark_turnstile_configuration_unavailable,
    normalize_verification_provider,
    restore_member_permissions,
    turnstile_verification_configured,
    upsert_join_verification,
    verification_deadline_passed,
    verification_keys_for_provider,
    verification_provider,
    verification_timeout_seconds_for_kind,
)
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.update_delivery import WEBHOOK_MAX_CONCURRENT_UPDATES
from bot.utils.timezone import now_shanghai_naive
from bot.web.settings_api import register_settings_routes

log = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
HCAPTCHA_VERIFY_URL = "https://api.hcaptcha.com/siteverify"
TURNSTILE_ERROR_SECRET_INVALID = "invalid-input-secret"
TURNSTILE_ERROR_SECRET_MISSING = "missing-input-secret"
TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE = "siteverify-unavailable"
_TURNSTILE_CONFIGURATION_ERRORS = {
    TURNSTILE_ERROR_SECRET_INVALID,
    TURNSTILE_ERROR_SECRET_MISSING,
}
_HCAPTCHA_CONFIGURATION_ERRORS = {
    "missing-input-secret",
    "invalid-input-secret",
    "sitekey-secret-mismatch",
    "not-using-dummy-passcode",
    # "not-using-dummy-secret" (test site key + production secret) is left out
    # on purpose: it is triggered by the user-supplied token, so mapping it to
    # the sticky configuration block would let any member disable the provider
    # by submitting hCaptcha's well-known dummy passcode.
}
_HCAPTCHA_REPLAY_ERRORS = {
    "already-seen-response",
    "invalid-or-already-seen-response",
}
# The widget token is single-use and short-lived (hCaptcha: 120s). These codes
# mean the human solved the challenge but the token can no longer be redeemed,
# so the member must re-solve — not "you failed the challenge".
_CHALLENGE_TOKEN_RETRY_ERRORS = {
    "expired-input-response",
    "already-seen-response",
    "invalid-or-already-seen-response",
    "timeout-or-duplicate",
}
_TURNSTILE_SERVICE_ERRORS = {
    TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE,
    "internal-error",
}
_COMPLETED_VERIFICATION_TTL_SECONDS = 5 * 60
_ACTIVE_VERIFICATION_TTL_SECONDS = 2 * 60
_VERIFICATION_WAIT_TIMEOUT_SECONDS = 60.0
_VERIFICATION_CACHE_MAX_ENTRIES = 2048
_SETTINGS_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

_VerificationFingerprint = tuple[int, int, str, str, str, str, int]


@dataclass(slots=True)
class _ActiveVerification:
    started_at: float
    fingerprint: _VerificationFingerprint
    future: asyncio.Future[bool]
    participants: int = 1


def _public_ip(value: object) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return ""
    return address.compressed if address.is_global else ""


def _client_public_ip(request: web.Request) -> str:
    peer = str(request.remote or "").strip()
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return ""
    if peer_address.is_global:
        return peer_address.compressed
    if peer_address.is_loopback or peer_address.is_private:
        return _public_ip(request.headers.get("CF-Connecting-IP"))
    return ""


def _verification_config_tag(
    settings: Settings,
    provider: str,
) -> str:
    """Opaque public tag that detects key rotation between GET and POST."""
    site_key, secret_key = verification_keys_for_provider(settings, provider)
    payload = "\x00".join(
        (
            normalize_verification_provider(provider),
            site_key,
            secret_key,
            settings.join_verification_public_base_url.strip().rstrip("/"),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_SETTINGS_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'"
)

_CHALLENGE_CSP_BASE = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org https://challenges.cloudflare.com "
    "https://hcaptcha.com https://*.hcaptcha.com https://js.hcaptcha.com {nonce}; "
    "style-src 'self' 'unsafe-inline' https://hcaptcha.com https://*.hcaptcha.com; "
    "connect-src 'self' https://challenges.cloudflare.com https://hcaptcha.com "
    "https://*.hcaptcha.com https://api.hcaptcha.com; "
    "frame-src https://challenges.cloudflare.com https://hcaptcha.com https://*.hcaptcha.com; "
    "img-src 'self' data: https://hcaptcha.com https://*.hcaptcha.com; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>真人验证</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="{script_url}" async defer></script>
<style>
  body {{
    box-sizing: border-box;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 100vh; margin: 0; padding: 16px;
    background: var(--tg-theme-bg-color, #f5f6f8);
    color: var(--tg-theme-text-color, #1f2328);
  }}
  .card {{
    box-sizing: border-box;
    background: var(--tg-theme-secondary-bg-color, #fff);
    border-radius: 8px; padding: 28px 20px;
    width: min(100%, 380px); text-align: center; overflow: visible;
  }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  p {{ font-size: 14px; opacity: .75; margin: 0 0 20px; }}
  #status {{ margin-top: 16px; font-size: 14px; min-height: 20px; }}
  .challenge-widget {{ display: flex; max-width: 100%; justify-content: center; overflow: visible; }}
  .challenge-widget > * {{ max-width: 100%; }}
  .ok {{ color: #1a7f37; }}
  .err {{ color: #cf222e; }}
  @media (max-width: 360px) {{
    body {{ padding: 8px; }}
    .card {{ padding: 22px 10px; }}
  }}
</style>
</head>
<body>
<div class="card">
  <h1>真人验证</h1>
  <p>完成下方人机验证后即可在群内发言。</p>
  <div class="challenge-widget">{widget_html}</div>
  <div id="status"></div>
</div>
<script nonce="{nonce}">
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) {{ tg.ready(); tg.expand(); }}

const provider = "{provider}";
const configTag = "{config_tag}";
let verificationId = {verification_id};
let challengeSubmitting = false;

function resetChallenge() {{
  if (provider === "hcaptcha" && window.hcaptcha) window.hcaptcha.reset();
  if (provider === "turnstile" && window.turnstile) window.turnstile.reset();
}}

function onChallengeError() {{
  if (challengeSubmitting) return;
  const status = document.getElementById("status");
  status.textContent = "❌ 验证组件暂时不可用，请稍后重试。";
  status.className = "err";
  setTimeout(resetChallenge, 1000);
}}

function onChallengeExpired() {{
  if (challengeSubmitting) return;
  const status = document.getElementById("status");
  status.textContent = "验证已过期，请重新完成验证。";
  status.className = "err";
  resetChallenge();
}}

async function onChallengeSuccess(token) {{
  if (challengeSubmitting) return;
  challengeSubmitting = true;
  const status = document.getElementById("status");
  if (!tg || !tg.initData) {{
    challengeSubmitting = false;
    status.textContent = "❌ 请在 Telegram 内打开本页面。";
    status.className = "err";
    return;
  }}
  status.textContent = "验证中…";
  status.className = "";
  try {{
    const resp = await fetch("/verify", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{challenge_token: token, provider, config_tag: configTag, verification_id: verificationId, init_data: tg.initData}}),
    }});
    const data = await resp.json();
    if (resp.ok && data.ok) {{
      status.textContent = "✅ 验证通过，群内权限已恢复。";
      status.className = "ok";
      setTimeout(() => tg.close(), 1500);
    }} else {{
      challengeSubmitting = false;
      if (Number.isInteger(data.verification_id) && data.verification_id > 0) {{
        verificationId = data.verification_id;
      }}
      status.textContent = "❌ " + (data.error || "验证失败，请重试。");
      status.className = "err";
      resetChallenge();
    }}
  }} catch (e) {{
    challengeSubmitting = false;
    status.textContent = "❌ 网络错误，请稍后重试。";
    status.className = "err";
    resetChallenge();
  }}
}}
</script>
</body>
</html>"""


async def verify_turnstile_token(
    *,
    secret_key: str,
    turnstile_token: str,
    remote_ip: str = "",
    timeout_sec: float = 10.0,
) -> tuple[bool, list[str]]:
    """Server-side validation against Cloudflare's siteverify endpoint."""
    payload: dict[str, str] = {
        "secret": secret_key,
        "response": (turnstile_token or "").strip(),
    }
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        timeout = aiohttp.ClientTimeout(total=max(1.0, timeout_sec))
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(TURNSTILE_VERIFY_URL, data=payload) as resp:
                if resp.status == 429 or resp.status >= 500:
                    log.warning("turnstile siteverify unavailable | status=%s", resp.status)
                    return False, [TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE]
                data = await resp.json(content_type=None)
    except Exception:
        log.exception("turnstile siteverify request failed")
        return False, [TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE]
    if not isinstance(data, dict):
        log.warning("turnstile siteverify returned invalid payload")
        return False, [TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE]
    success = bool(data.get("success"))
    errors = [
        str(item)
        for item in (data.get("error-codes") or [])
        if str(item).strip()
    ]
    if not success:
        log.info(
            "turnstile siteverify rejected | errors=%s",
            errors or data,
        )
    return success, errors


async def verify_hcaptcha_token(
    *,
    secret_key: str,
    hcaptcha_token: str,
    remote_ip: str = "",
    site_key: str = "",
    timeout_sec: float = 10.0,
) -> tuple[bool, list[str]]:
    """Server-side validation against hCaptcha's siteverify endpoint."""
    payload: dict[str, str] = {
        "secret": secret_key,
        "response": (hcaptcha_token or "").strip(),
    }
    if remote_ip:
        payload["remoteip"] = remote_ip
    if site_key:
        payload["sitekey"] = site_key
    try:
        timeout = aiohttp.ClientTimeout(total=max(1.0, timeout_sec))
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(HCAPTCHA_VERIFY_URL, data=payload) as resp:
                if resp.status == 429 or resp.status >= 500:
                    log.warning("hcaptcha siteverify unavailable | status=%s", resp.status)
                    return False, [TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE]
                data = await resp.json(content_type=None)
    except Exception:
        log.exception("hcaptcha siteverify request failed")
        return False, [TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE]
    if not isinstance(data, dict):
        return False, [TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE]
    raw_errors = data.get("error-codes") or []
    if isinstance(raw_errors, str):
        raw_errors = [raw_errors]
    errors = [str(item) for item in raw_errors if str(item).strip()]
    success = bool(data.get("success"))
    if not success:
        token = (hcaptcha_token or "").strip()
        log.info(
            "hcaptcha siteverify rejected | errors=%s token_len=%s token_shape=%s",
            errors or ["unknown"],
            len(token),
            "p1" if token.startswith("P1_") else "other",
        )
        if token.startswith("P1_") and len(token) > 200 and "invalid-input-response" in errors:
            # siteverify never validates the secret itself: a token from a
            # sitekey the secret's account does not own is simply "not found"
            # and reported as invalid-input-response. A well-formed P1_ solve
            # token rejected this way therefore usually means the Site Key and
            # Secret Key come from different hCaptcha accounts (or the secret
            # was rotated), not that the member failed the challenge.
            log.warning(
                "hcaptcha rejected a well-formed solve token as "
                "invalid-input-response; if real users hit this repeatedly, "
                "the hCaptcha Secret Key likely belongs to a different "
                "account than the Site Key (or was rotated) — re-copy the "
                "account secret (ES_...) from the dashboard that owns the "
                "site key"
            )
    return success, errors


class _WebhookUpdateQueue:
    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        bot: Bot,
        secret_token: str,
        worker_count: int,
    ) -> None:
        self.dispatcher = dispatcher
        self.bot = bot
        self.secret_token = secret_token
        self.worker_count = max(1, int(worker_count))
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=self.worker_count * 4
        )
        self._workers: list[asyncio.Task[None]] = []
        self._start_lock = asyncio.Lock()
        self._stopped = False

    def verify_secret(self, request: web.Request) -> bool:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        return secrets.compare_digest(supplied, self.secret_token)

    async def handle_verified(self, request: web.Request) -> web.Response:
        try:
            update = await request.json(loads=self.bot.session.json_loads)
        except Exception:
            return web.Response(text="Invalid update", status=400)
        if not isinstance(update, dict):
            return web.Response(text="Invalid update", status=400)

        await self._ensure_workers()
        if self._stopped:
            return web.Response(text="Webhook disabled", status=503)
        try:
            self.queue.put_nowait(update)
        except asyncio.QueueFull:
            return web.Response(text="Webhook busy", status=503)
        return web.json_response({}, dumps=self.bot.session.json_dumps)

    async def _ensure_workers(self) -> None:
        if self._workers or self._stopped:
            return
        async with self._start_lock:
            if self._workers or self._stopped:
                return
            self._workers = [
                asyncio.create_task(
                    self._worker(),
                    name=f"telegram-webhook-worker-{index + 1}",
                )
                for index in range(self.worker_count)
            ]

    async def _worker(self) -> None:
        while True:
            update = await self.queue.get()
            try:
                result = await self.dispatcher.feed_raw_update(
                    bot=self.bot,
                    update=update,
                )
                if isinstance(result, TelegramMethod):
                    await self.dispatcher.silent_call_request(
                        bot=self.bot,
                        result=result,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram webhook update processing failed")
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._workers:
            await self.queue.join()
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()


class VerifyWebServer:
    """aiohttp server hosting the settings and human-verification Mini Apps."""

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_config: RuntimeConfigManager | None = None,
        webhook_dispatcher: Dispatcher | None = None,
        webhook_path: str = "",
        webhook_secret: str = "",
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self.runtime_config = runtime_config
        self.webhook_dispatcher = webhook_dispatcher
        self.webhook_path = webhook_path
        self.webhook_secret = webhook_secret
        self.webhook_route_error: str | None = None
        self._webhook_accepting_updates = False
        self._webhook_processor: _WebhookUpdateQueue | None = None
        self._webhook_request_tasks: set[asyncio.Task[Any]] = set()
        self._runner: web.AppRunner | None = None
        self._completed_verifications: dict[
            tuple[int, int], tuple[float, _VerificationFingerprint]
        ] = {}
        # Keep short-lived idempotence only after the row is consumed. The
        # fingerprint prevents a legacy/recovered database from treating a
        # different issuance that happens to reuse an ID as completed.
        self._active_verifications: dict[
            tuple[int, int], _ActiveVerification
        ] = {}

    @staticmethod
    def _verification_fingerprint(
        record: JoinVerification,
    ) -> _VerificationFingerprint:
        return (
            int(record.group_id),
            int(record.user_id),
            str(record.kind),
            normalize_verification_provider(record.provider),
            str(record.created_at),
            str(record.deadline_at),
            int(record.prompt_message_id or 0),
        )

    def _verification_completed(
        self,
        verification_id: int,
        user_id: int,
        fingerprint: _VerificationFingerprint,
    ) -> bool:
        completed_fingerprint = self._completed_verification_fingerprint(
            verification_id,
            user_id,
        )
        if completed_fingerprint is None:
            return False
        return completed_fingerprint == fingerprint

    def _completed_verification_fingerprint(
        self,
        verification_id: int,
        user_id: int,
    ) -> _VerificationFingerprint | None:
        if verification_id <= 0:
            return None
        self._prune_verification_caches()
        completed = self._completed_verifications.get(
            (int(verification_id), int(user_id))
        )
        return completed[1] if completed is not None else None

    async def _completed_member_is_unrestricted(
        self,
        fingerprint: _VerificationFingerprint,
        user_id: int,
    ) -> bool:
        try:
            member = await self.bot.get_chat_member(fingerprint[0], user_id)
        except Exception:
            return False
        status = str(getattr(member, "status", "") or "")
        return status in {"member", "administrator", "creator"} or (
            status == "restricted"
            and bool(getattr(member, "can_send_messages", False))
        )

    def _prune_verification_caches(self) -> None:
        now = time.monotonic()
        completed_cutoff = now - _COMPLETED_VERIFICATION_TTL_SECONDS
        for key, completed in list(self._completed_verifications.items()):
            if completed[0] < completed_cutoff:
                self._completed_verifications.pop(key, None)

        active_cutoff = now - _ACTIVE_VERIFICATION_TTL_SECONDS
        for key, active in list(self._active_verifications.items()):
            if active.started_at < active_cutoff:
                if not active.future.done():
                    active.future.set_result(False)
                self._active_verifications.pop(key, None)

        for cache in (self._completed_verifications, self._active_verifications):
            overflow = len(cache) - _VERIFICATION_CACHE_MAX_ENTRIES
            if overflow <= 0:
                continue
            oldest = sorted(
                cache.items(),
                key=lambda item: (
                    item[1][0]
                    if isinstance(item[1], tuple)
                    else item[1].started_at
                ),
            )[:overflow]
            for key, value in oldest:
                if isinstance(value, _ActiveVerification) and not value.future.done():
                    value.future.set_result(False)
                cache.pop(key, None)

    def _active_verification(
        self,
        verification_id: int,
        user_id: int,
    ) -> _ActiveVerification | None:
        if verification_id <= 0:
            return None
        self._prune_verification_caches()
        return self._active_verifications.get(
            (int(verification_id), int(user_id))
        )

    def _enter_verification_active(
        self,
        verification_id: int,
        user_id: int,
        fingerprint: _VerificationFingerprint,
    ) -> tuple[_ActiveVerification, bool]:
        self._prune_verification_caches()
        key = (int(verification_id), int(user_id))
        current = self._active_verifications.get(key)
        if current is not None and current.fingerprint == fingerprint:
            current.participants += 1
            return current, True

        if current is not None and not current.future.done():
            current.future.set_result(False)
        active = _ActiveVerification(
            started_at=time.monotonic(),
            fingerprint=fingerprint,
            future=asyncio.get_running_loop().create_future(),
        )
        self._active_verifications[key] = active
        self._prune_verification_caches()
        return active, False

    def _leave_verification_active(
        self,
        verification_id: int,
        user_id: int,
        active: _ActiveVerification,
    ) -> None:
        key = (int(verification_id), int(user_id))
        current = self._active_verifications.get(key)
        if current is not active:
            return
        current.participants -= 1
        if current.participants <= 0:
            if not current.future.done():
                current.future.set_result(False)
            self._active_verifications.pop(key, None)

    def _fail_verification_active(
        self,
        verification_id: int,
        user_id: int,
        fingerprint: _VerificationFingerprint,
    ) -> None:
        key = (int(verification_id), int(user_id))
        active = self._active_verifications.get(key)
        if active is not None and active.fingerprint == fingerprint:
            if not active.future.done():
                active.future.set_result(False)
            self._active_verifications.pop(key, None)

    def _mark_verification_completed(
        self,
        verification_id: int,
        user_id: int,
        fingerprint: _VerificationFingerprint,
    ) -> None:
        if verification_id > 0:
            self._prune_verification_caches()
            self._completed_verifications[(int(verification_id), int(user_id))] = (
                time.monotonic(),
                fingerprint,
            )
            active = self._active_verifications.get(
                (int(verification_id), int(user_id))
            )
            if active is not None and active.fingerprint == fingerprint:
                if not active.future.done():
                    active.future.set_result(True)
                self._active_verifications.pop(
                    (int(verification_id), int(user_id)),
                    None,
                )
            self._prune_verification_caches()

    async def _wait_for_completed_verification(
        self,
        active: _ActiveVerification,
    ) -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    asyncio.shield(active.future),
                    timeout=_VERIFICATION_WAIT_TIMEOUT_SECONDS,
                )
            )
        except TimeoutError:
            return False

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/verify", self.handle_challenge_page)
        app.router.add_post("/verify", self.handle_challenge_submit)
        app.router.add_get("/healthz", self.handle_health)
        if self.runtime_config is not None:
            app.router.add_get("/settings", self.handle_settings_page)
            app.router.add_static(
                "/settings-assets/",
                _SETTINGS_STATIC_DIR,
                name="settings-assets",
            )
            register_settings_routes(
                app,
                bot=self.bot,
                bot_token=self.bot.token,
                settings=self.settings,
                manager=self.runtime_config,
                session_factory=self.session_factory,
            )
        self.webhook_route_error = None
        if self.webhook_dispatcher is not None and self.webhook_path:
            try:
                processor = _WebhookUpdateQueue(
                    dispatcher=self.webhook_dispatcher,
                    bot=self.bot,
                    secret_token=self.webhook_secret,
                    worker_count=WEBHOOK_MAX_CONCURRENT_UPDATES,
                )

                async def handle_webhook(request: web.Request) -> web.Response:
                    if not processor.verify_secret(request):
                        return web.Response(text="Unauthorized", status=401)
                    if not self._webhook_accepting_updates:
                        return web.Response(text="Webhook disabled", status=503)
                    task = asyncio.current_task()
                    if task is not None:
                        self._webhook_request_tasks.add(task)
                    try:
                        return await processor.handle_verified(request)
                    finally:
                        if task is not None:
                            self._webhook_request_tasks.discard(task)

                async def close_webhook_handler(_app: web.Application) -> None:
                    await self.disable_webhook_route()
                    await self.bot.session.close()

                app.router.add_post(self.webhook_path, handle_webhook)
                app.on_shutdown.append(close_webhook_handler)
                self._webhook_processor = processor
            except Exception as exc:
                self.webhook_route_error = f"Webhook HTTP 路由注册失败：{exc}"
                log.error(
                    "%s；该路由已禁用，将自动降级为轮询。",
                    self.webhook_route_error,
                    exc_info=True,
                )
        return app

    def enable_webhook_route(self) -> None:
        self._webhook_accepting_updates = True

    async def disable_webhook_route(self) -> None:
        self._webhook_accepting_updates = False
        request_tasks = [
            task
            for task in self._webhook_request_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        if request_tasks:
            await asyncio.gather(*request_tasks, return_exceptions=True)
        if self._webhook_processor is not None:
            await self._webhook_processor.stop()

    async def start(self) -> None:
        app = self.build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            host=self.settings.miniapp_listen_host,
            port=self.settings.miniapp_listen_port,
        )
        await site.start()
        log.info(
            "Mini App web server listening on %s:%s",
            self.settings.miniapp_listen_host,
            self.settings.miniapp_listen_port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "config_revision": self.runtime_config.revision
                if self.runtime_config is not None
                else None,
            }
        )

    async def handle_settings_page(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            _SETTINGS_STATIC_DIR / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _SETTINGS_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_challenge_page(self, request: web.Request) -> web.Response:
        # The page is static: user identity arrives with the POSTed initData,
        # so an unauthenticated GET reveals nothing but the public site key.
        raw_provider = request.query.get("provider")
        if raw_provider and raw_provider.strip().lower() not in {"turnstile", "hcaptcha"}:
            return web.json_response(
                {"ok": False, "error": "不支持的验证服务"}, status=400
            )
        provider = normalize_verification_provider(
            raw_provider,
            default=verification_provider(self.settings),
        )
        raw_verification_id = request.query.get("verification_id", "")
        try:
            verification_id = int(raw_verification_id) if raw_verification_id else 0
        except ValueError:
            return web.json_response(
                {"ok": False, "error": "验证记录参数无效"}, status=400
            )
        if verification_id < 0:
            return web.json_response(
                {"ok": False, "error": "验证记录参数无效"}, status=400
            )
        site_key, _secret_key = verification_keys_for_provider(self.settings, provider)
        if not turnstile_verification_configured(self.settings, provider):
            return web.json_response(
                {"ok": False, "error": "验证服务暂时不可用，请联系管理员"}, status=503
            )
        if provider == "hcaptcha":
            script_url = "https://js.hcaptcha.com/1/api.js"
            widget_html = (
                '<div class="h-captcha" data-size="compact" data-sitekey="{site_key}" '
                'data-callback="onChallengeSuccess" data-error-callback="onChallengeError" '
                'data-expired-callback="onChallengeExpired"></div>'
            ).format(site_key=html.escape(site_key))
        else:
            script_url = "https://challenges.cloudflare.com/turnstile/v0/api.js"
            widget_html = (
                '<div class="cf-turnstile" data-size="flexible" data-sitekey="{site_key}" '
                'data-callback="onChallengeSuccess" data-error-callback="onChallengeError" '
                'data-expired-callback="onChallengeExpired"></div>'
            ).format(site_key=html.escape(site_key))
        page = _PAGE_TEMPLATE.format(
            provider=provider,
            config_tag=_verification_config_tag(self.settings, provider),
            verification_id=verification_id,
            script_url=script_url,
            widget_html=widget_html,
            nonce=(nonce := secrets.token_urlsafe(18)),
        )
        return web.Response(
            text=page,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _CHALLENGE_CSP_BASE.format(
                    nonce=f"'nonce-{nonce}'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_challenge_submit(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        challenge_token = str(
            body.get("challenge_token") or body.get("turnstile_token") or ""
        )
        page_provider = str(body.get("provider") or "turnstile").strip().lower()
        page_config_tag = str(body.get("config_tag") or "").strip()
        try:
            verification_id = int(body.get("verification_id") or 0)
        except (TypeError, ValueError):
            verification_id = -1
        init_data = str(body.get("init_data") or "")
        if not challenge_token or not init_data or verification_id < 0:
            return web.json_response({"ok": False, "error": "缺少验证参数"}, status=400)

        # Telegram signs initData with HMAC(bot_token); a valid signature
        # proves the request comes from this bot's Mini App session and pins
        # the user identity.
        try:
            web_app_data = safe_parse_webapp_init_data(
                token=self.bot.token, init_data=init_data
            )
        except ValueError:
            log.info("verification rejected | reason=bad_init_data_signature")
            return web.json_response(
                {"ok": False, "error": "会话校验失败，请从 Telegram 重新打开"}, status=403
            )
        web_user = web_app_data.user
        if web_user is None:
            return web.json_response(
                {"ok": False, "error": "会话校验失败，请从 Telegram 重新打开"}, status=403
            )
        user_id = int(web_user.id)

        async with self.session_factory() as session:
            record = None
            if verification_id:
                record = await get_pending_verification_by_id_for_user(
                    session,
                    verification_id,
                    user_id,
                )
            if record is None and verification_id:
                # A kick/rejoin or requeue replaces the row (new id) while an
                # already-open page still pins the old id. Identity comes from
                # the signed initData, so fall back to the member's current
                # pending record instead of discarding a genuine solve — but
                # only when it is unambiguous: the page carries no group id, so
                # with records pending in several groups the solve must not be
                # redirected to a group the page never belonged to.
                record = await get_sole_pending_verification_for_user(
                    session, user_id
                )
            elif record is None:
                record = await get_pending_verification_for_user(session, user_id)
            locally_banned = bool(
                record
                and await session.scalar(
                    select(UserWarning.id).where(
                        UserWarning.group_id == record.group_id,
                        UserWarning.user_id == user_id,
                        UserWarning.is_banned == True,  # noqa: E712
                    )
                )
            )
        if record is None:
            completed_fingerprint = self._completed_verification_fingerprint(
                verification_id,
                user_id,
            )
            if (
                completed_fingerprint is not None
                and await self._completed_member_is_unrestricted(
                    completed_fingerprint,
                    user_id,
                )
            ):
                return web.json_response({"ok": True})
            active = self._active_verification(
                verification_id,
                user_id,
            )
            if (
                active is not None
                and await self._wait_for_completed_verification(active)
            ):
                return web.json_response({"ok": True})
            return web.json_response(
                {"ok": False, "error": "没有有效的待处理验证，请联系群管理员"},
                status=404,
            )
        if verification_deadline_passed(record.deadline_at):
            return web.json_response(
                {"ok": False, "error": "没有有效的待处理验证，请联系群管理员"},
                status=404,
            )
        if locally_banned:
            return web.json_response(
                {"ok": False, "error": "该用户已被本群管理员封禁，请联系管理员"},
                status=403,
            )

        record_fingerprint = self._verification_fingerprint(record)
        verification_id = int(record.id)
        provider = normalize_verification_provider(record.provider)
        if page_provider != provider:
            return web.json_response(
                {"ok": False, "error": "验证配置已切换，请关闭页面后重新打开"},
                status=409,
            )
        current_config_tag = _verification_config_tag(self.settings, provider)
        if page_config_tag and page_config_tag != current_config_tag:
            return web.json_response(
                {"ok": False, "error": "验证配置已更新，请重新打开验证页面"},
                status=409,
            )
        if not turnstile_verification_configured(self.settings, provider):
            return web.json_response(
                {"ok": False, "error": "验证服务暂时不可用，请联系管理员"}, status=503
            )
        site_key, secret_key = verification_keys_for_provider(self.settings, provider)
        active, joined_existing_active = self._enter_verification_active(
            verification_id,
            user_id,
            record_fingerprint,
        )
        active_released = False

        def release_active() -> None:
            nonlocal active_released
            if active_released:
                return
            self._leave_verification_active(
                verification_id,
                user_id,
                active,
            )
            active_released = True

        request_task = asyncio.current_task()
        if request_task is not None:
            request_task.add_done_callback(lambda _task: release_active())

        remote_ip = _client_public_ip(request)
        if provider == "hcaptcha":
            passed, verification_errors = await verify_hcaptcha_token(
                secret_key=secret_key,
                hcaptcha_token=challenge_token,
                remote_ip=remote_ip,
                site_key=site_key,
            )
        else:
            passed, verification_errors = await verify_turnstile_token(
                secret_key=secret_key,
                turnstile_token=challenge_token,
                remote_ip=remote_ip,
            )
        if _verification_config_tag(self.settings, provider) != current_config_tag:
            release_active()
            return web.json_response(
                {"ok": False, "error": "验证配置已更新，请重新打开验证页面"},
                status=409,
            )
        if not passed:
            replay_error = provider == "hcaptcha" and bool(
                _HCAPTCHA_REPLAY_ERRORS.intersection(verification_errors)
            )
            if replay_error and joined_existing_active:
                release_active()
            if replay_error and joined_existing_active and await self._wait_for_completed_verification(
                active
            ):
                # Fingerprints are second-granular, so a reissued record can
                # collide with an older completion. The winning submission
                # always consumes the row, so a still-pending row proves the
                # completion belongs to a previous issuance, not this one.
                async with self.session_factory() as session:
                    still_pending = await get_pending_verification_by_id_for_user(
                        session,
                        verification_id,
                        user_id,
                    )
                if still_pending is None:
                    return web.json_response({"ok": True})
            configuration_error_codes = (
                _HCAPTCHA_CONFIGURATION_ERRORS
                if provider == "hcaptcha"
                else _TURNSTILE_CONFIGURATION_ERRORS
            )
            configuration_errors = configuration_error_codes.intersection(
                verification_errors
            )
            if configuration_errors:
                release_active()
                mark_turnstile_configuration_unavailable(
                    self.settings,
                    reason=f"{provider} Secret Key 无效",
                    provider=provider,
                )
                async with self.session_factory() as session:
                    await extend_pending_verification_deadlines(
                        session,
                        settings=self.settings,
                        provider=provider,
                    )
                    await session.commit()
                return web.json_response(
                    {
                        "ok": False,
                        "error": "验证服务配置错误：Secret Key 无效，请联系管理员",
                    },
                    status=503,
                )
            if _TURNSTILE_SERVICE_ERRORS.intersection(verification_errors):
                release_active()
                return web.json_response(
                    {
                        "ok": False,
                        "error": "验证服务暂时不可用，请稍后重试",
                    },
                    status=503,
                )
            release_active()
            if _CHALLENGE_TOKEN_RETRY_ERRORS.intersection(verification_errors):
                # The human did solve the challenge; only the one-time token
                # was too old or already redeemed by the time it reached
                # siteverify. Distinguish this from a failed challenge so the
                # member knows a fresh attempt will work.
                return web.json_response(
                    {
                        "ok": False,
                        "error": "验证凭证已过期，请重新完成一次验证",
                    },
                    status=403,
                )
            return web.json_response(
                {"ok": False, "error": "人机验证未通过，请重试"},
                status=403,
            )

        clear_turnstile_configuration_unavailable(self.settings, provider=provider)

        async with self.session_factory() as session:
            # Re-check under a fresh session: the sweeper may have kicked the
            # user (record gone) while Turnstile was being validated.
            current = await get_pending_verification_by_id_for_user(
                session,
                int(record.id),
                user_id,
            )
            now = now_shanghai_naive()
            # A deadline moved backwards means the row was replaced by a new
            # issuance; moved forward is a benign /start re-arm mid-solve.
            if (
                current is None
                or current.id != record.id
                or normalize_verification_provider(current.provider) != provider
                or current.deadline_at < record.deadline_at
                or verification_deadline_passed(current.deadline_at, now=now)
            ):
                release_active()
                if await self._wait_for_completed_verification(active):
                    return web.json_response({"ok": True})
                return web.json_response(
                    {"ok": False, "error": "验证已过期或失效，请联系群管理员"},
                    status=404,
                )
            if await is_globally_banned(session, user_id):
                await delete_join_verification(session, current.group_id, user_id)
                await session.commit()
                self._fail_verification_active(
                    verification_id,
                    user_id,
                    record_fingerprint,
                )
                active_released = True
                return web.json_response(
                    {"ok": False, "error": "该账号已被管理员封禁，请联系管理员"},
                    status=403,
                )
            if await session.scalar(
                select(UserWarning.id).where(
                    UserWarning.group_id == current.group_id,
                    UserWarning.user_id == user_id,
                    UserWarning.is_banned == True,  # noqa: E712
                )
            ):
                self._fail_verification_active(
                    verification_id,
                    user_id,
                    record_fingerprint,
                )
                active_released = True
                return web.json_response(
                    {"ok": False, "error": "该用户已被本群管理员封禁，请联系管理员"},
                    status=403,
                )

            claimed = await claim_join_verification(
                session,
                verification_id=current.id,
                deadline_at=current.deadline_at,
                kind=current.kind,
                now=now,
                expired=False,
            )
            if not claimed:
                await session.rollback()
                release_active()
                if await self._wait_for_completed_verification(active):
                    return web.json_response({"ok": True})
                return web.json_response(
                    {"ok": False, "error": "验证已失效，请重新打开验证入口"},
                    status=404,
                )

            group_id = int(current.group_id)
            prompt_message_id = int(current.prompt_message_id or 0)
            display_name = (current.display_name or "").strip()
            kind = current.kind
            reason = current.reason or ""
            # Commit before Telegram calls so a failed restore cannot roll the
            # pass back and let the sweeper kick a verified member.
            await session.commit()

        terminal_task = asyncio.create_task(
            self._finish_claimed_verification(
                verification_id=verification_id,
                user_id=user_id,
                fingerprint=record_fingerprint,
                group_id=group_id,
                prompt_message_id=prompt_message_id,
                display_name=display_name,
                kind=kind,
                reason=reason,
                provider=provider,
            )
        )
        try:
            response = await asyncio.shield(terminal_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(terminal_task)
            except Exception:
                await asyncio.shield(
                    self._recover_claimed_verification(
                        verification_id=verification_id,
                        user_id=user_id,
                        fingerprint=record_fingerprint,
                        group_id=group_id,
                        prompt_message_id=prompt_message_id,
                        display_name=display_name,
                        kind=kind,
                        reason=reason,
                        provider=provider,
                    )
                )
            raise
        except Exception:
            await asyncio.shield(
                self._recover_claimed_verification(
                    verification_id=verification_id,
                    user_id=user_id,
                    fingerprint=record_fingerprint,
                    group_id=group_id,
                    prompt_message_id=prompt_message_id,
                    display_name=display_name,
                    kind=kind,
                    reason=reason,
                    provider=provider,
                )
            )
            raise
        active_released = True
        return response

    async def _finish_claimed_verification(
        self,
        *,
        verification_id: int,
        user_id: int,
        fingerprint: _VerificationFingerprint,
        group_id: int,
        prompt_message_id: int,
        display_name: str,
        kind: str,
        reason: str,
        provider: str,
    ) -> web.Response:
        restored = await restore_member_permissions(self.bot, group_id, user_id)
        log.info(
            "verification passed | kind=%s group=%s user=%s restored=%s",
            kind,
            group_id,
            user_id,
            restored,
        )
        if not restored:
            timeout_seconds = verification_timeout_seconds_for_kind(
                self.settings, kind
            )
            async with self.session_factory() as session:
                await upsert_join_verification(
                    session,
                    group_id=group_id,
                    user_id=user_id,
                    deadline_at=now_shanghai_naive()
                    + timedelta(seconds=max(60, int(timeout_seconds))),
                    kind=kind,
                    reason=reason,
                    display_name=display_name,
                    prompt_message_id=prompt_message_id,
                    provider=provider,
                )
                await session.commit()
                retry_record = await get_join_verification(
                    session,
                    group_id,
                    user_id,
                )
                retry_verification_id = int(retry_record.id) if retry_record else 0
            await self._announce_pass(
                group_id=group_id,
                user_id=user_id,
                display_name=display_name,
                prompt_message_id=prompt_message_id,
                kind=kind,
                restored=False,
            )
            self._fail_verification_active(
                verification_id,
                user_id,
                fingerprint,
            )
            return web.json_response(
                {
                    "ok": False,
                    "error": "验证已通过，但 Telegram 放行暂时失败；验证入口已保留，请稍后重试",
                    "verification_id": retry_verification_id,
                },
                status=502,
            )
        self._mark_verification_completed(
            verification_id,
            user_id,
            fingerprint,
        )
        if kind == VERIFICATION_KIND_PATROL:
            # Whitelist the passing profile so the next patrol run and the
            # on-message screening do not immediately re-flag it.
            from bot.services.patrol import acknowledge_patrol_pass

            try:
                await acknowledge_patrol_pass(
                    self.bot,
                    self.session_factory,
                    group_id=group_id,
                    user_id=user_id,
                    fetch_bio=bool(
                        getattr(self.settings, "patrol_fetch_bio", True)
                    ),
                )
            except Exception:
                log.exception(
                    "patrol pass acknowledgement failed | group=%s user=%s",
                    group_id,
                    user_id,
                )
        await self._announce_pass(
            group_id=group_id,
            user_id=user_id,
            display_name=display_name,
            prompt_message_id=prompt_message_id,
            kind=kind,
            restored=True,
        )
        return web.json_response({"ok": True})

    async def _recover_claimed_verification(
        self,
        *,
        verification_id: int,
        user_id: int,
        fingerprint: _VerificationFingerprint,
        group_id: int,
        prompt_message_id: int,
        display_name: str,
        kind: str,
        reason: str,
        provider: str,
    ) -> None:
        try:
            restored = await self._completed_member_is_unrestricted(
                fingerprint,
                user_id,
            ) or await restore_member_permissions(self.bot, group_id, user_id)
            if restored:
                self._mark_verification_completed(
                    verification_id,
                    user_id,
                    fingerprint,
                )
                return

            timeout_seconds = verification_timeout_seconds_for_kind(
                self.settings, kind
            )
            async with self.session_factory() as session:
                await upsert_join_verification(
                    session,
                    group_id=group_id,
                    user_id=user_id,
                    deadline_at=now_shanghai_naive()
                    + timedelta(seconds=max(60, int(timeout_seconds))),
                    kind=kind,
                    reason=reason,
                    display_name=display_name,
                    prompt_message_id=prompt_message_id,
                    provider=provider,
                )
                await session.commit()
        except Exception:
            log.exception(
                "verification terminal recovery failed | kind=%s group=%s user=%s",
                kind,
                group_id,
                user_id,
            )
        finally:
            self._fail_verification_active(
                verification_id,
                user_id,
                fingerprint,
            )

    async def _announce_pass(
        self,
        *,
        group_id: int,
        user_id: int,
        display_name: str,
        prompt_message_id: int,
        kind: str,
        restored: bool,
    ) -> None:
        shown = html.escape(display_name or str(user_id))
        if kind == VERIFICATION_KIND_MODERATION:
            passed_text = f"✅ <b>{shown}</b> 已通过消息审查验证，发言权限已恢复。"
        elif kind == VERIFICATION_KIND_PATROL:
            passed_text = f"✅ <b>{shown}</b> 已通过资料巡检质询，发言权限已恢复。"
        else:
            passed_text = f"✅ <b>{shown}</b> 已通过真人验证，欢迎加入！"
        text = passed_text + (
            "" if restored else "\n⚠️ 权限恢复失败，请管理员手动解除禁言。"
        )
        # A patrol prompt is shared by several violators; editing it would
        # remove the warning and its challenge button for the others.
        if prompt_message_id and kind != VERIFICATION_KIND_PATROL:
            try:
                await self.bot.edit_message_text(
                    chat_id=group_id,
                    message_id=prompt_message_id,
                    text=text,
                    parse_mode="HTML",
                )
                return
            except Exception:
                log.debug(
                    "verification pass edit failed | group=%s message=%s",
                    group_id,
                    prompt_message_id,
                )
        try:
            await self.bot.send_message(group_id, text, parse_mode="HTML")
        except Exception:
            log.debug("verification pass notice failed | group=%s", group_id)
