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
import json
import logging
import secrets
import time
from collections import OrderedDict, deque
from contextvars import Context
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiogram.utils.web_app import safe_parse_webapp_init_data
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import JoinVerification, UserWarning, WebhookInboxUpdate
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    COMBINED_VERIFICATION_PROVIDER,
    SHARED_PROMPT_VERIFICATION_KINDS,
    TERMINAL_LEASE_SECONDS,
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_PATROL,
    VERIFICATION_KIND_RAID,
    VERIFICATION_PROVIDERS,
    VERIFICATION_STATUS_RELEASING,
    claim_join_verification,
    clear_turnstile_configuration_unavailable,
    complete_leased_join_verification,
    delete_join_verification,
    delete_verification_prompt,
    extend_pending_verification_deadlines,
    get_pending_verification_by_id_for_user,
    get_pending_verification_for_user,
    get_sole_pending_verification_for_user,
    mark_turnstile_configuration_unavailable,
    normalize_verification_provider,
    renew_join_verification_lease,
    restore_member_permissions,
    turnstile_verification_configured,
    verification_deadline_passed,
    verification_keys_for_provider,
    verification_provider,
    verification_subproviders,
)
from bot.services.request_priority import (
    ExecutionPriority,
    execution_priority_scope,
    privileged_request_scope,
)
from bot.services.resource_health import (
    register_resource_health_provider,
    resource_health_snapshot,
    unregister_resource_health_provider,
)
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    bind_update_completion,
    reset_update_completion,
)
from bot.services.update_delivery import (
    WEBHOOK_AUTH_CONCURRENT_UPDATES,
    WEBHOOK_AUTH_QUEUE_CAPACITY,
    WEBHOOK_CRITICAL_CONCURRENT_UPDATES,
    WEBHOOK_CRITICAL_QUEUE_CAPACITY,
    WEBHOOK_MAX_CONCURRENT_UPDATES,
    WEBHOOK_SECURITY_CONCURRENT_UPDATES,
    WEBHOOK_SECURITY_QUEUE_CAPACITY,
    TELEGRAM_AUTH_CANDIDATE_BURST,
    mark_privileged_operator,
    privileged_operator_is_trusted,
    telegram_update_chat_id,
    telegram_update_is_auth_candidate,
    telegram_update_sender_id,
    telegram_update_priority,
)
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
)
from bot.utils.timezone import now_shanghai_naive
from bot.web.auth import (
    MAX_INIT_DATA_AGE_SECONDS,
    MAX_INIT_DATA_FUTURE_SECONDS,
    _auth_timestamp,
)
from bot.web.settings_api import flush_member_identity_tasks, register_settings_routes

log = logging.getLogger(__name__)


async def _read_siteverify_json(response: Any) -> Any:
    """Read a provider response without allowing an unbounded error body."""
    content = getattr(response, "content", None)
    read = getattr(content, "read", None)
    if not callable(read):
        return await response.json(content_type=None)
    content_length = getattr(response, "content_length", None)
    if content_length is not None and int(content_length) > 64 * 1024:
        raise ValueError("siteverify response too large")
    raw = await read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("siteverify response too large")
    return json.loads(raw.decode("utf-8", errors="strict"))

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
_VERIFICATION_MAX_CHALLENGE_TOKEN_CHARS = 8 * 1024
_VERIFICATION_MAX_INIT_DATA_CHARS = 16 * 1024
_VERIFICATION_MAX_CONFIG_TAG_CHARS = 128
_VERIFICATION_MAX_PROVIDER_CHARS = 32
_VERIFICATION_MAX_ID_CHARS = 20
_VERIFICATION_MAX_CONCURRENT_UPSTREAM = 8
_VERIFICATION_RATE_WINDOW_SECONDS = 60.0
_VERIFICATION_USER_RATE_LIMIT = 8
_VERIFICATION_IP_RATE_LIMIT = 30
_VERIFICATION_RATE_CACHE_MAX_ENTRIES = 4096
_WEBHOOK_REQUEST_BODY_TIMEOUT_SECONDS = 5.0
_PUBLIC_JSON_BODY_TIMEOUT_SECONDS = 5.0
_WEB_MAX_REQUEST_BYTES = 1024 * 1024
# A group-message handler can defer completion to its bounded reply worker. Its
# configured budget is at most 120s, so the delivery deadline must cover that
# work plus debounce and the synchronous moderation/indexing prefix. The HTTP
# request may disconnect sooner; the shielded shared result remains available
# to Telegram's retry and still prevents duplicate execution.
_WEBHOOK_UPDATE_TIMEOUT_SECONDS = 150.0
# Privileged updates receive a fresh execution budget when a reserved worker
# takes them.  Queue wait must not consume the time needed to acknowledge an
# administrator callback or enforce a membership change.  Long batch work is
# handed off by the handlers and therefore should not need the ordinary 150s
# end-to-end queue budget.
_WEBHOOK_CRITICAL_UPDATE_TIMEOUT_SECONDS = 60.0
_WEBHOOK_SECURITY_UPDATE_TIMEOUT_SECONDS = 120.0
_WEBHOOK_AUTH_UPDATE_TIMEOUT_SECONDS = 45.0
_WEBHOOK_CRITICAL_QUEUE_WARN_SECONDS = 2.0
_WEBHOOK_SECURITY_QUEUE_WARN_SECONDS = 5.0
_WEBHOOK_UNTRUSTED_CRITICAL_WINDOW_SECONDS = 10.0
_WEBHOOK_UNTRUSTED_CRITICAL_BURST = 6
_WEBHOOK_UNTRUSTED_AUTH_WINDOW_SECONDS = 10.0
_WEBHOOK_UNTRUSTED_AUTH_BURST = TELEGRAM_AUTH_CANDIDATE_BURST
_WEBHOOK_ORPHAN_FATAL_AGE_SECONDS = 120.0
_WEBHOOK_LATE_FINALIZER_FATAL_AGE_SECONDS = 300.0
_WEBHOOK_HTTP_RESPONSE_TIMEOUT_SECONDS = 155.0
_WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS = 2.0
_WEBHOOK_DRAIN_TIMEOUT_SECONDS = 15.0
_WEBHOOK_REQUEST_DRAIN_TIMEOUT_SECONDS = 3.0
_WEBHOOK_WORKER_STOP_TIMEOUT_SECONDS = 3.0
_WEBHOOK_FAILURE_THRESHOLD = 3
_WEBHOOK_DEDUP_TTL_SECONDS = 60.0 * 60.0
_WEBHOOK_DEDUP_MAX_UPDATES = 4096
_WEBHOOK_INBOX_LEASE_SECONDS = 5 * 60
_WEBHOOK_INBOX_RECOVERY_INTERVAL_SECONDS = 2.0
_WEBHOOK_INBOX_RETENTION_SECONDS = 7 * 24 * 60 * 60
_WEBHOOK_INBOX_DLQ_RETENTION_SECONDS = 30 * 24 * 60 * 60
_WEBHOOK_INBOX_RECOVERY_BATCH = 64
_WEBHOOK_INBOX_MAX_ATTEMPTS = 12
_WEBHOOK_INBOX_RETRY_BASE_SECONDS = 2.0
_WEBHOOK_INBOX_RETRY_MAX_SECONDS = 5 * 60.0
_WEBHOOK_INBOX_CLEANUP_INTERVAL_SECONDS = 60.0
_WEBHOOK_INBOX_CLEANUP_BATCH = 256
_WEB_SERVER_SHUTDOWN_TIMEOUT_SECONDS = 25.0
_PUBLIC_BODY_READ_TASK_LIMIT = 64
_SETTINGS_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
_PUBLIC_BODY_READ_TASKS: set[asyncio.Task[Any]] = set()

_VerificationFingerprint = tuple[int, int, str, str, str, str, int]


def _observe_public_body_task(task: asyncio.Task[Any]) -> None:
    _PUBLIC_BODY_READ_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def _read_json_body_hard(
    request: web.Request,
    *,
    timeout_seconds: float,
    loads: Any | None = None,
) -> Any:
    active = sum(not task.done() for task in _PUBLIC_BODY_READ_TASKS)
    if active >= _PUBLIC_BODY_READ_TASK_LIMIT:
        raise RuntimeError("public request body reader is saturated")
    awaitable = request.json(loads=loads) if loads is not None else request.json()
    task = asyncio.create_task(awaitable, name="public-json-body")
    _PUBLIC_BODY_READ_TASKS.add(task)
    task.add_done_callback(_observe_public_body_task)
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=max(0.01, float(timeout_seconds)),
        )
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    raise TimeoutError("request body deadline exceeded")


async def _flush_public_body_tasks(*, timeout_seconds: float = 1.0) -> None:
    tasks = {task for task in _PUBLIC_BODY_READ_TASKS if not task.done()}
    for task in tasks:
        task.cancel()
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        for task in done:
            _observe_public_body_task(task)
        for task in pending:
            task.add_done_callback(_observe_public_body_task)


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
    peer = str(getattr(request, "remote", "") or "").strip()
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return ""
    if peer_address.is_global:
        return peer_address.compressed
    if peer_address.is_loopback or peer_address.is_private:
        headers = getattr(request, "headers", {}) or {}
        return _public_ip(headers.get("CF-Connecting-IP"))
    return ""


def _verification_config_tag(
    settings: Settings,
    provider: str,
) -> str:
    """Opaque public tag that detects key rotation between GET and POST."""
    parts = [normalize_verification_provider(provider)]
    for subprovider in verification_subproviders(provider):
        parts.extend(verification_keys_for_provider(settings, subprovider))
    parts.append(settings.join_verification_public_base_url.strip().rstrip("/"))
    payload = "\x00".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_SETTINGS_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors https://web.telegram.org https://telegram.org https://*.telegram.org"
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
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors https://web.telegram.org https://telegram.org https://*.telegram.org"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>真人验证</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
{script_tags}
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
  .challenge-steps {{ display: flex; flex-direction: column; width: 100%; }}
  .challenge-step {{ margin-top: 16px; transition: opacity .2s ease; }}
  .challenge-step:first-child {{ margin-top: 0; }}
  .challenge-step.locked {{ opacity: .4; pointer-events: none; }}
  .challenge-step-title {{ font-size: 13px; font-weight: 600; margin: 0 0 8px; text-align: left; }}
  .challenge-step-widget {{ display: flex; max-width: 100%; justify-content: center; overflow: visible; }}
  .challenge-step-widget > * {{ max-width: 100%; }}
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
  <p>{intro_text}</p>
  <div class="challenge-widget">{widget_html}</div>
  <div id="status"></div>
</div>
<script nonce="{nonce}">
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) {{ tg.ready(); tg.expand(); }}

const provider = "{provider}";
const combined = provider === "turnstile_hcaptcha";
const configTag = "{config_tag}";
let verificationId = {verification_id};
let challengeSubmitting = false;
let turnstileToken = "";
let hcaptchaToken = "";

function setStatus(text, cls) {{
  const status = document.getElementById("status");
  status.textContent = text;
  status.className = cls || "";
}}

function setHcaptchaStepLocked(locked) {{
  const step = document.getElementById("step-hcaptcha");
  if (step) step.classList.toggle("locked", locked);
}}

function resetChallenge() {{
  turnstileToken = "";
  hcaptchaToken = "";
  if (combined) setHcaptchaStepLocked(true);
  if ((combined || provider === "hcaptcha") && window.hcaptcha) window.hcaptcha.reset();
  if ((combined || provider === "turnstile") && window.turnstile) window.turnstile.reset();
}}

function onChallengeError() {{
  if (challengeSubmitting) return;
  setStatus("❌ 验证组件暂时不可用，请稍后重试。", "err");
  setTimeout(resetChallenge, 1000);
}}

function onChallengeExpired() {{
  if (challengeSubmitting) return;
  setStatus("验证已过期，请重新完成验证。", "err");
  resetChallenge();
}}

async function submitVerification(tokens) {{
  if (challengeSubmitting) return;
  challengeSubmitting = true;
  if (!tg || !tg.initData) {{
    challengeSubmitting = false;
    setStatus("❌ 请在 Telegram 内打开本页面。", "err");
    return;
  }}
  setStatus("验证中…", "");
  try {{
    const resp = await fetch("/verify", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(Object.assign({{provider, config_tag: configTag, verification_id: verificationId, init_data: tg.initData}}, tokens)),
    }});
    const data = await resp.json();
    if (resp.ok && data.ok) {{
      setStatus("✅ 验证通过，群内权限已恢复。", "ok");
      setTimeout(() => tg.close(), 1500);
    }} else {{
      challengeSubmitting = false;
      if (Number.isInteger(data.verification_id) && data.verification_id > 0) {{
        verificationId = data.verification_id;
      }}
      let message = "❌ " + (data.error || "验证失败，请重试。");
      if (combined) message += " 请从第 1 步重新开始。";
      setStatus(message, "err");
      resetChallenge();
    }}
  }} catch (e) {{
    challengeSubmitting = false;
    setStatus("❌ 网络错误，请稍后重试。", "err");
    resetChallenge();
  }}
}}

function onChallengeSuccess(token) {{
  submitVerification({{challenge_token: token}});
}}

function maybeSubmitCombined() {{
  if (turnstileToken && hcaptchaToken) {{
    submitVerification({{turnstile_token: turnstileToken, hcaptcha_token: hcaptchaToken}});
  }}
}}

function onTurnstileSuccess(token) {{
  turnstileToken = token;
  setHcaptchaStepLocked(false);
  if (hcaptchaToken) {{ maybeSubmitCombined(); return; }}
  setStatus("✅ 第 1 步已完成，请继续完成第 2 步 hCaptcha 验证。", "ok");
}}

function onTurnstileExpired() {{
  turnstileToken = "";
  if (challengeSubmitting) return;
  setStatus("第 1 步 Turnstile 验证已过期，请重新完成。", "err");
}}

function onHcaptchaSuccess(token) {{
  hcaptchaToken = token;
  if (turnstileToken) {{ maybeSubmitCombined(); return; }}
  setStatus("请先完成第 1 步 Turnstile 验证。", "err");
}}

function onHcaptchaExpired() {{
  hcaptchaToken = "";
  if (challengeSubmitting) return;
  setStatus("第 2 步 hCaptcha 验证已过期，请重新完成。", "err");
}}

if (combined) setStatus("请先完成第 1 步 Turnstile 验证。", "");
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
                data = await _read_siteverify_json(resp)
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
                data = await _read_siteverify_json(resp)
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
            "hcaptcha siteverify rejected | errors=%s hostname=%s token_len=%s "
            "token_shape=%s",
            errors or ["unknown"],
            data.get("hostname") or "-",
            len(token),
            "p1" if token.startswith("P1_") else "other",
        )
        if token.startswith("P1_") and len(token) > 200 and "invalid-input-response" in errors:
            # siteverify reports invalid-input-response for every token it
            # cannot redeem, with no distinguishing code: a sitekey Domain
            # allowlist rejecting the embedding host, a secret from a
            # different account than the sitekey, or a rotated secret all
            # look identical. A well-formed P1_ solve token rejected this way
            # means one of those sitekey/secret settings, not a failed
            # challenge.
            log.warning(
                "hcaptcha rejected a well-formed solve token as "
                "invalid-input-response; if real users hit this repeatedly, "
                "check the sitekey settings in the hCaptcha dashboard: "
                "Domain allowlisting must include this Mini App's public "
                "host (or be disabled), and the Secret Key (ES_...) must "
                "come from the account that owns the Site Key"
            )
    return success, errors


