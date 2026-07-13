"""Built-in HTTP server for join and moderation Turnstile challenges.

Serves two endpoints on JOIN_VERIFICATION_LISTEN_HOST:PORT (put it behind a
reverse proxy / Cloudflare Tunnel and expose it as
JOIN_VERIFICATION_PUBLIC_BASE_URL):

- GET  /verify: the Mini App page embedding the Turnstile widget. Opened
  inside Telegram via a web_app button — the URL is never shown to the user.
- POST /verify: receives the widget token plus Telegram's signed initData.
  The initData signature is validated against the bot token (aiogram's
  safe_parse_webapp_init_data), which authenticates the user without any
  link token; the Turnstile token is validated against Cloudflare's
  siteverify API. Both must pass before the member's restriction is lifted.
"""
from __future__ import annotations

import html
import logging

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    VERIFICATION_KIND_MODERATION,
    claim_join_verification,
    delete_join_verification,
    get_pending_verification_for_user,
    restore_member_permissions,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>真人验证</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>
  body {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 100vh; margin: 0;
    background: var(--tg-theme-bg-color, #f5f6f8);
    color: var(--tg-theme-text-color, #1f2328);
  }}
  .card {{
    background: var(--tg-theme-secondary-bg-color, #fff);
    border-radius: 12px; padding: 32px 28px;
    max-width: 360px; text-align: center;
  }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  p {{ font-size: 14px; opacity: .75; margin: 0 0 20px; }}
  #status {{ margin-top: 16px; font-size: 14px; min-height: 20px; }}
  .ok {{ color: #1a7f37; }}
  .err {{ color: #cf222e; }}
</style>
</head>
<body>
<div class="card">
  <h1>真人验证</h1>
  <p>完成下方人机验证后即可在群内发言。</p>
  <div class="cf-turnstile" data-sitekey="{site_key}" data-callback="onTurnstileSuccess"></div>
  <div id="status"></div>
</div>
<script>
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) {{ tg.ready(); tg.expand(); }}

async function onTurnstileSuccess(token) {{
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
      body: JSON.stringify({{turnstile_token: token, init_data: tg.initData}}),
    }});
    const data = await resp.json();
    if (resp.ok && data.ok) {{
      status.textContent = "✅ 验证通过，群内权限已恢复。";
      status.className = "ok";
      setTimeout(() => tg.close(), 1500);
    }} else {{
      status.textContent = "❌ " + (data.error || "验证失败，请重试。");
      status.className = "err";
    }}
  }} catch (e) {{
    status.textContent = "❌ 网络错误，请关闭后重试。";
    status.className = "err";
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
) -> bool:
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
                data = await resp.json(content_type=None)
    except Exception:
        log.exception("turnstile siteverify request failed")
        return False
    success = bool(isinstance(data, dict) and data.get("success"))
    if not success:
        log.info(
            "turnstile siteverify rejected | errors=%s",
            (data or {}).get("error-codes") if isinstance(data, dict) else data,
        )
    return success


class VerifyWebServer:
    """aiohttp server hosting the Turnstile Mini App."""

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/verify", self.handle_challenge_page)
        app.router.add_post("/verify", self.handle_challenge_submit)
        app.router.add_get("/healthz", self.handle_health)
        return app

    async def start(self) -> None:
        app = self.build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            host=self.settings.join_verification_listen_host,
            port=self.settings.join_verification_listen_port,
        )
        await site.start()
        log.info(
            "join verification web server listening on %s:%s",
            self.settings.join_verification_listen_host,
            self.settings.join_verification_listen_port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def handle_challenge_page(self, _request: web.Request) -> web.Response:
        # The page is static: user identity arrives with the POSTed initData,
        # so an unauthenticated GET reveals nothing but the public site key.
        page = _PAGE_TEMPLATE.format(
            site_key=html.escape(self.settings.join_verification_turnstile_site_key),
        )
        return web.Response(text=page, content_type="text/html")

    async def handle_challenge_submit(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        turnstile_token = str((body or {}).get("turnstile_token") or "")
        init_data = str((body or {}).get("init_data") or "")
        if not turnstile_token or not init_data:
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
            record = await get_pending_verification_for_user(session, user_id)
        if record is None or record.deadline_at <= now_shanghai_naive():
            return web.json_response(
                {"ok": False, "error": "没有有效的待处理验证，请联系群管理员"},
                status=404,
            )

        passed = await verify_turnstile_token(
            secret_key=self.settings.join_verification_turnstile_secret_key,
            turnstile_token=turnstile_token,
            remote_ip=request.headers.get("CF-Connecting-IP", request.remote or ""),
        )
        if not passed:
            return web.json_response({"ok": False, "error": "人机验证未通过，请重试"}, status=403)

        async with self.session_factory() as session:
            # Re-check under a fresh session: the sweeper may have kicked the
            # user (record gone) while Turnstile was being validated.
            current = await get_pending_verification_for_user(session, user_id)
            now = now_shanghai_naive()
            if current is None or current.deadline_at <= now:
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
