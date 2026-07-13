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

import html
import hashlib
import logging
import secrets
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import UserWarning
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    VERIFICATION_KIND_MODERATION,
    claim_join_verification,
    clear_turnstile_configuration_unavailable,
    delete_join_verification,
    extend_pending_verification_deadlines,
    get_pending_verification_by_id_for_user,
    get_pending_verification_for_user,
    mark_turnstile_configuration_unavailable,
    normalize_verification_provider,
    restore_member_permissions,
    turnstile_verification_configured,
    verification_keys_for_provider,
    verification_provider,
)
from bot.services.runtime_config import RuntimeConfigManager
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
}
_TURNSTILE_SERVICE_ERRORS = {
    TURNSTILE_ERROR_UPSTREAM_UNAVAILABLE,
    "internal-error",
}
_SETTINGS_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


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
const verificationId = {verification_id};

function resetChallenge() {{
  if (provider === "hcaptcha" && window.hcaptcha) window.hcaptcha.reset();
  if (provider === "turnstile" && window.turnstile) window.turnstile.reset();
}}

function onChallengeError() {{
  const status = document.getElementById("status");
  status.textContent = "❌ 验证组件暂时不可用，请稍后重试。";
  status.className = "err";
  setTimeout(resetChallenge, 1000);
}}

function onChallengeExpired() {{
  const status = document.getElementById("status");
  status.textContent = "验证已过期，请重新完成验证。";
  status.className = "err";
  resetChallenge();
}}

async function onChallengeSuccess(token) {{
  const status = document.getElementById("status");
  if (!tg || !tg.initData) {{
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
      status.textContent = "❌ " + (data.error || "验证失败，请重试。");
      status.className = "err";
      resetChallenge();
    }}
  }} catch (e) {{
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
    return bool(data.get("success")), errors


class VerifyWebServer:
    """aiohttp server hosting the settings and human-verification Mini Apps."""

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_config: RuntimeConfigManager | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self.runtime_config = runtime_config
        self._runner: web.AppRunner | None = None

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
        return app

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
            record = (
                await get_pending_verification_by_id_for_user(
                    session,
                    verification_id,
                    user_id,
                )
                if verification_id
                else await get_pending_verification_for_user(session, user_id)
            )
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
        if record is None or record.deadline_at <= now_shanghai_naive():
            return web.json_response(
                {"ok": False, "error": "没有有效的待处理验证，请联系群管理员"},
                status=404,
            )
        if locally_banned:
            return web.json_response(
                {"ok": False, "error": "该用户已被本群管理员封禁，请联系管理员"},
                status=403,
            )

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

        remote_ip = request.headers.get("CF-Connecting-IP", request.remote or "")
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
            return web.json_response(
                {"ok": False, "error": "验证配置已更新，请重新打开验证页面"},
                status=409,
            )
        if not passed:
            configuration_error_codes = (
                _HCAPTCHA_CONFIGURATION_ERRORS
                if provider == "hcaptcha"
                else _TURNSTILE_CONFIGURATION_ERRORS
            )
            configuration_errors = configuration_error_codes.intersection(
                verification_errors
            )
            if configuration_errors:
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
                return web.json_response(
                    {
                        "ok": False,
                        "error": "验证服务暂时不可用，请稍后重试",
                    },
                    status=503,
                )
            return web.json_response({"ok": False, "error": "人机验证未通过，请重试"}, status=403)

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
            if (
                current is None
                or current.id != record.id
                or normalize_verification_provider(current.provider) != provider
                or current.deadline_at != record.deadline_at
                or current.deadline_at <= now
            ):
                return web.json_response(
                    {"ok": False, "error": "验证已过期或失效，请联系群管理员"},
                    status=404,
                )
            if await is_globally_banned(session, user_id):
                await delete_join_verification(session, current.group_id, user_id)
                await session.commit()
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
                return web.json_response(
                    {"ok": False, "error": "验证已失效，请重新打开验证入口"},
                    status=404,
                )

            group_id = int(current.group_id)
            prompt_message_id = int(current.prompt_message_id or 0)
            display_name = (current.display_name or "").strip()
            kind = current.kind
            # Commit before Telegram calls so a failed restore cannot roll the
            # pass back and let the sweeper kick a verified member.
            await session.commit()

        restored = await restore_member_permissions(self.bot, group_id, user_id)
        log.info(
            "verification passed | kind=%s group=%s user=%s restored=%s",
            kind,
            group_id,
            user_id,
            restored,
        )
        await self._announce_pass(
            group_id=group_id,
            user_id=user_id,
            display_name=display_name,
            prompt_message_id=prompt_message_id,
            kind=kind,
            restored=restored,
        )
        if not restored:
            return web.json_response(
                {"ok": False, "error": "验证已通过，但恢复权限失败，请联系群管理员"},
                status=502,
            )
        return web.json_response({"ok": True})

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
        passed_text = (
            f"✅ <b>{shown}</b> 已通过消息审查验证，发言权限已恢复。"
            if kind == VERIFICATION_KIND_MODERATION
            else f"✅ <b>{shown}</b> 已通过真人验证，欢迎加入！"
        )
        text = passed_text + (
            "" if restored else "\n⚠️ 权限恢复失败，请管理员手动解除禁言。"
        )
        if prompt_message_id:
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