@dataclass(slots=True)
class _QueuedWebhookUpdate:
    update: dict[str, Any]
    update_id: int
    enqueued_at: float
    result: asyncio.Future[bool]
    lease_until: datetime | None = None
    priority: ExecutionPriority = ExecutionPriority.NORMAL
    auth_candidate: bool = False

    @property
    def privileged(self) -> bool:
        return self.priority < ExecutionPriority.NORMAL

    @property
    def critical(self) -> bool:
        return self.priority <= ExecutionPriority.CRITICAL


class _BoundedPriorityUpdateQueue:
    """Four FIFO lanes with hard capacity reserved by urgency.

    Ordinary work cannot consume security capacity, and automatic membership
    work cannot consume the critical operator lane. Unknown management-command
    candidates use a separate, unprivileged authentication lane: they bypass
    ordinary backlog without being allowed to consume trusted capacity. This
    is stronger than a plain ``PriorityQueue`` because already-queued low-
    priority items cannot consume another class's admission or worker capacity.
    """

    def __init__(
        self,
        *,
        ordinary_capacity: int,
        auth_capacity: int,
        security_capacity: int,
        critical_capacity: int,
    ) -> None:
        self._ordinary: asyncio.Queue[_QueuedWebhookUpdate] = asyncio.Queue(
            maxsize=max(1, int(ordinary_capacity))
        )
        self._auth: asyncio.Queue[_QueuedWebhookUpdate] = asyncio.Queue(
            maxsize=max(1, int(auth_capacity))
        )
        self._security: asyncio.Queue[_QueuedWebhookUpdate] = asyncio.Queue(
            maxsize=max(1, int(security_capacity))
        )
        self._critical: asyncio.Queue[_QueuedWebhookUpdate] = asyncio.Queue(
            maxsize=max(1, int(critical_capacity))
        )
        self.ordinary_maxsize = self._ordinary.maxsize
        self.auth_maxsize = self._auth.maxsize
        self.security_maxsize = self._security.maxsize
        self.critical_maxsize = self._critical.maxsize
        self.maxsize = (
            self.ordinary_maxsize
            + self.auth_maxsize
            + self.security_maxsize
            + self.critical_maxsize
        )

    def _queue_for(
        self,
        priority: ExecutionPriority,
        *,
        auth_candidate: bool = False,
    ) -> asyncio.Queue[_QueuedWebhookUpdate]:
        if auth_candidate:
            return self._auth
        if priority <= ExecutionPriority.CRITICAL:
            return self._critical
        if priority <= ExecutionPriority.HIGH:
            return self._security
        return self._ordinary

    def qsize(self) -> int:
        return (
            self._critical.qsize()
            + self._security.qsize()
            + self._auth.qsize()
            + self._ordinary.qsize()
        )

    def ordinary_qsize(self) -> int:
        return self._ordinary.qsize()

    def auth_qsize(self) -> int:
        return self._auth.qsize()

    def security_qsize(self) -> int:
        return self._security.qsize()

    def critical_qsize(self) -> int:
        return self._critical.qsize()

    def empty(self) -> bool:
        return (
            self._critical.empty()
            and self._security.empty()
            and self._auth.empty()
            and self._ordinary.empty()
        )

    def full(self) -> bool:
        return (
            self._critical.full()
            and self._security.full()
            and self._auth.full()
            and self._ordinary.full()
        )

    def ordinary_full(self) -> bool:
        return self._ordinary.full()

    def auth_full(self) -> bool:
        return self._auth.full()

    def security_full(self) -> bool:
        return self._security.full()

    def critical_full(self) -> bool:
        return self._critical.full()

    def can_accept(
        self,
        *,
        priority: ExecutionPriority,
        auth_candidate: bool = False,
    ) -> bool:
        # Keep the explicit overall check so tests and emergency instrumentation
        # can force the queue closed by replacing ``full``.
        if self.full():
            return False
        return not self._queue_for(
            priority,
            auth_candidate=auth_candidate,
        ).full()

    def put_nowait(self, item: _QueuedWebhookUpdate) -> None:
        self._queue_for(
            item.priority,
            auth_candidate=item.auth_candidate,
        ).put_nowait(item)

    async def get(
        self,
        *,
        priority: ExecutionPriority,
        auth_candidate: bool = False,
    ) -> _QueuedWebhookUpdate:
        return await self._queue_for(
            priority,
            auth_candidate=auth_candidate,
        ).get()

    def get_nowait(self) -> _QueuedWebhookUpdate:
        try:
            return self._critical.get_nowait()
        except asyncio.QueueEmpty:
            try:
                return self._security.get_nowait()
            except asyncio.QueueEmpty:
                try:
                    return self._auth.get_nowait()
                except asyncio.QueueEmpty:
                    return self._ordinary.get_nowait()

    def task_done(self, item: _QueuedWebhookUpdate) -> None:
        self._queue_for(
            item.priority,
            auth_candidate=item.auth_candidate,
        ).task_done()

    async def join(self) -> None:
        await asyncio.gather(
            self._critical.join(),
            self._security.join(),
            self._auth.join(),
            self._ordinary.join(),
        )

    def items(
        self,
        *,
        priority: ExecutionPriority | None = None,
        auth_candidate: bool = False,
    ) -> tuple[_QueuedWebhookUpdate, ...]:
        if priority is not None:
            return tuple(
                self._queue_for(
                    priority,
                    auth_candidate=auth_candidate,
                )._queue  # type: ignore[attr-defined]
            )
        return (
            *tuple(self._critical._queue),  # type: ignore[attr-defined]
            *tuple(self._security._queue),  # type: ignore[attr-defined]
            *tuple(self._auth._queue),  # type: ignore[attr-defined]
            *tuple(self._ordinary._queue),  # type: ignore[attr-defined]
        )


class _WebhookUpdateDeadlineExceeded(TimeoutError):
    def __init__(
        self,
        message: str,
        *,
        unfinished_task: asyncio.Task[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.unfinished_task = unfinished_task


class _WebhookUpdateQueue:
    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession] | None,
        secret_token: str,
        worker_count: int,
        critical_worker_count: int = WEBHOOK_CRITICAL_CONCURRENT_UPDATES,
        security_worker_count: int = WEBHOOK_SECURITY_CONCURRENT_UPDATES,
        auth_worker_count: int = WEBHOOK_AUTH_CONCURRENT_UPDATES,
        critical_queue_capacity: int = WEBHOOK_CRITICAL_QUEUE_CAPACITY,
        security_queue_capacity: int = WEBHOOK_SECURITY_QUEUE_CAPACITY,
        auth_queue_capacity: int = WEBHOOK_AUTH_QUEUE_CAPACITY,
    ) -> None:
        self.dispatcher = dispatcher
        self.bot = bot
        self.session_factory = session_factory
        self.secret_token = secret_token
        self.worker_count = max(1, int(worker_count))
        self.critical_worker_count = max(1, int(critical_worker_count))
        self.security_worker_count = max(1, int(security_worker_count))
        self.auth_worker_count = max(1, int(auth_worker_count))
        self.queue = _BoundedPriorityUpdateQueue(
            ordinary_capacity=self.worker_count * 4,
            auth_capacity=max(
                self.auth_worker_count,
                int(auth_queue_capacity),
            ),
            critical_capacity=max(
                self.critical_worker_count,
                int(critical_queue_capacity),
            ),
            security_capacity=max(
                self.security_worker_count,
                int(security_queue_capacity),
            ),
        )
        self._workers: list[asyncio.Task[None]] = []
        self._ordinary_workers: list[asyncio.Task[None]] = []
        self._auth_workers: list[asyncio.Task[None]] = []
        self._critical_workers: list[asyncio.Task[None]] = []
        self._security_workers: list[asyncio.Task[None]] = []
        self._workers_started = False
        self._processing_tasks: set[asyncio.Task[Any]] = set()
        self._orphaned_tasks: set[asyncio.Task[Any]] = set()
        self._orphaned_started_at: dict[asyncio.Task[Any], float] = {}
        self._deferred_tasks: set[asyncio.Task[Any]] = set()
        self._late_finalizers: dict[int, asyncio.Task[Any]] = {}
        self._late_finalizer_started_at: dict[int, float] = {}
        self._recovery_task: asyncio.Task[None] | None = None
        self._last_cleanup_at = 0.0
        self._active_started_at: dict[int, float] = {}
        self._active_auth_started_at: dict[int, float] = {}
        self._active_privileged_started_at: dict[int, float] = {}
        self._active_critical_started_at: dict[int, float] = {}
        self._active_security_started_at: dict[int, float] = {}
        self._start_lock = asyncio.Lock()
        self._dedup_lock = asyncio.Lock()
        self._stopped = False
        self._quiescing = False
        self._stop_task: asyncio.Task[None] | None = None
        self._fatal_error: str | None = None
        self._consecutive_failures = 0
        self._inflight_updates: dict[int, asyncio.Future[bool]] = {}
        self._completed_update_ids: dict[int, float] = {}
        self._seen_update_ids: dict[int, float] = {}
        self._accepted_updates = 0
        self._accepted_auth_updates = 0
        self._accepted_privileged_updates = 0
        self._accepted_critical_updates = 0
        self._accepted_security_updates = 0
        self._completed_updates = 0
        self._completed_auth_updates = 0
        self._completed_privileged_updates = 0
        self._completed_critical_updates = 0
        self._completed_security_updates = 0
        self._failed_updates = 0
        self._failed_auth_updates = 0
        self._failed_privileged_updates = 0
        self._failed_critical_updates = 0
        self._failed_security_updates = 0
        self._timed_out_updates = 0
        self._retried_updates = 0
        self._dropped_updates = 0
        self._deferred_failed_updates = 0
        self._late_failed_updates = 0
        self._dead_lettered_updates = 0
        self._retry_scheduled_updates = 0
        self._cleaned_inbox_updates = 0
        self._recovery_failures = 0
        self._last_business_error: str | None = None
        self._business_failure_issue: str | None = None
        self._failed_update_ids_since_success: set[int] = set()
        self._last_recovery_error: str | None = None
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._priority_cache: OrderedDict[
            int, tuple[ExecutionPriority, bool, float]
        ] = OrderedDict()
        self._untrusted_critical_windows: dict[tuple[int, int], deque[float]] = {}
        self._untrusted_auth_windows: dict[tuple[int, int], deque[float]] = {}
        self._demoted_untrusted_critical_updates = 0
        self._demoted_untrusted_auth_updates = 0

    def _classification_for_update(
        self,
        update: Any,
    ) -> tuple[ExecutionPriority, bool]:
        """Atomically classify one update and protect reserved lanes from spam."""

        update_id_raw = (
            update.get("update_id")
            if isinstance(update, dict)
            else getattr(update, "update_id", 0)
        )
        try:
            update_id = int(update_id_raw or 0)
        except (TypeError, ValueError):
            update_id = 0
        now = time.monotonic()
        if update_id:
            cached = self._priority_cache.get(update_id)
            if cached is not None and now - cached[2] <= _WEBHOOK_DEDUP_TTL_SECONDS:
                self._priority_cache.move_to_end(update_id)
                return cached[0], cached[1]

        priority = telegram_update_priority(update)
        sender_id = telegram_update_sender_id(update)
        chat_id = telegram_update_chat_id(update)
        if (
            priority <= ExecutionPriority.CRITICAL
            and sender_id > 0
            and not privileged_operator_is_trusted(sender_id, group_id=chat_id)
        ):
            scope_key = (chat_id, sender_id)
            window = self._untrusted_critical_windows.setdefault(scope_key, deque())
            cutoff = now - _WEBHOOK_UNTRUSTED_CRITICAL_WINDOW_SECONDS
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= _WEBHOOK_UNTRUSTED_CRITICAL_BURST:
                # Still use the isolated security lane; only the operator-only
                # reserve is protected from an unauthenticated flood.
                priority = ExecutionPriority.HIGH
                self._demoted_untrusted_critical_updates += 1
            else:
                window.append(now)

        auth_candidate = telegram_update_is_auth_candidate(
            update,
            priority=priority,
        )
        if auth_candidate:
            scope_key = (chat_id, sender_id)
            window = self._untrusted_auth_windows.setdefault(scope_key, deque())
            cutoff = now - _WEBHOOK_UNTRUSTED_AUTH_WINDOW_SECONDS
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= _WEBHOOK_UNTRUSTED_AUTH_BURST:
                # Duplicate/forged candidates keep ordinary delivery semantics
                # after a small fast-auth burst. A successful first check warms
                # the trusted cache, so subsequent real administrator commands
                # are classified CRITICAL instead of reaching this branch.
                auth_candidate = False
                self._demoted_untrusted_auth_updates += 1
            else:
                window.append(now)

        if update_id:
            self._priority_cache[update_id] = (priority, auth_candidate, now)
            self._priority_cache.move_to_end(update_id)
            while len(self._priority_cache) > _WEBHOOK_DEDUP_MAX_UPDATES:
                self._priority_cache.popitem(last=False)
        if len(self._untrusted_critical_windows) > 4096:
            for scope_key, window in tuple(self._untrusted_critical_windows.items()):
                if not window or window[-1] <= now - _WEBHOOK_UNTRUSTED_CRITICAL_WINDOW_SECONDS:
                    self._untrusted_critical_windows.pop(scope_key, None)
        if len(self._untrusted_auth_windows) > 4096:
            for scope_key, window in tuple(self._untrusted_auth_windows.items()):
                if (
                    not window
                    or window[-1]
                    <= now - _WEBHOOK_UNTRUSTED_AUTH_WINDOW_SECONDS
                ):
                    self._untrusted_auth_windows.pop(scope_key, None)
        return priority, auth_candidate

    def _priority_for_update(self, update: Any) -> ExecutionPriority:
        return self._classification_for_update(update)[0]

    @property
    def _durable_inbox_enabled(self) -> bool:
        return callable(self.session_factory)

    def _record_priority_accepted(
        self,
        priority: ExecutionPriority,
        *,
        auth_candidate: bool = False,
    ) -> None:
        if auth_candidate:
            self._accepted_auth_updates += 1
        if priority >= ExecutionPriority.NORMAL:
            return
        self._accepted_privileged_updates += 1
        if priority <= ExecutionPriority.CRITICAL:
            self._accepted_critical_updates += 1
        else:
            self._accepted_security_updates += 1

    def _record_priority_completed(
        self,
        priority: ExecutionPriority,
        *,
        auth_candidate: bool = False,
    ) -> None:
        if auth_candidate:
            self._completed_auth_updates += 1
        if priority >= ExecutionPriority.NORMAL:
            return
        self._completed_privileged_updates += 1
        if priority <= ExecutionPriority.CRITICAL:
            self._completed_critical_updates += 1
        else:
            self._completed_security_updates += 1

    def _record_priority_failed(
        self,
        priority: ExecutionPriority,
        *,
        auth_candidate: bool = False,
    ) -> None:
        if auth_candidate:
            self._failed_auth_updates += 1
        if priority >= ExecutionPriority.NORMAL:
            return
        self._failed_privileged_updates += 1
        if priority <= ExecutionPriority.CRITICAL:
            self._failed_critical_updates += 1
        else:
            self._failed_security_updates += 1

    def verify_secret(self, request: web.Request) -> bool:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        return secrets.compare_digest(supplied, self.secret_token)

    async def _ensure_durable_update(
        self,
        update_id: int,
        payload: dict[str, Any],
    ) -> bool:
        """Persist raw update; return True when it is already terminal."""

        if not self._durable_inbox_enabled:
            return False
        assert self.session_factory is not None
        priority, auth_candidate = self._classification_for_update(payload)
        with execution_priority_scope(priority):
            async with self.session_factory() as session:
                dialect = getattr(getattr(session, "bind", None), "dialect", None)
                if getattr(dialect, "name", "") == "sqlite":
                    await session.execute(
                        sqlite_insert(WebhookInboxUpdate)
                        .values(
                            update_id=int(update_id),
                            payload=dict(payload),
                            priority=int(priority),
                            auth_candidate=auth_candidate,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[WebhookInboxUpdate.update_id]
                        )
                    )
                else:
                    row = await session.get(WebhookInboxUpdate, int(update_id))
                    if row is None:
                        session.add(
                            WebhookInboxUpdate(
                                update_id=int(update_id),
                                payload=dict(payload),
                                priority=int(priority),
                                auth_candidate=auth_candidate,
                            )
                        )
                await session.commit()
                row = await session.get(WebhookInboxUpdate, int(update_id))
                return bool(
                    row is not None
                    and (
                        row.completed_at is not None
                        or row.dead_lettered_at is not None
                    )
                )

    async def accept_durable_update(self, payload: dict[str, Any]) -> int:
        """Persist one update before a transport is allowed to acknowledge it.

        Both webhook HTTP delivery and getUpdates polling use this boundary.
        Once this method returns, a process crash can at worst delay execution:
        the recovery loop still owns the canonical payload. Publishing to the
        bounded in-memory worker queue is deliberately best effort and never
        weakens that durability guarantee.
        """

        if not self._durable_inbox_enabled:
            raise RuntimeError("durable Telegram update inbox is unavailable")
        if self._stopped or self._quiescing:
            raise RuntimeError("durable Telegram update inbox is stopping")
        if not isinstance(payload, dict):
            raise ValueError("Telegram update payload must be an object")
        update_id_value = payload.get("update_id")
        if isinstance(update_id_value, bool) or not isinstance(update_id_value, int):
            raise ValueError("Telegram update payload has no valid update_id")
        update_id = int(update_id_value)

        terminal = await self._ensure_durable_update(update_id, payload)
        self._recovery_failures = 0
        self._last_recovery_error = None
        if terminal:
            return update_id

        await self._ensure_workers()

        async def _publish_persisted() -> None:
            try:
                await self._enqueue_durable_update(
                    update_id,
                    priority_hint=self._classification_for_update(payload)[0],
                )
            except Exception:
                # Persistence, not queue publication, is the acknowledgement
                # boundary. Recovery will retry this row after queue pressure or
                # a transient claim/load failure.
                log.exception(
                    "durable Telegram update persisted but immediate enqueue "
                    "failed | update=%s",
                    update_id,
                )

        publish = asyncio.create_task(
            _publish_persisted(),
            name=f"telegram-inbox-publish:{update_id}",
        )
        self._track_deferred_task(publish)
        return update_id

    async def _claim_durable_update(self, update_id: int) -> datetime | None:
        if not self._durable_inbox_enabled:
            return None
        assert self.session_factory is not None
        now = now_shanghai_naive()
        lease_until = now + timedelta(seconds=_WEBHOOK_INBOX_LEASE_SECONDS)
        async with self.session_factory() as session:
            result = await session.execute(
                update(WebhookInboxUpdate)
                .where(
                    WebhookInboxUpdate.update_id == int(update_id),
                    WebhookInboxUpdate.completed_at.is_(None),
                    WebhookInboxUpdate.dead_lettered_at.is_(None),
                    WebhookInboxUpdate.attempts < _WEBHOOK_INBOX_MAX_ATTEMPTS,
                    or_(
                        WebhookInboxUpdate.next_attempt_at.is_(None),
                        WebhookInboxUpdate.next_attempt_at <= now,
                    ),
                    or_(
                        WebhookInboxUpdate.lease_until.is_(None),
                        WebhookInboxUpdate.lease_until <= now,
                    ),
                )
                .values(
                    lease_until=lease_until,
                    attempts=WebhookInboxUpdate.attempts + 1,
                    next_attempt_at=None,
                    last_error="",
                    updated_at=now,
                )
            )
            await session.commit()
            return lease_until if int(result.rowcount or 0) == 1 else None

    async def _load_durable_payload(self, update_id: int) -> dict[str, Any] | None:
        if not self._durable_inbox_enabled:
            return None
        assert self.session_factory is not None
        async with self.session_factory() as session:
            payload = await session.scalar(
                select(WebhookInboxUpdate.payload).where(
                    WebhookInboxUpdate.update_id == int(update_id)
                )
            )
        return dict(payload) if isinstance(payload, dict) else None

    async def _renew_durable_lease(self, queued: _QueuedWebhookUpdate) -> bool:
        """Extend a live lease without making the update claimable in between."""

        if not self._durable_inbox_enabled or queued.lease_until is None:
            return True
        assert self.session_factory is not None
        now = now_shanghai_naive()
        lease_until = now + timedelta(seconds=_WEBHOOK_INBOX_LEASE_SECONDS)
        async with self.session_factory() as session:
            result = await session.execute(
                update(WebhookInboxUpdate)
                .where(
                    WebhookInboxUpdate.update_id == queued.update_id,
                    WebhookInboxUpdate.completed_at.is_(None),
                    WebhookInboxUpdate.dead_lettered_at.is_(None),
                    WebhookInboxUpdate.lease_until == queued.lease_until,
                )
                .values(lease_until=lease_until, updated_at=now)
            )
            await session.commit()
        if int(result.rowcount or 0) != 1:
            return False
        queued.lease_until = lease_until
        return True

    async def _complete_durable_update(self, queued: _QueuedWebhookUpdate) -> bool:
        if not self._durable_inbox_enabled or queued.lease_until is None:
            return True
        assert self.session_factory is not None
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            result = await session.execute(
                update(WebhookInboxUpdate)
                .where(
                    WebhookInboxUpdate.update_id == queued.update_id,
                    WebhookInboxUpdate.completed_at.is_(None),
                    WebhookInboxUpdate.dead_lettered_at.is_(None),
                    WebhookInboxUpdate.lease_until == queued.lease_until,
                )
                .values(
                    payload={},
                    completed_at=now,
                    lease_until=None,
                    next_attempt_at=None,
                    last_error="",
                    updated_at=now,
                )
            )
            await session.commit()
            if int(result.rowcount or 0) == 1:
                return True
            row = await session.get(WebhookInboxUpdate, queued.update_id)
            return bool(row is not None and row.completed_at is not None)

    @staticmethod
    def _retry_delay_seconds(attempts: int, *, update_id: int = 0) -> float:
        exponent = max(0, int(attempts) - 1)
        base_delay = min(
            _WEBHOOK_INBOX_RETRY_MAX_SECONDS,
            _WEBHOOK_INBOX_RETRY_BASE_SECONDS * (2**exponent),
        )
        # Stable per-update jitter avoids a wave of poison/transient rows all
        # becoming claimable on the same recovery tick after an outage.
        jitter = 0.90 + ((abs(int(update_id)) * 17) % 21) / 100.0
        return min(_WEBHOOK_INBOX_RETRY_MAX_SECONDS, base_delay * jitter)

    async def _release_durable_update(
        self,
        queued: _QueuedWebhookUpdate,
        *,
        error: str,
    ) -> str:
        if not self._durable_inbox_enabled or queued.lease_until is None:
            return "noop"
        assert self.session_factory is not None
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(WebhookInboxUpdate.attempts).where(
                        WebhookInboxUpdate.update_id == queued.update_id,
                        WebhookInboxUpdate.completed_at.is_(None),
                        WebhookInboxUpdate.dead_lettered_at.is_(None),
                        WebhookInboxUpdate.lease_until == queued.lease_until,
                    )
                )
            ).first()
            if row is None:
                return "lost"
            attempts = max(0, int(row[0] or 0))
            is_dead = attempts >= _WEBHOOK_INBOX_MAX_ATTEMPTS
            next_attempt_at = None
            if not is_dead:
                next_attempt_at = now + timedelta(
                    seconds=self._retry_delay_seconds(
                        attempts,
                        update_id=queued.update_id,
                    )
                )
            result = await session.execute(
                update(WebhookInboxUpdate)
                .where(
                    WebhookInboxUpdate.update_id == queued.update_id,
                    WebhookInboxUpdate.completed_at.is_(None),
                    WebhookInboxUpdate.dead_lettered_at.is_(None),
                    WebhookInboxUpdate.lease_until == queued.lease_until,
                )
                .values(
                    lease_until=None,
                    next_attempt_at=next_attempt_at,
                    dead_lettered_at=now if is_dead else None,
                    last_error=str(error or "")[:500],
                    updated_at=now,
                )
            )
            await session.commit()
        if int(result.rowcount or 0) != 1:
            return "lost"
        if is_dead:
            self._dead_lettered_updates += 1
            self._last_business_error = (
                f"update {queued.update_id} moved to dead letter after "
                f"{attempts} attempts: {str(error or '')[:300]}"
            )
            return "dead"
        self._retry_scheduled_updates += 1
        return "retry"

    async def _unclaim_durable_update(
        self,
        queued: _QueuedWebhookUpdate,
        *,
        reason: str,
    ) -> None:
        """Undo a claim that never reached a worker (queue pressure only)."""

        if not self._durable_inbox_enabled or queued.lease_until is None:
            return
        assert self.session_factory is not None
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(WebhookInboxUpdate.attempts).where(
                        WebhookInboxUpdate.update_id == queued.update_id,
                        WebhookInboxUpdate.completed_at.is_(None),
                        WebhookInboxUpdate.dead_lettered_at.is_(None),
                        WebhookInboxUpdate.lease_until == queued.lease_until,
                    )
                )
            ).first()
            if row is None:
                return
            await session.execute(
                update(WebhookInboxUpdate)
                .where(
                    WebhookInboxUpdate.update_id == queued.update_id,
                    WebhookInboxUpdate.completed_at.is_(None),
                    WebhookInboxUpdate.dead_lettered_at.is_(None),
                    WebhookInboxUpdate.lease_until == queued.lease_until,
                )
                .values(
                    attempts=max(0, int(row[0] or 0) - 1),
                    lease_until=None,
                    next_attempt_at=None,
                    last_error=str(reason or "")[:500],
                    updated_at=now,
                )
            )
            await session.commit()

    async def _cleanup_durable_inbox(self) -> int:
        if not self._durable_inbox_enabled:
            return 0
        assert self.session_factory is not None
        now = now_shanghai_naive()
        completed_cutoff = now - timedelta(seconds=_WEBHOOK_INBOX_RETENTION_SECONDS)
        dead_cutoff = now - timedelta(seconds=_WEBHOOK_INBOX_DLQ_RETENTION_SECONDS)
        cleaned_total = 0
        cleanup_deadline = time.monotonic() + 0.25
        # Drain several batches per pass while yielding between commits.  A
        # single 256-row batch/minute had a hard growth threshold of only
        # 4.27 updates/s, below realistic traffic for multiple active groups.
        while cleaned_total < _WEBHOOK_INBOX_CLEANUP_BATCH * 16:
            cleaned_round = 0
            for terminal_column, cutoff in (
                (WebhookInboxUpdate.completed_at, completed_cutoff),
                (WebhookInboxUpdate.dead_lettered_at, dead_cutoff),
            ):
                remaining = _WEBHOOK_INBOX_CLEANUP_BATCH * 16 - cleaned_total
                if remaining <= 0:
                    break
                batch_limit = min(_WEBHOOK_INBOX_CLEANUP_BATCH, remaining)
                async with self.session_factory() as session:
                    stale_ids = (
                        select(WebhookInboxUpdate.update_id)
                        .where(
                            terminal_column.is_not(None),
                            terminal_column < cutoff,
                        )
                        .order_by(terminal_column)
                        .limit(batch_limit)
                    )
                    result = await session.execute(
                        delete(WebhookInboxUpdate).where(
                            WebhookInboxUpdate.update_id.in_(stale_ids)
                        )
                    )
                    await session.commit()
                cleaned = max(0, int(result.rowcount or 0))
                cleaned_round += cleaned
                cleaned_total += cleaned
                if time.monotonic() >= cleanup_deadline:
                    break
            if cleaned_round == 0:
                break
            if time.monotonic() >= cleanup_deadline:
                break
            await asyncio.sleep(0)
        self._cleaned_inbox_updates += cleaned_total
        return cleaned_total

    async def _prepare_durable_recovery(self) -> None:
        if not self._durable_inbox_enabled:
            return
        assert self.session_factory is not None
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            # Never revoke a live lease merely because another processor has
            # started. Rolling deploys, an accidental second instance, or an
            # overlapping shutdown can all leave the original handler active.
            # Recovery queries already reclaim a row as soon as its lease
            # naturally expires.
            await session.execute(
                update(WebhookInboxUpdate)
                .where(
                    WebhookInboxUpdate.completed_at.is_(None),
                    WebhookInboxUpdate.dead_lettered_at.is_(None),
                    WebhookInboxUpdate.attempts >= _WEBHOOK_INBOX_MAX_ATTEMPTS,
                    or_(
                        WebhookInboxUpdate.lease_until.is_(None),
                        WebhookInboxUpdate.lease_until <= now,
                    ),
                )
                .values(
                    lease_until=None,
                    next_attempt_at=None,
                    dead_lettered_at=now,
                    last_error="attempt limit reached before recovery",
                    updated_at=now,
                )
            )
            await session.commit()
        await self._cleanup_durable_inbox()
        self._last_cleanup_at = time.monotonic()

    async def start_recovery(self) -> None:
        if not self._durable_inbox_enabled or self._recovery_task is not None:
            return
        await self._prepare_durable_recovery()
        await self._ensure_workers()
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(),
            name="telegram-webhook-inbox-recovery",
        )

    async def _recover_durable_once(self) -> int:
        if not self._durable_inbox_enabled or self._stopped or self._quiescing:
            return 0
        assert self.session_factory is not None
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            eligibility = (
                WebhookInboxUpdate.completed_at.is_(None),
                WebhookInboxUpdate.dead_lettered_at.is_(None),
                WebhookInboxUpdate.attempts < _WEBHOOK_INBOX_MAX_ATTEMPTS,
                or_(
                    WebhookInboxUpdate.next_attempt_at.is_(None),
                    WebhookInboxUpdate.next_attempt_at <= now,
                ),
                or_(
                    WebhookInboxUpdate.lease_until.is_(None),
                    WebhookInboxUpdate.lease_until <= now,
                ),
            )
            lane_ranges = (
                (
                    WebhookInboxUpdate.priority
                    <= int(ExecutionPriority.CRITICAL),
                ),
                (
                    and_(
                        WebhookInboxUpdate.priority
                        > int(ExecutionPriority.CRITICAL),
                        WebhookInboxUpdate.priority
                        < int(ExecutionPriority.NORMAL),
                    ),
                ),
                (
                    WebhookInboxUpdate.priority
                    >= int(ExecutionPriority.NORMAL),
                    WebhookInboxUpdate.auth_candidate.is_(True),
                ),
                (
                    WebhookInboxUpdate.priority
                    >= int(ExecutionPriority.NORMAL),
                    or_(
                        WebhookInboxUpdate.auth_candidate.is_(False),
                        WebhookInboxUpdate.auth_candidate.is_(None),
                    ),
                ),
            )
            rows: list[Any] = []
            for lane_range in lane_ranges:
                lane_rows = (
                    await session.execute(
                        select(
                            WebhookInboxUpdate.update_id,
                            WebhookInboxUpdate.payload,
                        )
                        .where(*eligibility, *lane_range)
                        .order_by(WebhookInboxUpdate.created_at)
                        .limit(_WEBHOOK_INBOX_RECOVERY_BATCH)
                    )
                ).all()
                rows.extend(lane_rows)

        # Prefer critical, then automatic-security work within each recovery
        # page while preserving FIFO order inside every class (sort is stable).
        def recovery_order(row: Any) -> tuple[int, bool]:
            priority, auth_candidate = self._classification_for_update(row[1])
            return int(priority), not auth_candidate

        prioritized_rows = sorted(rows, key=recovery_order)
        recovered = 0
        for update_id, payload in prioritized_rows:
            if self._stopped or self._quiescing or self.queue.full():
                break
            priority, auth_candidate = self._classification_for_update(payload)
            if not self.queue.can_accept(
                priority=priority,
                auth_candidate=auth_candidate,
            ):
                # A full ordinary lane must not hide a privileged row later in
                # this recovery page, and vice versa.
                continue
            async with self._dedup_lock:
                self._prune_dedup_state(now=asyncio.get_running_loop().time())
                if int(update_id) in self._inflight_updates:
                    continue
            with execution_priority_scope(priority):
                lease_until = await self._claim_durable_update(int(update_id))
            if lease_until is None:
                continue
            loop = asyncio.get_running_loop()
            result = loop.create_future()
            queued = _QueuedWebhookUpdate(
                update=dict(payload or {}),
                update_id=int(update_id),
                enqueued_at=time.monotonic(),
                result=result,
                lease_until=lease_until,
                priority=priority,
                auth_candidate=auth_candidate,
            )
            must_unclaim = False
            async with self._dedup_lock:
                if (
                    int(update_id) in self._inflight_updates
                    or not self.queue.can_accept(
                        priority=priority,
                        auth_candidate=auth_candidate,
                    )
                ):
                    must_unclaim = True
                else:
                    try:
                        self.queue.put_nowait(queued)
                    except asyncio.QueueFull:
                        must_unclaim = True
                    else:
                        self._completed_update_ids.pop(int(update_id), None)
                        self._inflight_updates[int(update_id)] = result
                        if int(update_id) in self._seen_update_ids:
                            self._retried_updates += 1
                        self._seen_update_ids[int(update_id)] = loop.time()
                        self._accepted_updates += 1
                        self._record_priority_accepted(
                            priority,
                            auth_candidate=auth_candidate,
                        )
                        recovered += 1
            if must_unclaim:
                with execution_priority_scope(priority):
                    await self._unclaim_durable_update(
                        queued,
                        reason="recovery queue full or update already active",
                    )
                if self.queue.full():
                    break
        return recovered

    async def _enqueue_durable_update(
        self,
        update_id: int,
        *,
        priority_hint: ExecutionPriority | None = None,
    ) -> bool:
        """Best-effort publish of a persisted row to the in-process queue."""

        if self._stopped or self._quiescing or self.queue.full():
            return False
        loop = asyncio.get_running_loop()
        async with self._dedup_lock:
            self._prune_dedup_state(now=loop.time())
            if update_id in self._inflight_updates:
                return True

        selected_priority = (
            priority_hint
            if priority_hint is not None
            else ExecutionPriority.NORMAL
        )
        with execution_priority_scope(selected_priority):
            payload = await self._load_durable_payload(update_id)
        if payload is None:
            return False
        priority, auth_candidate = self._classification_for_update(payload)
        if not self.queue.can_accept(
            priority=priority,
            auth_candidate=auth_candidate,
        ):
            return False

        with execution_priority_scope(priority):
            lease_until = await self._claim_durable_update(update_id)
        if lease_until is None:
            # Completed, delayed, dead-lettered, or already owned. Every case is
            # safe to ACK because the canonical payload is already durable.
            return False
        result = loop.create_future()
        queued = _QueuedWebhookUpdate(
            update=payload,
            update_id=update_id,
            enqueued_at=time.monotonic(),
            result=result,
            lease_until=lease_until,
            priority=priority,
            auth_candidate=auth_candidate,
        )
        must_unclaim = False
        async with self._dedup_lock:
            if (
                update_id in self._inflight_updates
                or not self.queue.can_accept(
                    priority=priority,
                    auth_candidate=auth_candidate,
                )
            ):
                must_unclaim = True
            else:
                try:
                    self.queue.put_nowait(queued)
                except asyncio.QueueFull:
                    must_unclaim = True
                else:
                    now = loop.time()
                    if update_id in self._seen_update_ids:
                        self._retried_updates += 1
                    self._seen_update_ids[update_id] = now
                    self._inflight_updates[update_id] = result
                    self._accepted_updates += 1
                    self._record_priority_accepted(
                        priority,
                        auth_candidate=auth_candidate,
                    )
        if must_unclaim:
            with execution_priority_scope(priority):
                await self._unclaim_durable_update(
                    queued,
                    reason="webhook queue full or update already active",
                )
            return False
        return True

    async def _recovery_loop(self) -> None:
        while not self._stopped and not self._quiescing:
            try:
                await self._recover_durable_once()
                now = time.monotonic()
                if (
                    now - self._last_cleanup_at
                    >= _WEBHOOK_INBOX_CLEANUP_INTERVAL_SECONDS
                ):
                    await self._cleanup_durable_inbox()
                    self._last_cleanup_at = now
                self._recovery_failures = 0
                self._last_recovery_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._recovery_failures += 1
                self._last_recovery_error = str(exc)[:500]
                if self._recovery_failures >= _WEBHOOK_FAILURE_THRESHOLD:
                    self._set_fatal(
                        "durable webhook inbox recovery failed "
                        f"{self._recovery_failures} consecutive times: {exc}"
                    )
                log.exception("durable webhook inbox recovery failed")
            try:
                await asyncio.sleep(_WEBHOOK_INBOX_RECOVERY_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise

    async def handle_verified(self, request: web.Request) -> web.Response:
        try:
            update = await _read_json_body_hard(
                request,
                loads=self.bot.session.json_loads,
                timeout_seconds=_WEBHOOK_REQUEST_BODY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return web.Response(text="Update body timeout", status=408)
        except Exception:
            return web.Response(text="Invalid update", status=400)
        if not isinstance(update, dict):
            return web.Response(text="Invalid update", status=400)
        update_id_value = update.get("update_id")
        if isinstance(update_id_value, bool) or not isinstance(update_id_value, int):
            return web.Response(text="Invalid update id", status=400)
        update_id = int(update_id_value)

        try:
            if self._durable_inbox_enabled:
                await self.accept_durable_update(update)
                return web.json_response({}, dumps=self.bot.session.json_dumps)
            terminal = await self._ensure_durable_update(update_id, update)
            if terminal:
                return web.json_response({}, dumps=self.bot.session.json_dumps)
        except Exception:
            self._recovery_failures += 1
            self._last_recovery_error = "webhook inbox persistence unavailable"
            if self._recovery_failures >= _WEBHOOK_FAILURE_THRESHOLD:
                self._set_fatal(self._last_recovery_error)
            log.exception("failed to persist webhook update before acknowledgement")
            return web.Response(text="Webhook persistence unavailable", status=503)

        await self._ensure_workers()
        loop = asyncio.get_running_loop()
        async with self._dedup_lock:
            self._prune_dedup_state(now=loop.time())
            if update_id in self._completed_update_ids:
                return web.json_response({}, dumps=self.bot.session.json_dumps)

            result = self._inflight_updates.get(update_id)
            if result is None:
                priority, auth_candidate = self._classification_for_update(update)
                hard_issue = (
                    self._stopped
                    or self._fatal_error is not None
                )
                ordinary_business_failure = (
                    self._business_failure_issue is not None
                    and priority >= ExecutionPriority.NORMAL
                    and not auth_candidate
                )
                if (
                    hard_issue
                    or ordinary_business_failure
                    or not self.queue.can_accept(
                        priority=priority,
                        auth_candidate=auth_candidate,
                    )
                ):
                    return web.Response(text="Webhook unavailable", status=503)
                lease_until = None
                result = loop.create_future()
                queued = _QueuedWebhookUpdate(
                    update=update,
                    update_id=update_id,
                    enqueued_at=time.monotonic(),
                    result=result,
                    lease_until=lease_until,
                    priority=priority,
                    auth_candidate=auth_candidate,
                )
                try:
                    self.queue.put_nowait(queued)
                except asyncio.QueueFull:
                    return web.Response(text="Webhook busy", status=503)
                if update_id in self._seen_update_ids:
                    self._retried_updates += 1
                self._seen_update_ids[update_id] = loop.time()
                self._inflight_updates[update_id] = result
                self._accepted_updates += 1
                self._record_priority_accepted(
                    priority,
                    auth_candidate=auth_candidate,
                )

        # The worker returns success only after synchronous handlers finish, or
        # after a detached group-reply job is both enqueued and represented by
        # the durable inbox row. Concurrent deliveries share this future.
        try:
            succeeded = await asyncio.wait_for(
                asyncio.shield(result),
                timeout=_WEBHOOK_HTTP_RESPONSE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return web.Response(text="Update processing timeout", status=503)
        if not succeeded:
            return web.Response(text="Update processing failed", status=503)
        return web.json_response({}, dumps=self.bot.session.json_dumps)

    def _prune_dedup_state(self, *, now: float) -> None:
        cutoff = now - _WEBHOOK_DEDUP_TTL_SECONDS
        limit = max(1, int(_WEBHOOK_DEDUP_MAX_UPDATES))
        for mapping in (self._completed_update_ids, self._seen_update_ids):
            stale = [key for key, timestamp in mapping.items() if timestamp < cutoff]
            for key in stale:
                mapping.pop(key, None)
            # Callers prune immediately before publishing one new key. Keep one
            # slot free so the insertion cannot grow the map to limit + 1.
            while len(mapping) >= limit:
                mapping.pop(next(iter(mapping)), None)

    async def _finish_queued_update(
        self,
        queued: _QueuedWebhookUpdate,
        *,
        succeeded: bool,
    ) -> None:
        async with self._dedup_lock:
            self._prune_dedup_state(now=asyncio.get_running_loop().time())
            current = self._inflight_updates.get(queued.update_id)
            has_late_finalizer = queued.update_id in self._late_finalizers
            if current is queued.result and not has_late_finalizer:
                self._inflight_updates.pop(queued.update_id, None)
            if succeeded and not has_late_finalizer:
                self._completed_update_ids[queued.update_id] = (
                    asyncio.get_running_loop().time()
                )
            if not queued.result.done():
                queued.result.set_result(succeeded)

    async def _ensure_workers(self) -> None:
        if self._stopped:
            return
        async with self._start_lock:
            if self._stopped:
                return
            self._workers_started = True
            self._ordinary_workers = [
                worker for worker in self._ordinary_workers if not worker.done()
            ]
            self._auth_workers = [
                worker for worker in self._auth_workers if not worker.done()
            ]
            self._security_workers = [
                worker for worker in self._security_workers if not worker.done()
            ]
            self._critical_workers = [
                worker for worker in self._critical_workers if not worker.done()
            ]

            while len(self._ordinary_workers) < self.worker_count:
                index = len(self._ordinary_workers)
                worker = asyncio.create_task(
                    self._worker(priority=ExecutionPriority.NORMAL),
                    name=f"telegram-webhook-worker-{index + 1}",
                    context=Context(),
                )
                worker.add_done_callback(self._worker_finished)
                self._ordinary_workers.append(worker)
            while len(self._auth_workers) < self.auth_worker_count:
                index = len(self._auth_workers)
                worker = asyncio.create_task(
                    self._worker(
                        priority=ExecutionPriority.NORMAL,
                        auth_candidate=True,
                    ),
                    name=f"telegram-auth-candidate-worker-{index + 1}",
                    context=Context(),
                )
                worker.add_done_callback(self._worker_finished)
                self._auth_workers.append(worker)
            while len(self._security_workers) < self.security_worker_count:
                index = len(self._security_workers)
                worker = asyncio.create_task(
                    self._worker(priority=ExecutionPriority.HIGH),
                    name=f"telegram-security-worker-{index + 1}",
                    context=Context(),
                )
                worker.add_done_callback(self._worker_finished)
                self._security_workers.append(worker)
            while len(self._critical_workers) < self.critical_worker_count:
                index = len(self._critical_workers)
                worker = asyncio.create_task(
                    self._worker(priority=ExecutionPriority.CRITICAL),
                    name=f"telegram-critical-worker-{index + 1}",
                    context=Context(),
                )
                worker.add_done_callback(self._worker_finished)
                self._critical_workers.append(worker)
            self._workers = [
                *self._ordinary_workers,
                *self._auth_workers,
                *self._security_workers,
                *self._critical_workers,
            ]

    def _worker_finished(self, task: asyncio.Task[None]) -> None:
        if self._stopped:
            return
        if task.cancelled():
            issue = "webhook worker 意外被取消"
        else:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                issue = "webhook worker 意外被取消"
            else:
                issue = (
                    f"webhook worker 异常退出：{error}"
                    if error is not None
                    else "webhook worker 意外正常退出"
                )
        self._set_fatal(issue)
        try:
            asyncio.create_task(
                self._ensure_workers(),
                name="telegram-update-worker-replenish",
                context=Context(),
            )
        except RuntimeError:
            pass

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _track_orphan(self, task: asyncio.Task[Any]) -> None:
        self._processing_tasks.discard(task)
        if task.done():
            self._consume_task_result(task)
            return
        self._orphaned_tasks.add(task)
        self._orphaned_started_at.setdefault(task, time.monotonic())

        def finished(done: asyncio.Task[Any]) -> None:
            self._orphaned_tasks.discard(done)
            self._orphaned_started_at.pop(done, None)
            self._consume_task_result(done)

        task.add_done_callback(finished)

    def _set_fatal(self, issue: str) -> None:
        if self._fatal_error is None:
            self._fatal_error = issue[:500]
            log.error("Telegram webhook processor unhealthy: %s", self._fatal_error)

    async def _await_stage(
        self,
        awaitable: Any,
        *,
        deadline: float,
    ) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise _WebhookUpdateDeadlineExceeded("update deadline exceeded")
        task = asyncio.create_task(awaitable)
        self._processing_tasks.add(task)
        try:
            done, _pending = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            task.cancel()
            self._track_orphan(task)
            raise
        if task in done:
            self._processing_tasks.discard(task)
            if task.cancelled():
                raise RuntimeError("webhook update stage was cancelled")
            return task.result()

        task.cancel()
        done, _pending = await asyncio.wait(
            {task},
            timeout=_WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS,
        )
        if task in done:
            self._processing_tasks.discard(task)
            if task.cancelled():
                raise _WebhookUpdateDeadlineExceeded("update deadline exceeded")
            # A handler may catch cancellation to finish an atomic DB/Telegram
            # compensation and return during the grace period.  Its successful
            # result is authoritative: discarding it and releasing the durable
            # lease would replay already-applied side effects.
            return task.result()
        else:
            self._track_orphan(task)
            raise _WebhookUpdateDeadlineExceeded(
                "update deadline exceeded",
                unfinished_task=task,
            )

    def _track_deferred_task(self, task: asyncio.Task[Any]) -> None:
        self._deferred_tasks.add(task)

        def _finished(done: asyncio.Task[Any]) -> None:
            self._deferred_tasks.discard(done)
            self._consume_task_result(done)

        task.add_done_callback(_finished)

    async def _wait_task_with_lease(
        self,
        queued: _QueuedWebhookUpdate,
        task: asyncio.Task[Any],
    ) -> Any:
        """Wait for a terminal task while keeping its durable lease exclusive."""

        renew_interval = max(0.05, _WEBHOOK_INBOX_LEASE_SECONDS / 3.0)
        lease_warning_reported = False
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=renew_interval)
                if task in done:
                    if task.cancelled():
                        raise RuntimeError("webhook update task was cancelled")
                    return task.result()
                try:
                    renewed = await self._renew_durable_lease(queued)
                except Exception:
                    renewed = False
                    if not lease_warning_reported:
                        log.exception(
                            "durable webhook lease renewal failed; retaining the "
                            "in-memory ownership barrier | update=%s",
                            queued.update_id,
                        )
                if not renewed and not lease_warning_reported:
                    lease_warning_reported = True
                    self._set_fatal(
                        f"durable webhook lease renewal was lost for "
                        f"update {queued.update_id}"
                    )
                # Never stop waiting merely because persistence is temporarily
                # unavailable. The in-memory inflight entry remains the second
                # ownership barrier, so recovery in this process cannot replay
                # the update while the old task is still alive.
        except asyncio.CancelledError:
            task.cancel()
            self._track_orphan(task)
            raise

    def _record_deferred_failure(
        self,
        update_id: int,
        error: str,
        *,
        priority: ExecutionPriority = ExecutionPriority.NORMAL,
        auth_candidate: bool = False,
    ) -> None:
        self._deferred_failed_updates += 1
        self._failed_updates += 1
        self._record_priority_failed(
            priority,
            auth_candidate=auth_candidate,
        )
        self._last_failure_at = time.time()
        self._record_business_failure(update_id, error)

    def _record_business_failure(self, update_id: int, error: str) -> None:
        normalized_id = int(update_id)
        self._last_business_error = f"update={normalized_id} error={error}"[:500]
        if len(self._failed_update_ids_since_success) < _WEBHOOK_FAILURE_THRESHOLD:
            self._failed_update_ids_since_success.add(normalized_id)
        distinct_failures = len(self._failed_update_ids_since_success)
        if distinct_failures >= _WEBHOOK_FAILURE_THRESHOLD:
            self._business_failure_issue = (
                "durable webhook processing failed for "
                f"{distinct_failures} consecutive distinct updates"
            )

    def _record_durable_success(
        self,
        *,
        priority: ExecutionPriority = ExecutionPriority.NORMAL,
        auth_candidate: bool = False,
    ) -> None:
        self._consecutive_failures = 0
        self._failed_update_ids_since_success.clear()
        self._business_failure_issue = None
        self._completed_updates += 1
        self._record_priority_completed(
            priority,
            auth_candidate=auth_candidate,
        )
        self._last_success_at = time.time()

    async def _finalize_deferred_update(
        self,
        queued: _QueuedWebhookUpdate,
        receipt: UpdateCompletionReceipt,
    ) -> None:
        error = "deferred group reply failed"
        durably_completed = False
        failure_recorded = False
        receipt_task = asyncio.create_task(
            receipt.wait(),
            name=f"webhook-receipt-wait:{queued.update_id}",
        )
        try:
            succeeded = bool(await self._wait_task_with_lease(queued, receipt_task))
            if succeeded:
                persisted = await self._complete_durable_update(queued)
                if persisted:
                    durably_completed = True
                    self._record_durable_success(
                        priority=queued.priority,
                        auth_candidate=queued.auth_candidate,
                    )
                    return
                error = "deferred completion lease was lost"
            self._record_deferred_failure(
                queued.update_id,
                error,
                priority=queued.priority,
                auth_candidate=queued.auth_candidate,
            )
            failure_recorded = True
            await self._release_durable_update(queued, error=error)
        except asyncio.CancelledError:
            # Process shutdown resets unfinished leases on the next start.
            raise
        except Exception:
            if not failure_recorded:
                self._record_deferred_failure(
                    queued.update_id,
                    "deferred finalizer persistence failure",
                    priority=queued.priority,
                    auth_candidate=queued.auth_candidate,
                )
            log.exception(
                "failed to finalize durable deferred webhook update | update=%s",
                queued.update_id,
            )
            try:
                await self._release_durable_update(
                    queued,
                    error="deferred finalizer persistence failure",
                )
            except Exception:
                log.exception(
                    "failed to release durable deferred webhook lease | update=%s",
                    queued.update_id,
                )
        finally:
            if self._durable_inbox_enabled and not durably_completed:
                async with self._dedup_lock:
                    # Accepted-in-memory is not the same as durably completed.
                    # Let the recovery loop execute a failed durable job.
                    self._completed_update_ids.pop(queued.update_id, None)

    async def _finalize_late_update(
        self,
        queued: _QueuedWebhookUpdate,
        receipt: UpdateCompletionReceipt,
        unfinished_task: asyncio.Task[Any],
        *,
        phase: str,
    ) -> None:
        """Finalize an update only after its cancellation-resistant work ends.

        The lease is renewed throughout the wait. This is the critical barrier
        preventing recovery from dispatching a concurrent second copy while an
        old provider/handler coroutine is still performing side effects.
        """

        durably_completed = False
        error = "late webhook update failed"
        try:
            result = await self._wait_task_with_lease(queued, unfinished_task)
            if phase == "dispatch" and isinstance(result, TelegramMethod):
                silent_task = asyncio.create_task(
                    self.dispatcher.silent_call_request(bot=self.bot, result=result),
                    name=f"webhook-late-silent:{queued.update_id}",
                )
                await self._wait_task_with_lease(queued, silent_task)
            receipt.seal()
            if receipt.deferred:
                receipt_task = asyncio.create_task(
                    receipt.wait(),
                    name=f"webhook-late-receipt:{queued.update_id}",
                )
                if not bool(await self._wait_task_with_lease(queued, receipt_task)):
                    raise RuntimeError("deferred group reply processing failed")
            if not await self._complete_durable_update(queued):
                raise RuntimeError("durable webhook completion lease was lost")
            durably_completed = True
            self._record_durable_success(
                priority=queued.priority,
                auth_candidate=queued.auth_candidate,
            )
        except asyncio.CancelledError:
            # Only real server/process shutdown cancels late finalizers. Keep the
            # lease intact; startup recovery will reclaim it after the old
            # process (and therefore the old handler) no longer exists.
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._late_failed_updates += 1
            self._failed_updates += 1
            self._record_priority_failed(
                queued.priority,
                auth_candidate=queued.auth_candidate,
            )
            self._last_failure_at = time.time()
            self._record_business_failure(queued.update_id, error)
            try:
                await self._release_durable_update(queued, error=error)
            except Exception:
                log.exception(
                    "failed to release late durable webhook update | update=%s",
                    queued.update_id,
                )
            log.exception(
                "late durable webhook update failed | update=%s phase=%s",
                queued.update_id,
                phase,
            )
        finally:
            async with self._dedup_lock:
                current_late = self._late_finalizers.get(queued.update_id)
                if current_late is asyncio.current_task():
                    self._late_finalizers.pop(queued.update_id, None)
                    self._late_finalizer_started_at.pop(queued.update_id, None)
                current = self._inflight_updates.get(queued.update_id)
                if current is queued.result:
                    self._inflight_updates.pop(queued.update_id, None)
                if durably_completed:
                    self._completed_update_ids[queued.update_id] = (
                        asyncio.get_running_loop().time()
                    )
                else:
                    self._completed_update_ids.pop(queued.update_id, None)

    @staticmethod
    def _update_timeout_seconds(queued: _QueuedWebhookUpdate) -> float:
        if queued.auth_candidate:
            return _WEBHOOK_AUTH_UPDATE_TIMEOUT_SECONDS
        if queued.priority <= ExecutionPriority.CRITICAL:
            return _WEBHOOK_CRITICAL_UPDATE_TIMEOUT_SECONDS
        if queued.priority <= ExecutionPriority.HIGH:
            return _WEBHOOK_SECURITY_UPDATE_TIMEOUT_SECONDS
        return _WEBHOOK_UPDATE_TIMEOUT_SECONDS

    async def _process_update(self, queued: _QueuedWebhookUpdate) -> bool:
        loop = asyncio.get_running_loop()
        if queued.privileged or queued.auth_candidate:
            # Reserved workers make queueing exceptional.  If a short burst did
            # queue this update, do not turn that wait into an already-expired
            # callback or half-applied permission change.
            deadline = loop.time() + self._update_timeout_seconds(queued)
        else:
            # Ordinary work retains the original end-to-end budget so queue
            # pressure cannot add another full execution window.
            deadline = queued.enqueued_at + _WEBHOOK_UPDATE_TIMEOUT_SECONDS
        key = id(queued)
        self._active_started_at[key] = loop.time()
        if queued.auth_candidate:
            self._active_auth_started_at[key] = loop.time()
        if queued.privileged:
            self._active_privileged_started_at[key] = loop.time()
        if queued.critical:
            self._active_critical_started_at[key] = loop.time()
        elif queued.priority <= ExecutionPriority.HIGH:
            self._active_security_started_at[key] = loop.time()
        receipt = UpdateCompletionReceipt()
        receipt_token = bind_update_completion(receipt)
        durable_handed_off = False
        terminal_error = "update processing did not complete"
        phase = "dispatch"
        try:
            try:
                result = await self._await_stage(
                    self.dispatcher.feed_raw_update(
                        bot=self.bot,
                        update=queued.update,
                    ),
                    deadline=deadline,
                )
                receipt.seal()
            finally:
                reset_update_completion(receipt_token)
            if isinstance(result, TelegramMethod):
                phase = "silent"
                await self._await_stage(
                    self.dispatcher.silent_call_request(
                        bot=self.bot,
                        result=result,
                    ),
                    deadline=deadline,
                )
            if receipt.deferred:
                if self._durable_inbox_enabled:
                    finalizer = asyncio.create_task(
                        self._finalize_deferred_update(queued, receipt),
                        name=f"webhook-deferred:{queued.update_id}",
                    )
                    self._track_deferred_task(finalizer)
                    durable_handed_off = True
                else:
                    deferred_succeeded = await self._await_stage(
                        receipt.wait(),
                        deadline=deadline,
                    )
                    if not deferred_succeeded:
                        raise RuntimeError("deferred group reply processing failed")
            elif self._durable_inbox_enabled:
                if not await self._complete_durable_update(queued):
                    raise RuntimeError("durable webhook completion lease was lost")
                durable_handed_off = True
        except asyncio.CancelledError:
            terminal_error = "update processing cancelled"
            raise
        except _WebhookUpdateDeadlineExceeded as exc:
            if exc.unfinished_task is None:
                receipt.seal()
            terminal_error = "update processing deadline exceeded"
            self._timed_out_updates += 1
            late_handoff = bool(
                self._durable_inbox_enabled
                and exc.unfinished_task is not None
            )
            if not late_handoff:
                self._failed_updates += 1
                self._record_priority_failed(
                    queued.priority,
                    auth_candidate=queued.auth_candidate,
                )
            self._last_failure_at = time.time()
            if self._durable_inbox_enabled and not late_handoff:
                self._record_business_failure(
                    queued.update_id,
                    "processing exceeded "
                    f"{self._update_timeout_seconds(queued):.1f}s",
                )
            else:
                self._set_fatal(
                    f"update processing exceeded "
                    f"{self._update_timeout_seconds(queued):.1f}s"
                )
            log.exception("Telegram webhook update processing timed out")
            if late_handoff:
                assert exc.unfinished_task is not None
                late = asyncio.create_task(
                    self._finalize_late_update(
                        queued,
                        receipt,
                        exc.unfinished_task,
                        phase=phase,
                    ),
                    name=f"webhook-late-finalizer:{queued.update_id}",
                )
                self._late_finalizers[queued.update_id] = late
                self._late_finalizer_started_at[queued.update_id] = time.monotonic()
                self._track_deferred_task(late)
                durable_handed_off = True
            return False
        except Exception as exc:
            receipt.seal()
            terminal_error = str(exc)
            self._failed_updates += 1
            self._record_priority_failed(
                queued.priority,
                auth_candidate=queued.auth_candidate,
            )
            self._last_failure_at = time.time()
            if self._durable_inbox_enabled:
                self._record_business_failure(
                    queued.update_id,
                    str(exc) or type(exc).__name__,
                )
            else:
                self._consecutive_failures += 1
            if (
                not self._durable_inbox_enabled
                and self._consecutive_failures >= _WEBHOOK_FAILURE_THRESHOLD
            ):
                self._set_fatal(
                    "update processing failed "
                    f"{self._consecutive_failures} consecutive times: {exc}"
                )
            log.exception("Telegram webhook update processing failed")
            return False
        else:
            if not (self._durable_inbox_enabled and receipt.deferred):
                self._record_durable_success(
                    priority=queued.priority,
                    auth_candidate=queued.auth_candidate,
                )
            return True
        finally:
            self._active_started_at.pop(key, None)
            self._active_auth_started_at.pop(key, None)
            self._active_privileged_started_at.pop(key, None)
            self._active_critical_started_at.pop(key, None)
            self._active_security_started_at.pop(key, None)
            if self._durable_inbox_enabled and not durable_handed_off:
                try:
                    await self._release_durable_update(
                        queued,
                        error=terminal_error,
                    )
                except Exception:
                    log.exception(
                        "failed to release durable webhook update | update=%s",
                        queued.update_id,
                    )

    async def _worker(
        self,
        *,
        priority: ExecutionPriority,
        auth_candidate: bool = False,
    ) -> None:
        while True:
            queued = await self.queue.get(
                priority=priority,
                auth_candidate=auth_candidate,
            )
            succeeded = False
            try:
                if priority <= ExecutionPriority.CRITICAL:
                    with privileged_request_scope():
                        succeeded = await self._process_update(queued)
                elif priority <= ExecutionPriority.HIGH:
                    with privileged_request_scope(background=True):
                        succeeded = await self._process_update(queued)
                elif auth_candidate:
                    # Fast authorization needs a reserved outbound path too,
                    # but remains below the trusted CRITICAL operator reserve.
                    # The lane is independently bounded, so unauthenticated
                    # command spam cannot fan out without limit.
                    with privileged_request_scope(background=True):
                        succeeded = await self._process_update(queued)
                else:
                    succeeded = await self._process_update(queued)
            finally:
                finish_task = asyncio.create_task(
                    self._finish_queued_update(queued, succeeded=succeeded),
                    name=f"webhook-finish:{queued.update_id}",
                )
                self._track_deferred_task(finish_task)
                try:
                    await asyncio.shield(finish_task)
                finally:
                    # Queue accounting must remain balanced even when shutdown
                    # cancels this worker while its dedup/result bookkeeping is
                    # still completing in the shielded child task.
                    self.queue.task_done(queued)

    def fatal_issue(self) -> str | None:
        self._promote_stale_tasks_to_fatal()
        return self._fatal_error or self._business_failure_issue

    def _promote_stale_tasks_to_fatal(self) -> None:
        if self._fatal_error is not None:
            return
        oldest_orphan_age = self._oldest_orphan_age()
        if oldest_orphan_age >= _WEBHOOK_ORPHAN_FATAL_AGE_SECONDS:
            self._set_fatal(
                "oldest orphaned webhook update task is "
                f"{oldest_orphan_age:.1f}s old"
            )
            return
        oldest_late_finalizer_age = self._oldest_late_finalizer_age()
        if oldest_late_finalizer_age >= _WEBHOOK_LATE_FINALIZER_FATAL_AGE_SECONDS:
            self._set_fatal(
                "oldest webhook late finalizer is "
                f"{oldest_late_finalizer_age:.1f}s old"
            )

    def _oldest_queue_age(
        self,
        *,
        priority: ExecutionPriority | None = None,
        auth_candidate: bool = False,
    ) -> float:
        items = self.queue.items(
            priority=priority,
            auth_candidate=auth_candidate,
        )
        if not items:
            return 0.0
        enqueued_at = min(item.enqueued_at for item in items)
        return max(0.0, time.monotonic() - enqueued_at)

    def _oldest_active_age(
        self,
        *,
        priority: ExecutionPriority | None = None,
        auth_candidate: bool = False,
    ) -> float:
        if auth_candidate:
            active = self._active_auth_started_at
        elif priority is None:
            active = self._active_started_at
        elif priority <= ExecutionPriority.CRITICAL:
            active = self._active_critical_started_at
        elif priority <= ExecutionPriority.HIGH:
            active = self._active_security_started_at
        else:
            privileged_keys = self._active_privileged_started_at
            auth_keys = self._active_auth_started_at
            active = {
                key: started_at
                for key, started_at in self._active_started_at.items()
                if key not in privileged_keys and key not in auth_keys
            }
        if not active:
            return 0.0
        return max(0.0, time.monotonic() - min(active.values()))

    def _oldest_orphan_age(self) -> float:
        active = {
            task: started_at
            for task, started_at in self._orphaned_started_at.items()
            if not task.done()
        }
        if not active:
            return 0.0
        return max(0.0, time.monotonic() - min(active.values()))

    def _oldest_late_finalizer_age(self) -> float:
        active_started = [
            started_at
            for update_id, started_at in self._late_finalizer_started_at.items()
            if (
                (task := self._late_finalizers.get(update_id)) is not None
                and not task.done()
            )
        ]
        if not active_started:
            return 0.0
        return max(0.0, time.monotonic() - min(active_started))

    def delivery_issue(self) -> str | None:
        if self._stopped:
            return "webhook processor is stopped"
        fatal = self.fatal_issue()
        if fatal is not None:
            return fatal
        if self._workers_started:
            ordinary_alive = sum(
                not worker.done() for worker in self._ordinary_workers
            )
            auth_alive = sum(not worker.done() for worker in self._auth_workers)
            security_alive = sum(
                not worker.done() for worker in self._security_workers
            )
            critical_alive = sum(
                not worker.done() for worker in self._critical_workers
            )
            if ordinary_alive != self.worker_count:
                return (
                    f"only {ordinary_alive}/{self.worker_count} ordinary "
                    "webhook workers are alive"
                )
            if auth_alive != self.auth_worker_count:
                return (
                    f"only {auth_alive}/{self.auth_worker_count} authentication "
                    "webhook workers are alive"
                )
            if security_alive != self.security_worker_count:
                return (
                    f"only {security_alive}/{self.security_worker_count} "
                    "security webhook workers are alive"
                )
            if critical_alive != self.critical_worker_count:
                return (
                    f"only {critical_alive}/{self.critical_worker_count} "
                    "critical webhook workers are alive"
                )
        if self.queue.critical_full():
            return "critical webhook update queue is saturated"
        if self.queue.security_full():
            return "security webhook update queue is saturated"
        if self.queue.ordinary_full():
            return "ordinary webhook update queue is saturated"
        critical_queue_age = self._oldest_queue_age(
            priority=ExecutionPriority.CRITICAL
        )
        if critical_queue_age > _WEBHOOK_CRITICAL_QUEUE_WARN_SECONDS:
            return (
                "oldest critical queued update is "
                f"{critical_queue_age:.1f}s old"
            )
        security_queue_age = self._oldest_queue_age(
            priority=ExecutionPriority.HIGH
        )
        if security_queue_age > _WEBHOOK_SECURITY_QUEUE_WARN_SECONDS:
            return (
                "oldest security queued update is "
                f"{security_queue_age:.1f}s old"
            )
        oldest_queue_age = self._oldest_queue_age(
            priority=ExecutionPriority.NORMAL
        )
        if oldest_queue_age > _WEBHOOK_UPDATE_TIMEOUT_SECONDS:
            return f"oldest queued update is {oldest_queue_age:.1f}s old"
        critical_active_age = self._oldest_active_age(
            priority=ExecutionPriority.CRITICAL
        )
        if (
            critical_active_age
            > _WEBHOOK_CRITICAL_UPDATE_TIMEOUT_SECONDS
            + _WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS
        ):
            return (
                "oldest critical active update is "
                f"{critical_active_age:.1f}s old"
            )
        security_active_age = self._oldest_active_age(
            priority=ExecutionPriority.HIGH
        )
        if (
            security_active_age
            > _WEBHOOK_SECURITY_UPDATE_TIMEOUT_SECONDS
            + _WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS
        ):
            return (
                "oldest security active update is "
                f"{security_active_age:.1f}s old"
            )
        auth_active_age = self._oldest_active_age(auth_candidate=True)
        if (
            auth_active_age
            > _WEBHOOK_AUTH_UPDATE_TIMEOUT_SECONDS
            + _WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS
        ):
            return (
                "oldest authentication-candidate active update is "
                f"{auth_active_age:.1f}s old"
            )
        oldest_active_age = self._oldest_active_age(
            priority=ExecutionPriority.NORMAL
        )
        if (
            oldest_active_age
            > _WEBHOOK_UPDATE_TIMEOUT_SECONDS + _WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS
        ):
            return f"oldest active update is {oldest_active_age:.1f}s old"
        return None

    def health_snapshot(self) -> dict[str, Any]:
        issue = self.delivery_issue()
        return {
            "ok": issue is None,
            "issue": issue,
            "stopped": self._stopped,
            "workers_configured": (
                self.worker_count
                + self.auth_worker_count
                + self.security_worker_count
                + self.critical_worker_count
            ),
            "workers_alive": sum(not worker.done() for worker in self._workers),
            "ordinary_workers_configured": self.worker_count,
            "ordinary_workers_alive": sum(
                not worker.done() for worker in self._ordinary_workers
            ),
            "auth_workers_configured": self.auth_worker_count,
            "auth_workers_alive": sum(
                not worker.done() for worker in self._auth_workers
            ),
            "privileged_workers_configured": (
                self.security_worker_count + self.critical_worker_count
            ),
            "privileged_workers_alive": sum(
                not worker.done()
                for worker in (*self._security_workers, *self._critical_workers)
            ),
            "security_workers_configured": self.security_worker_count,
            "security_workers_alive": sum(
                not worker.done() for worker in self._security_workers
            ),
            "critical_workers_configured": self.critical_worker_count,
            "critical_workers_alive": sum(
                not worker.done() for worker in self._critical_workers
            ),
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            "ordinary_queue_size": self.queue.ordinary_qsize(),
            "ordinary_queue_capacity": self.queue.ordinary_maxsize,
            "auth_queue_size": self.queue.auth_qsize(),
            "auth_queue_capacity": self.queue.auth_maxsize,
            "auth_queue_saturated": self.queue.auth_full(),
            "privileged_queue_size": (
                self.queue.security_qsize() + self.queue.critical_qsize()
            ),
            "privileged_queue_capacity": (
                self.queue.security_maxsize + self.queue.critical_maxsize
            ),
            "security_queue_size": self.queue.security_qsize(),
            "security_queue_capacity": self.queue.security_maxsize,
            "critical_queue_size": self.queue.critical_qsize(),
            "critical_queue_capacity": self.queue.critical_maxsize,
            "oldest_queue_age_seconds": round(self._oldest_queue_age(), 3),
            "oldest_ordinary_queue_age_seconds": round(
                self._oldest_queue_age(priority=ExecutionPriority.NORMAL),
                3,
            ),
            "oldest_auth_queue_age_seconds": round(
                self._oldest_queue_age(
                    priority=ExecutionPriority.NORMAL,
                    auth_candidate=True,
                ),
                3,
            ),
            "oldest_privileged_queue_age_seconds": round(
                max(
                    self._oldest_queue_age(priority=ExecutionPriority.CRITICAL),
                    self._oldest_queue_age(priority=ExecutionPriority.HIGH),
                ),
                3,
            ),
            "oldest_critical_queue_age_seconds": round(
                self._oldest_queue_age(priority=ExecutionPriority.CRITICAL),
                3,
            ),
            "oldest_security_queue_age_seconds": round(
                self._oldest_queue_age(priority=ExecutionPriority.HIGH),
                3,
            ),
            "active_updates": len(self._active_started_at),
            "oldest_active_age_seconds": round(self._oldest_active_age(), 3),
            "active_auth_updates": len(self._active_auth_started_at),
            "oldest_auth_active_age_seconds": round(
                self._oldest_active_age(auth_candidate=True),
                3,
            ),
            "active_privileged_updates": len(
                self._active_privileged_started_at
            ),
            "oldest_privileged_active_age_seconds": round(
                max(
                    self._oldest_active_age(priority=ExecutionPriority.CRITICAL),
                    self._oldest_active_age(priority=ExecutionPriority.HIGH),
                ),
                3,
            ),
            "active_critical_updates": len(self._active_critical_started_at),
            "oldest_critical_active_age_seconds": round(
                self._oldest_active_age(priority=ExecutionPriority.CRITICAL),
                3,
            ),
            "active_security_updates": len(self._active_security_started_at),
            "oldest_security_active_age_seconds": round(
                self._oldest_active_age(priority=ExecutionPriority.HIGH),
                3,
            ),
            "orphaned_update_tasks": len(self._orphaned_tasks),
            "oldest_orphaned_update_age_seconds": round(
                self._oldest_orphan_age(),
                3,
            ),
            "deferred_reply_jobs": len(self._deferred_tasks),
            "late_finalizers": len(self._late_finalizers),
            "oldest_late_finalizer_age_seconds": round(
                self._oldest_late_finalizer_age(),
                3,
            ),
            "accepted_updates": self._accepted_updates,
            "accepted_auth_updates": self._accepted_auth_updates,
            "accepted_privileged_updates": self._accepted_privileged_updates,
            "accepted_critical_updates": self._accepted_critical_updates,
            "accepted_security_updates": self._accepted_security_updates,
            "demoted_untrusted_critical_updates": (
                self._demoted_untrusted_critical_updates
            ),
            "demoted_untrusted_auth_updates": (
                self._demoted_untrusted_auth_updates
            ),
            "completed_updates": self._completed_updates,
            "completed_auth_updates": self._completed_auth_updates,
            "completed_privileged_updates": self._completed_privileged_updates,
            "completed_critical_updates": self._completed_critical_updates,
            "completed_security_updates": self._completed_security_updates,
            "failed_updates": self._failed_updates,
            "failed_auth_updates": self._failed_auth_updates,
            "failed_privileged_updates": self._failed_privileged_updates,
            "failed_critical_updates": self._failed_critical_updates,
            "failed_security_updates": self._failed_security_updates,
            "timed_out_updates": self._timed_out_updates,
            "retried_updates": self._retried_updates,
            "dropped_updates": self._dropped_updates,
            "deferred_failed_updates": self._deferred_failed_updates,
            "late_failed_updates": self._late_failed_updates,
            "dead_lettered_updates": self._dead_lettered_updates,
            "retry_scheduled_updates": self._retry_scheduled_updates,
            "cleaned_inbox_updates": self._cleaned_inbox_updates,
            "recovery_failures": self._recovery_failures,
            "last_business_error": self._last_business_error,
            "consecutive_distinct_failures": len(
                self._failed_update_ids_since_success
            ),
            "last_recovery_error": self._last_recovery_error,
            "last_success_at": self._last_success_at,
            "last_failure_at": self._last_failure_at,
        }

    async def _discard_queued_updates(self) -> int:
        discarded = 0
        while True:
            try:
                queued = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                discarded += 1
                try:
                    await self._release_durable_update(
                        queued,
                        error="webhook queue discarded during shutdown",
                    )
                except Exception:
                    log.exception(
                        "failed to release discarded durable webhook update | update=%s",
                        queued.update_id,
                    )
                await self._finish_queued_update(queued, succeeded=False)
                self.queue.task_done(queued)
        self._dropped_updates += discarded
        return discarded

    async def _stop_impl(self) -> None:
        self._stopped = True
        self._quiescing = True
        if self._recovery_task is not None:
            recovery_task = self._recovery_task
            recovery_task.cancel()
            done, pending = await asyncio.wait(
                {recovery_task},
                timeout=_WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS,
            )
            if recovery_task in done:
                self._consume_task_result(recovery_task)
            elif pending:
                self._track_orphan(recovery_task)
            self._recovery_task = None
        if self._workers:
            try:
                await asyncio.wait_for(
                    self.queue.join(),
                    timeout=_WEBHOOK_DRAIN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                discarded = await self._discard_queued_updates()
                self._set_fatal(
                    "webhook queue drain timed out; "
                    f"rejected {discarded} queued updates"
                )
                log.error(
                    "Webhook queue drain timed out after %.1fs; discarded=%d",
                    _WEBHOOK_DRAIN_TIMEOUT_SECONDS,
                    discarded,
                )
            for worker in self._workers:
                worker.cancel()
            done, pending = await asyncio.wait(
                self._workers,
                timeout=_WEBHOOK_WORKER_STOP_TIMEOUT_SECONDS,
            )
            for task in done:
                self._consume_task_result(task)
            if pending:
                log.error(
                    "%d webhook workers ignored cancellation during shutdown",
                    len(pending),
                )
                for task in pending:
                    task.add_done_callback(self._consume_task_result)
            self._workers.clear()
            self._ordinary_workers.clear()
            self._auth_workers.clear()
            self._security_workers.clear()
            self._critical_workers.clear()

        outstanding = list(self._processing_tasks | self._orphaned_tasks)
        for task in outstanding:
            task.cancel()
        if outstanding:
            done, pending = await asyncio.wait(
                outstanding,
                timeout=_WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS,
            )
            for task in done:
                self._consume_task_result(task)
            if pending:
                log.error(
                    "%d webhook update tasks ignored cancellation during shutdown",
                    len(pending),
                )
                for task in pending:
                    task.add_done_callback(self._consume_task_result)

        deferred = {task for task in self._deferred_tasks if not task.done()}
        if deferred:
            done, pending = await asyncio.wait(
                deferred,
                timeout=_WEBHOOK_WORKER_STOP_TIMEOUT_SECONDS,
            )
            for task in done:
                self._consume_task_result(task)
            for task in pending:
                task.cancel()
            if pending:
                done_after_cancel, still_pending = await asyncio.wait(
                    pending,
                    timeout=_WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS,
                )
                for task in done_after_cancel:
                    self._consume_task_result(task)
                for task in still_pending:
                    task.add_done_callback(self._consume_task_result)
                if still_pending:
                    log.error(
                        "%d deferred webhook finalizers ignored cancellation",
                        len(still_pending),
                    )

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_impl(),
                name="telegram-webhook-processor-stop",
            )
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            self._stop_task.cancel()
            await asyncio.wait({self._stop_task}, timeout=1.0)
            raise

    async def quiesce(self, *, timeout_seconds: float = 20.0) -> bool:
        """Stop new/recovery ingestion and drain dispatched handlers only.

        Deferred completion finalizers deliberately remain alive so their
        explicit owners (privileged jobs, pending replies and memory writes)
        can finish before :meth:`stop` closes the processor.
        """

        self._quiescing = True
        recovery_task = self._recovery_task
        self._recovery_task = None
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
            done, _pending = await asyncio.wait(
                {recovery_task},
                timeout=_WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS,
            )
            if recovery_task in done:
                self._consume_task_result(recovery_task)
            else:
                self._track_orphan(recovery_task)
        try:
            await asyncio.wait_for(
                self.queue.join(),
                timeout=max(0.01, float(timeout_seconds)),
            )
            return True
        except TimeoutError:
            self._set_fatal("Telegram update processor quiesce timed out")
            return False


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
        mark_privileged_operator(
            int(settings.super_admin_id or 0),
            ttl_seconds=365 * 24 * 60 * 60.0,
        )
        self.webhook_route_error: str | None = None
        self._webhook_accepting_updates = False
        self._delivery_mode = "starting"
        self._webhook_processor: _WebhookUpdateQueue | None = None
        self._webhook_request_tasks: set[asyncio.Task[Any]] = set()
        self._runner: web.AppRunner | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._resource_health_provider = self._update_processor_health_snapshot
        # Production owns one server. Clearing a stale registration here also
        # prevents reload/test instances from retaining an old queue graph.
        unregister_resource_health_provider("telegram_update_processor")
        self._completed_verifications: dict[
            tuple[int, int], tuple[float, _VerificationFingerprint]
        ] = {}
        # Keep short-lived idempotence only after the row is consumed. The
        # fingerprint prevents a legacy/recovered database from treating a
        # different issuance that happens to reuse an ID as completed.
        self._active_verifications: dict[
            tuple[int, int], _ActiveVerification
        ] = {}
        self._verification_upstream_slots = asyncio.Semaphore(
            _VERIFICATION_MAX_CONCURRENT_UPSTREAM
        )
        self._verification_upstream_admission_lock = asyncio.Lock()
        self._verification_rate_events: OrderedDict[
            tuple[str, str], deque[float]
        ] = OrderedDict()

    async def _try_acquire_verification_upstream(self) -> bool:
        """Atomically claim an upstream slot without joining its wait queue."""

        async with self._verification_upstream_admission_lock:
            if self._verification_upstream_slots.locked():
                return False
            # The lock makes the capacity check and decrement one admission
            # transaction. Since capacity is known to be available, acquire()
            # completes immediately and never adds this request as a waiter.
            await self._verification_upstream_slots.acquire()
            return True

    def _verification_rate_limited(
        self,
        *,
        user_id: int | None,
        remote_ip: str,
    ) -> bool:
        """Apply a bounded sliding-window limit before DB/provider work."""

        now = time.monotonic()
        cutoff = now - _VERIFICATION_RATE_WINDOW_SECONDS
        keyed_limits: list[tuple[tuple[str, str], int]] = []
        if user_id is not None and int(user_id) > 0:
            keyed_limits.append(
                (("user", str(int(user_id))), _VERIFICATION_USER_RATE_LIMIT)
            )
        if remote_ip:
            keyed_limits.append((("ip", remote_ip), _VERIFICATION_IP_RATE_LIMIT))
        if not keyed_limits:
            return False

        buckets: list[deque[float]] = []
        for key, limit in keyed_limits:
            bucket = self._verification_rate_events.get(key)
            if bucket is None:
                while len(self._verification_rate_events) >= _VERIFICATION_RATE_CACHE_MAX_ENTRIES:
                    self._verification_rate_events.popitem(last=False)
                bucket = deque()
                self._verification_rate_events[key] = bucket
            else:
                self._verification_rate_events.move_to_end(key)
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return True
            buckets.append(bucket)

        for bucket in buckets:
            bucket.append(now)
        return False

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
        app = web.Application(client_max_size=_WEB_MAX_REQUEST_BYTES)
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
        if self.webhook_dispatcher is not None:
            processor = _WebhookUpdateQueue(
                dispatcher=self.webhook_dispatcher,
                bot=self.bot,
                session_factory=(
                    self.session_factory if callable(self.session_factory) else None
                ),
                secret_token=self.webhook_secret,
                worker_count=WEBHOOK_MAX_CONCURRENT_UPDATES,
                auth_worker_count=WEBHOOK_AUTH_CONCURRENT_UPDATES,
                critical_worker_count=WEBHOOK_CRITICAL_CONCURRENT_UPDATES,
                security_worker_count=WEBHOOK_SECURITY_CONCURRENT_UPDATES,
                critical_queue_capacity=WEBHOOK_CRITICAL_QUEUE_CAPACITY,
                security_queue_capacity=WEBHOOK_SECURITY_QUEUE_CAPACITY,
                auth_queue_capacity=WEBHOOK_AUTH_QUEUE_CAPACITY,
            )
            self._webhook_processor = processor
            register_resource_health_provider(
                "telegram_update_processor",
                self._resource_health_provider,
            )

            async def close_webhook_handler(_app: web.Application) -> None:
                await self.disable_webhook_route()
                await self._stop_webhook_processor()

            app.on_shutdown.append(close_webhook_handler)

        if self._webhook_processor is not None and self.webhook_path:
            processor = self._webhook_processor
            try:
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

                app.router.add_post(self.webhook_path, handle_webhook)
            except Exception as exc:
                self.webhook_route_error = f"Webhook HTTP 路由注册失败：{exc}"
                log.error(
                    "%s；该路由已禁用，将自动降级为轮询。",
                    self.webhook_route_error,
                    exc_info=True,
                )
        return app

    def enable_webhook_route(self) -> None:
        self._delivery_mode = "registering"
        self._webhook_accepting_updates = True

    def mark_webhook_active(self) -> None:
        if self._webhook_accepting_updates and self._delivery_mode != "stopping":
            self._delivery_mode = "webhook"

    async def disable_webhook_route(self) -> None:
        self._webhook_accepting_updates = False
        if self._delivery_mode != "stopping":
            self._delivery_mode = "switching"
        request_tasks = [
            task
            for task in self._webhook_request_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        if request_tasks:
            done, pending = await asyncio.wait(
                request_tasks,
                timeout=_WEBHOOK_REQUEST_DRAIN_TIMEOUT_SECONDS,
            )
            if pending:
                log.error(
                    "%d webhook HTTP requests exceeded %.1fs shutdown grace",
                    len(pending),
                    _WEBHOOK_REQUEST_DRAIN_TIMEOUT_SECONDS,
                )
                for task in pending:
                    task.cancel()
                    task.add_done_callback(_WebhookUpdateQueue._consume_task_result)
            for task in done:
                _WebhookUpdateQueue._consume_task_result(task)
        if (
            self._webhook_processor is not None
            and not self._webhook_processor._durable_inbox_enabled
        ):
            await self._webhook_processor.stop()

    async def _stop_webhook_processor(self) -> None:
        if self._webhook_processor is not None:
            await self._webhook_processor.stop()

    async def stop_update_processor(self) -> None:
        """Stop durable update consumption before reply/LLM shutdown flushes."""

        await self._stop_webhook_processor()

    async def quiesce_update_processor(self) -> bool:
        if self._webhook_processor is None:
            return True
        return await self._webhook_processor.quiesce()

    async def start_update_processor(self) -> None:
        """Start the transport-neutral durable Telegram inbox consumer.

        The dispatcher startup hook must have completed before this method is
        called. Keeping it separate from the HTTP listener prevents recovered
        rows from reaching handlers while router startup is still in progress.
        """

        if self._webhook_processor is None:
            raise RuntimeError("durable Telegram update processor is unavailable")
        await self._webhook_processor.start_recovery()

    async def accept_polling_update(self, update: Any) -> int:
        """Durably accept one getUpdates item before polling advances offset."""

        if self._webhook_processor is None:
            raise RuntimeError("durable Telegram update processor is unavailable")
        if isinstance(update, dict):
            payload = dict(update)
        else:
            model_dump = getattr(update, "model_dump", None)
            if not callable(model_dump):
                raise TypeError("unsupported Telegram update object")
            # exclude_unset drops aiogram's Default(...) sentinel fields (e.g.
            # LinkPreviewOptions.is_disabled), which exclude_none keeps and
            # pydantic cannot serialize; incoming updates only carry set fields.
            payload = model_dump(mode="json", exclude_none=True, exclude_unset=True)
        if not isinstance(payload, dict):
            raise TypeError("Telegram update serialization did not return an object")
        return await self._webhook_processor.accept_durable_update(payload)

    def mark_polling_active(self) -> None:
        if self._delivery_mode != "stopping":
            self._delivery_mode = "polling"

    def webhook_runtime_failure(self) -> str | None:
        """Return only confirmed local failures suitable for transport fallback."""
        if not self._webhook_accepting_updates:
            return None
        if self._webhook_processor is None:
            return "webhook route is enabled without an update processor"
        return self._webhook_processor.fatal_issue()

    def _update_processor_health_snapshot(self) -> dict[str, Any]:
        processor = self._webhook_processor
        if processor is None:
            return {
                "ok": self.webhook_dispatcher is None,
                "fatal": False,
                "issue": (
                    None
                    if self.webhook_dispatcher is None
                    else "Telegram update processor is not initialized"
                ),
            }
        snapshot = processor.health_snapshot()
        snapshot["fatal"] = processor.fatal_issue() is not None
        return snapshot

    async def start(self) -> None:
        app = self.build_app()
        self._runner = web.AppRunner(
            app,
            shutdown_timeout=_WEB_SERVER_SHUTDOWN_TIMEOUT_SECONDS,
        )
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

    async def _stop_impl(self) -> None:
        self._delivery_mode = "stopping"
        # Stop accepting/drain Telegram updates explicitly before aiohttp starts
        # its own shutdown sequence. This keeps the processor's bounded budget
        # separate from AppRunner.shutdown_timeout and guarantees no update task
        # survives into Bot-session/engine teardown.
        await self.disable_webhook_route()
        await self._stop_webhook_processor()
        try:
            if self._runner is not None:
                runner = self._runner
                self._runner = None
                cleanup_task = asyncio.create_task(
                    runner.cleanup(),
                    name="miniapp-web-cleanup",
                )
                done, _pending = await asyncio.wait(
                    {cleanup_task},
                    timeout=_WEB_SERVER_SHUTDOWN_TIMEOUT_SECONDS,
                )
                if cleanup_task not in done:
                    log.error(
                        "Mini App web server shutdown exceeded %.1fs",
                        _WEB_SERVER_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                    cleanup_task.cancel()
                    done_after_cancel, pending_after_cancel = await asyncio.wait(
                        {cleanup_task},
                        timeout=_WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS,
                    )
                    for task in done_after_cancel:
                        _WebhookUpdateQueue._consume_task_result(task)
                    for task in pending_after_cancel:
                        task.add_done_callback(_WebhookUpdateQueue._consume_task_result)
                else:
                    try:
                        await cleanup_task
                    except asyncio.CancelledError:
                        log.error("Mini App web server cleanup was cancelled")
                    except Exception:
                        log.exception("Mini App web server cleanup failed")
        finally:
            unregister_resource_health_provider(
                "telegram_update_processor",
                self._resource_health_provider,
            )
            await _flush_public_body_tasks()
            await flush_member_identity_tasks()

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_impl(),
                name="miniapp-web-server-stop",
            )
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            self._stop_task.cancel()
            await asyncio.wait({self._stop_task}, timeout=1.0)
            raise

    async def handle_health(self, _request: web.Request) -> web.Response:
        processor = self._webhook_processor
        webhook_health = processor.health_snapshot() if processor is not None else None
        resources = resource_health_snapshot()
        webhook_issue = (
            processor.delivery_issue()
            if processor is not None
            and (
                self._webhook_accepting_updates
                or (
                    processor._durable_inbox_enabled
                    and not processor._stopped
                )
            )
            else (
                "webhook route is enabled without an update processor"
                if self._webhook_accepting_updates
                else None
            )
        )
        ok = (
            webhook_issue is None
            and self._delivery_mode in {"webhook", "polling"}
            and bool(resources.get("ok", False))
        )
        return web.json_response(
            {
                "ok": ok,
                "delivery_mode": self._delivery_mode,
                "webhook_accepting_updates": self._webhook_accepting_updates,
                "webhook": webhook_health,
                "resources": resources,
                "config_revision": self.runtime_config.revision
                if self.runtime_config is not None
                else None,
            },
            status=200 if ok else 503,
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
        if raw_provider and raw_provider.strip().lower() not in VERIFICATION_PROVIDERS:
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
        if not turnstile_verification_configured(self.settings, provider):
            return web.json_response(
                {"ok": False, "error": "验证服务暂时不可用，请联系管理员"}, status=503
            )
        turnstile_script = (
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" '
            "async defer></script>"
        )
        hcaptcha_script = (
            '<script src="https://js.hcaptcha.com/1/api.js" async defer></script>'
        )
        intro_text = "完成下方人机验证后即可在群内发言。"
        if provider == COMBINED_VERIFICATION_PROVIDER:
            turnstile_key, _ = verification_keys_for_provider(
                self.settings, "turnstile"
            )
            hcaptcha_key, _ = verification_keys_for_provider(self.settings, "hcaptcha")
            script_tags = turnstile_script + "\n" + hcaptcha_script
            intro_text = (
                "本群需要依次完成两项人机验证：请先完成第 1 步 Turnstile，"
                "再完成第 2 步 hCaptcha，两项都通过后即可在群内发言。"
            )
            widget_html = (
                '<div class="challenge-steps">'
                '<div class="challenge-step" id="step-turnstile">'
                '<p class="challenge-step-title">第 1 步 · Cloudflare Turnstile</p>'
                '<div class="challenge-step-widget">'
                '<div class="cf-turnstile" data-size="flexible" data-sitekey="{turnstile_key}" '
                'data-callback="onTurnstileSuccess" data-error-callback="onChallengeError" '
                'data-expired-callback="onTurnstileExpired"></div>'
                "</div></div>"
                '<div class="challenge-step locked" id="step-hcaptcha">'
                '<p class="challenge-step-title">第 2 步 · hCaptcha</p>'
                '<div class="challenge-step-widget">'
                '<div class="h-captcha" data-size="compact" data-sitekey="{hcaptcha_key}" '
                'data-callback="onHcaptchaSuccess" data-error-callback="onChallengeError" '
                'data-expired-callback="onHcaptchaExpired"></div>'
                "</div></div>"
                "</div>"
            ).format(
                turnstile_key=html.escape(turnstile_key),
                hcaptcha_key=html.escape(hcaptcha_key),
            )
        elif provider == "hcaptcha":
            site_key, _secret_key = verification_keys_for_provider(
                self.settings, provider
            )
            script_tags = hcaptcha_script
            widget_html = (
                '<div class="h-captcha" data-size="compact" data-sitekey="{site_key}" '
                'data-callback="onChallengeSuccess" data-error-callback="onChallengeError" '
                'data-expired-callback="onChallengeExpired"></div>'
            ).format(site_key=html.escape(site_key))
        else:
            site_key, _secret_key = verification_keys_for_provider(
                self.settings, provider
            )
            script_tags = turnstile_script
            widget_html = (
                '<div class="cf-turnstile" data-size="flexible" data-sitekey="{site_key}" '
                'data-callback="onChallengeSuccess" data-error-callback="onChallengeError" '
                'data-expired-callback="onChallengeExpired"></div>'
            ).format(site_key=html.escape(site_key))
        page = _PAGE_TEMPLATE.format(
            provider=provider,
            config_tag=_verification_config_tag(self.settings, provider),
            verification_id=verification_id,
            script_tags=script_tags,
            intro_text=intro_text,
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
            body = await _read_json_body_hard(
                request,
                timeout_seconds=_PUBLIC_JSON_BODY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return web.json_response(
                {"ok": False, "error": "请求体读取超时"},
                status=408,
            )
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        page_provider = str(body.get("provider") or "turnstile").strip().lower()
        # The combined provider submits one token per solved challenge; base
        # providers keep the single challenge_token field (with the legacy
        # turnstile_token alias).
        if page_provider == COMBINED_VERIFICATION_PROVIDER:
            subprovider_tokens = {
                "turnstile": str(body.get("turnstile_token") or ""),
                "hcaptcha": str(body.get("hcaptcha_token") or ""),
            }
        else:
            challenge_token = str(
                body.get("challenge_token") or body.get("turnstile_token") or ""
            )
            subprovider_tokens = {
                "hcaptcha"
                if page_provider == "hcaptcha"
                else "turnstile": challenge_token
            }
        page_config_tag = str(body.get("config_tag") or "").strip()
        raw_verification_id = body.get("verification_id") or 0
        raw_verification_id_text = str(raw_verification_id).strip()
        init_data = str(body.get("init_data") or "")
        if (
            len(page_provider) > _VERIFICATION_MAX_PROVIDER_CHARS
            or len(page_config_tag) > _VERIFICATION_MAX_CONFIG_TAG_CHARS
            or len(raw_verification_id_text) > _VERIFICATION_MAX_ID_CHARS
            or len(init_data) > _VERIFICATION_MAX_INIT_DATA_CHARS
            or any(
                len(token) > _VERIFICATION_MAX_CHALLENGE_TOKEN_CHARS
                for token in subprovider_tokens.values()
            )
        ):
            return web.json_response(
                {"ok": False, "error": "验证参数过长"},
                status=413,
            )
        remote_ip = _client_public_ip(request)
        if self._verification_rate_limited(user_id=None, remote_ip=remote_ip):
            log.info(
                "verification rejected | reason=ip_rate_limited ip=%s",
                remote_ip,
            )
            return web.json_response(
                {"ok": False, "error": "请求过于频繁，请稍后重试"},
                status=429,
                headers={"Retry-After": str(int(_VERIFICATION_RATE_WINDOW_SECONDS))},
            )
        try:
            verification_id = int(raw_verification_id_text or 0)
        except (TypeError, ValueError):
            verification_id = -1
        if (
            not all(subprovider_tokens.values())
            or not init_data
            or verification_id < 0
        ):
            return web.json_response({"ok": False, "error": "缺少验证参数"}, status=400)

        # Telegram signs initData with HMAC(bot_token); a valid signature
        # proves the request comes from this bot's Mini App session and pins
        # the user identity.
        try:
            web_app_data = safe_parse_webapp_init_data(
                token=self.bot.token, init_data=init_data
            )
            auth_timestamp = _auth_timestamp(web_app_data.auth_date)
        except (ValueError, AttributeError, TypeError, OverflowError, OSError):
            log.info("verification rejected | reason=bad_init_data_signature")
            return web.json_response(
                {"ok": False, "error": "会话校验失败，请从 Telegram 重新打开"}, status=403
            )
        init_data_age = time.time() - auth_timestamp
        if init_data_age > MAX_INIT_DATA_AGE_SECONDS:
            log.info(
                "verification rejected | reason=expired_init_data age=%.1f",
                init_data_age,
            )
            return web.json_response(
                {"ok": False, "error": "会话已过期，请从 Telegram 重新打开"},
                status=403,
            )
        if init_data_age < -MAX_INIT_DATA_FUTURE_SECONDS:
            log.info(
                "verification rejected | reason=future_init_data age=%.1f",
                init_data_age,
            )
            return web.json_response(
                {"ok": False, "error": "会话时间异常，请从 Telegram 重新打开"},
                status=403,
            )
        web_user = web_app_data.user
        if web_user is None:
            return web.json_response(
                {"ok": False, "error": "会话校验失败，请从 Telegram 重新打开"}, status=403
            )
        user_id = int(web_user.id)
        if self._verification_rate_limited(user_id=user_id, remote_ip=""):
            log.info(
                "verification rejected | reason=rate_limited user=%s ip=%s",
                user_id,
                remote_ip or "-",
            )
            return web.json_response(
                {"ok": False, "error": "请求过于频繁，请稍后重试"},
                status=429,
                headers={"Retry-After": str(int(_VERIFICATION_RATE_WINDOW_SECONDS))},
            )

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
        required_subproviders = verification_subproviders(provider)
        if set(subprovider_tokens) != set(required_subproviders):
            return web.json_response({"ok": False, "error": "缺少验证参数"}, status=400)
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

        if joined_existing_active:
            # Widget tokens are single-use for both providers. A concurrent
            # duplicate submit must join the first terminal future before any
            # second siteverify call, otherwise Turnstile commonly reports
            # timeout-or-duplicate and a genuine solve is returned as 403.
            release_active()
            if await self._wait_for_completed_verification(active):
                async with self.session_factory() as session:
                    still_pending = await get_pending_verification_by_id_for_user(
                        session,
                        verification_id,
                        user_id,
                    )
                if still_pending is None:
                    return web.json_response({"ok": True})
            return web.json_response(
                {
                    "ok": False,
                    "error": "验证正在处理中或未完成，请稍后重试",
                },
                status=409,
            )

        if not await self._try_acquire_verification_upstream():
            release_active()
            return web.json_response(
                {"ok": False, "error": "验证服务繁忙，请稍后重试"},
                status=429,
                headers={"Retry-After": "2"},
            )
        # Combined verification checks each service in guided order and stops
        # at the first failure; the member must pass every one of them.
        passed = True
        verification_errors: list[str] = []
        failed_subprovider = required_subproviders[0]
        try:
            for subprovider in required_subproviders:
                site_key, secret_key = verification_keys_for_provider(
                    self.settings, subprovider
                )
                if subprovider == "hcaptcha":
                    passed, verification_errors = await verify_hcaptcha_token(
                        secret_key=secret_key,
                        hcaptcha_token=subprovider_tokens[subprovider],
                        remote_ip=remote_ip,
                        site_key=site_key,
                    )
                else:
                    passed, verification_errors = await verify_turnstile_token(
                        secret_key=secret_key,
                        turnstile_token=subprovider_tokens[subprovider],
                        remote_ip=remote_ip,
                    )
                if not passed:
                    failed_subprovider = subprovider
                    break
        finally:
            self._verification_upstream_slots.release()
        if _verification_config_tag(self.settings, provider) != current_config_tag:
            release_active()
            return web.json_response(
                {"ok": False, "error": "验证配置已更新，请重新打开验证页面"},
                status=409,
            )
        if not passed:
            replay_error = failed_subprovider == "hcaptcha" and bool(
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
                if failed_subprovider == "hcaptcha"
                else _TURNSTILE_CONFIGURATION_ERRORS
            )
            configuration_errors = configuration_error_codes.intersection(
                verification_errors
            )
            if configuration_errors:
                release_active()
                # Block only the base service whose secret failed; extending
                # by that service also covers combined records that need it.
                mark_turnstile_configuration_unavailable(
                    self.settings,
                    reason=f"{failed_subprovider} Secret Key 无效",
                    provider=failed_subprovider,
                )
                async with self.session_factory() as session:
                    await extend_pending_verification_deadlines(
                        session,
                        settings=self.settings,
                        provider=failed_subprovider,
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

            lease_until = now + timedelta(seconds=TERMINAL_LEASE_SECONDS)
            claimed = await claim_join_verification(
                session,
                verification_id=current.id,
                deadline_at=current.deadline_at,
                kind=current.kind,
                now=now,
                expired=False,
                lease_until=lease_until,
                target_status=VERIFICATION_STATUS_RELEASING,
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
            # Persist the releasing lease before Telegram calls. A crash after
            # this commit is recovered by the sweeper as restore+CAS delete,
            # never as a timeout kick/ban.
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
                lease_until=lease_until,
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
                        lease_until=lease_until,
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
                    lease_until=lease_until,
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
        lease_until: datetime,
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
            retry_lease = now_shanghai_naive() + timedelta(
                seconds=TERMINAL_LEASE_SECONDS
            )
            async with self.session_factory() as session:
                deferred = await renew_join_verification_lease(
                    session,
                    verification_id=verification_id,
                    lease_until=lease_until,
                    new_lease_until=retry_lease,
                    status=VERIFICATION_STATUS_RELEASING,
                )
                if deferred:
                    await session.commit()
                else:
                    await session.rollback()
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
                    "error": (
                        "验证已通过，但 Telegram 放行暂时失败；后台将继续重试"
                        if deferred
                        else "验证已通过，但放行工单已由后台接管，请稍后重试"
                    ),
                    "verification_id": verification_id,
                },
                status=502,
            )
        async with self.session_factory() as session:
            completed = await complete_leased_join_verification(
                session,
                verification_id=verification_id,
                lease_until=lease_until,
                status=VERIFICATION_STATUS_RELEASING,
            )
            if completed:
                await session.commit()
            else:
                await session.rollback()
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
        if kind == VERIFICATION_KIND_JOIN:
            from bot.services.welcome import send_group_welcome

            try:
                async with self.session_factory() as session:
                    await send_group_welcome(
                        self.bot,
                        session,
                        self.settings,
                        group_id=group_id,
                        user_id=user_id,
                        display_name=display_name,
                    )
            except Exception:
                log.debug("welcome after verification failed | group=%s", group_id)
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
        lease_until: datetime,
    ) -> None:
        try:
            restored = await self._completed_member_is_unrestricted(
                fingerprint,
                user_id,
            ) or await restore_member_permissions(self.bot, group_id, user_id)
            if restored:
                async with self.session_factory() as session:
                    completed = await complete_leased_join_verification(
                        session,
                        verification_id=verification_id,
                        lease_until=lease_until,
                        status=VERIFICATION_STATUS_RELEASING,
                    )
                    if completed:
                        await session.commit()
                    else:
                        await session.rollback()
                self._mark_verification_completed(
                    verification_id,
                    user_id,
                    fingerprint,
                )
                return

            retry_lease = now_shanghai_naive() + timedelta(
                seconds=TERMINAL_LEASE_SECONDS
            )
            async with self.session_factory() as session:
                deferred = await renew_join_verification_lease(
                    session,
                    verification_id=verification_id,
                    lease_until=lease_until,
                    new_lease_until=retry_lease,
                    status=VERIFICATION_STATUS_RELEASING,
                )
                if deferred:
                    await session.commit()
                else:
                    await session.rollback()
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
        elif kind == VERIFICATION_KIND_RAID:
            passed_text = f"✅ <b>{shown}</b> 已通过爆破防护质询，发言权限已恢复。"
        else:
            passed_text = f"✅ <b>{shown}</b> 已通过真人验证，欢迎加入！"
        text = passed_text + (
            "" if restored else "\n⚠️ 权限恢复失败，请管理员手动解除禁言。"
        )
        # A patrol/raid prompt is shared by several members; editing it would
        # remove the warning and its challenge button for the others.
        if prompt_message_id and kind not in SHARED_PROMPT_VERIFICATION_KINDS:
            try:
                edited = await self.bot.edit_message_text(
                    chat_id=group_id,
                    message_id=prompt_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
                # Repurposed challenge prompt is now a moderation outcome
                # notice: honor the group's auto-delete retention.
                await schedule_message_auto_delete_durable(
                    edited if not isinstance(edited, bool) else None,
                    configured_auto_delete_seconds(self.settings, "moderation"),
                )
                return
            except Exception:
                log.debug(
                    "verification pass edit failed | group=%s message=%s",
                    group_id,
                    prompt_message_id,
                )
                # The record has already been consumed. If the edit failed,
                # remove the original prompt before sending a standalone
                # outcome so its old buttons cannot report a confusing
                # "expired" state forever.
                await delete_verification_prompt(
                    self.bot,
                    group_id,
                    prompt_message_id,
                )
        try:
            # Standalone pass notice (shared patrol/raid kinds, or edit
            # fallback) is a moderation outcome notice: honor the group's
            # auto-delete retention. The in-place edit above reuses the
            # persisted challenge prompt and is left to persist like other
            # finalizations.
            sent = await self.bot.send_message(group_id, text, parse_mode="HTML")
            await schedule_message_auto_delete_durable(
                sent, configured_auto_delete_seconds(self.settings, "moderation")
            )
        except Exception:
            log.debug("verification pass notice failed | group=%s", group_id)
