import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from bot.db.engine import init_db
from bot.services.join_screening import add_global_ban
from bot.services.join_verification import (
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    delete_join_verification,
    get_join_verification,
    upsert_join_verification,
)
from bot.services.verify_web import (
    VerifyWebServer,
    _client_public_ip,
    verify_hcaptcha_token,
    verify_turnstile_token,
)
from bot.utils.timezone import now_shanghai_naive

BOT_TOKEN = "42:TEST_TOKEN"


def _signed_init_data(user_id: int, *, bot_token: str = BOT_TOKEN) -> str:
    """Build initData signed the way Telegram signs Mini App payloads."""
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "新人"}, separators=(",", ":")
        ),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _settings():
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.join_verification_enabled = True
    settings.join_verification_turnstile_site_key = "site-key"
    settings.join_verification_turnstile_secret_key = "secret-key"
    settings.join_verification_public_base_url = "https://verify.example.com"
    return settings


class VerifyWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            restrict_chat_member=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        self.settings = _settings()
        self.server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
        )
        self.client = TestClient(TestServer(self.server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _seed(
        self,
        *,
        user_id: int,
        minutes: int = 5,
        kind: str = VERIFICATION_KIND_JOIN,
        reason: str = "",
        provider: str = "turnstile",
        group_id: int = -100,
    ) -> int:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=group_id,
                user_id=user_id,
                deadline_at=now_shanghai_naive() + timedelta(minutes=minutes),
                kind=kind,
                reason=reason,
                display_name="新人",
                prompt_message_id=777,
                provider=provider,
            )
            await session.commit()
            record = await get_join_verification(session, group_id, user_id)
            return int(record.id)

    async def _submit(
        self,
        user_id: int,
        *,
        init_data: str | None = None,
        turnstile: str = "cf-tok",
        verification_id: int = 0,
    ):
        return await self.client.post(
            "/verify",
            json={
                "challenge_token": turnstile,
                "provider": "turnstile",
                "verification_id": verification_id,
                "init_data": _signed_init_data(user_id) if init_data is None else init_data,
            },
        )

    async def _submit_hcaptcha(
        self,
        user_id: int,
        *,
        verification_id: int,
        token: str = "h-token",
    ):
        return await self.client.post(
            "/verify",
            json={
                "challenge_token": token,
                "provider": "hcaptcha",
                "verification_id": verification_id,
                "init_data": _signed_init_data(user_id),
            },
        )

    async def _force_verification_id(
        self,
        *,
        group_id: int,
        user_id: int,
        verification_id: int,
    ) -> None:
        async with self.session_factory() as session:
            record = await get_join_verification(session, group_id, user_id)
            self.assertIsNotNone(record)
            record.id = verification_id
            await session.commit()

    async def test_health_endpoint(self) -> None:
        resp = await self.client.get("/healthz")
        self.assertEqual(resp.status, 200)

    async def test_non_object_submit_body_gets_400(self) -> None:
        resp = await self.client.post("/verify", json=["invalid"])
        self.assertEqual(resp.status, 400)

    async def test_challenge_page_renders_turnstile_and_webapp_sdk(self) -> None:
        resp = await self.client.get("/verify")
        body = await resp.text()

        self.assertEqual(resp.status, 200)
        self.assertIn("cf-turnstile", body)
        self.assertIn('data-sitekey="site-key"', body)
        self.assertIn("challenges.cloudflare.com/turnstile/v0/api.js", body)
        self.assertIn("telegram.org/js/telegram-web-app.js", body)
        self.assertIn('data-error-callback="onChallengeError"', body)
        self.assertIn("resetChallenge", body)
        self.assertIn('script-src', resp.headers["Content-Security-Policy"])
        self.assertIn("'nonce-", resp.headers["Content-Security-Policy"])
        self.assertIn('<script nonce="', body)

    async def test_siteverify_error_codes_are_preserved(self) -> None:
        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, *, content_type=None):
                return {
                    "success": False,
                    "error-codes": ["invalid-input-secret"],
                }

        class FakeClientSession:
            def __init__(self, *, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, _url, *, data):
                self.data = data
                return FakeResponse()

        with patch(
            "bot.services.verify_web.aiohttp.ClientSession",
            FakeClientSession,
        ):
            passed, errors = await verify_turnstile_token(
                secret_key="wrong-secret",
                turnstile_token="token",
            )

        self.assertFalse(passed)
        self.assertEqual(errors, ["invalid-input-secret"])

    async def test_hcaptcha_page_and_submission_use_selected_provider(self) -> None:
        settings = _settings()
        settings.join_verification_hcaptcha_site_key = "h-site"
        settings.join_verification_hcaptcha_secret_key = "h-secret"
        server = VerifyWebServer(
            bot=self.bot,
            settings=settings,
            session_factory=self.session_factory,
        )
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        try:
            page = await client.get("/verify?provider=hcaptcha")
            body = await page.text()
            self.assertIn("h-captcha", body)
            self.assertIn('data-size="compact"', body)
            self.assertIn("js.hcaptcha.com/1/api.js", body)
            self.assertIn('const provider = "hcaptcha"', body)

            verification_id = await self._seed(user_id=120, provider="hcaptcha")
            with patch(
                "bot.services.verify_web.verify_hcaptcha_token",
                new=AsyncMock(return_value=(True, [])),
            ) as verify_mock:
                response = await client.post(
                    "/verify",
                    json={
                        "challenge_token": "h-token",
                        "provider": "hcaptcha",
                        "verification_id": verification_id,
                        "init_data": _signed_init_data(120),
                    },
                )
            self.assertEqual(response.status, 200)
            self.assertEqual(verify_mock.await_args.kwargs["hcaptcha_token"], "h-token")
            self.assertEqual(verify_mock.await_args.kwargs["remote_ip"], "")
        finally:
            await client.close()

    async def test_hcaptcha_siteverify_payload_includes_site_key_and_remote_ip(self) -> None:
        captured: dict = {}

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, *, content_type=None):
                return {"success": True}

        class FakeClientSession:
            def __init__(self, *, timeout):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, url, *, data):
                captured["url"] = url
                captured["data"] = data
                return FakeResponse()

        with patch("bot.services.verify_web.aiohttp.ClientSession", FakeClientSession):
            passed, errors = await verify_hcaptcha_token(
                secret_key="h-secret",
                hcaptcha_token="h-token",
                remote_ip="203.0.113.2",
                site_key="h-site",
            )

        self.assertTrue(passed)
        self.assertEqual(errors, [])
        self.assertEqual(captured["url"], "https://api.hcaptcha.com/siteverify")
        self.assertEqual(
            captured["data"],
            {
                "secret": "h-secret",
                "response": "h-token",
                "remoteip": "203.0.113.2",
                "sitekey": "h-site",
            },
        )

    def test_client_ip_trusts_proxy_header_only_from_private_peer(self) -> None:
        proxied = SimpleNamespace(
            remote="127.0.0.1",
            headers={"CF-Connecting-IP": "8.8.8.8"},
        )
        direct = SimpleNamespace(
            remote="1.1.1.1",
            headers={"CF-Connecting-IP": "8.8.8.8"},
        )

        self.assertEqual(_client_public_ip(proxied), "8.8.8.8")
        self.assertEqual(_client_public_ip(direct), "1.1.1.1")

    async def test_stale_provider_page_is_rejected_before_siteverify(self) -> None:
        await self._seed(user_id=121)
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(),
        ) as verify_mock:
            response = await self.client.post(
                "/verify",
                json={
                    "challenge_token": "token",
                    "provider": "hcaptcha",
                    "init_data": _signed_init_data(121),
                },
            )
        self.assertEqual(response.status, 409)
        verify_mock.assert_not_awaited()

    async def test_verification_id_selects_exact_group_record(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        turnstile_id = await self._seed(
            user_id=122,
            group_id=-100,
            provider="turnstile",
        )
        hcaptcha_id = await self._seed(
            user_id=122,
            group_id=-200,
            provider="hcaptcha",
        )

        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ) as turnstile_mock:
            response = await self._submit(122, verification_id=turnstile_id)

        self.assertEqual(response.status, 200)
        turnstile_mock.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 122))
            self.assertIsNotNone(await get_join_verification(session, -200, 122))

        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(return_value=(True, [])),
        ) as hcaptcha_mock:
            response = await self.client.post(
                "/verify",
                json={
                    "challenge_token": "h-token",
                    "provider": "hcaptcha",
                    "verification_id": hcaptcha_id,
                    "init_data": _signed_init_data(122),
                },
            )

        self.assertEqual(response.status, 200)
        hcaptcha_mock.assert_awaited_once()

    async def test_successful_submit_restores_permissions(self) -> None:
        await self._seed(user_id=102)
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ) as verify_mock:
            resp = await self._submit(102)

        self.assertEqual(resp.status, 200)
        self.assertTrue((await resp.json())["ok"])
        verify_mock.assert_awaited_once()
        self.assertEqual(verify_mock.await_args.kwargs["turnstile_token"], "cf-tok")
        self.bot.restrict_chat_member.assert_awaited_once()
        args = self.bot.restrict_chat_member.await_args
        self.assertEqual(args.args[:2], (-100, 102))
        self.assertTrue(args.kwargs["permissions"].can_send_messages)
        self.assertTrue(args.kwargs["permissions"].can_react_to_messages)
        self.assertTrue(args.kwargs["permissions"].can_edit_tag)
        self.assertTrue(args.kwargs["use_independent_chat_permissions"])
        self.bot.edit_message_text.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 102))

    async def test_successful_moderation_submit_uses_moderation_notice(self) -> None:
        await self._seed(
            user_id=109,
            kind=VERIFICATION_KIND_MODERATION,
            reason="疑似广告引流",
        )
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            resp = await self._submit(109)

        self.assertEqual(resp.status, 200)
        self.assertTrue((await resp.json())["ok"])
        self.bot.restrict_chat_member.assert_awaited_once()
        notice = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual(notice["chat_id"], -100)
        self.assertEqual(notice["message_id"], 777)
        self.assertIn("已通过消息审查验证", notice["text"])
        self.assertNotIn("欢迎加入", notice["text"])
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 109))

    async def test_forged_init_data_is_rejected(self) -> None:
        await self._seed(user_id=103)
        forged = _signed_init_data(103, bot_token="43:WRONG_TOKEN")
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ) as verify_mock:
            resp = await self._submit(103, init_data=forged)

        self.assertEqual(resp.status, 403)
        verify_mock.assert_not_awaited()
        self.bot.restrict_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 103))

    async def test_failed_turnstile_keeps_record_and_restriction(self) -> None:
        await self._seed(user_id=104)
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(False, [])),
        ):
            resp = await self._submit(104)

        self.assertEqual(resp.status, 403)
        self.bot.restrict_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 104))

    async def test_submit_without_params_is_rejected(self) -> None:
        resp = await self.client.post("/verify", json={})
        self.assertEqual(resp.status, 400)

    async def test_user_without_pending_record_gets_404(self) -> None:
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ) as verify_mock:
            resp = await self._submit(105)
        self.assertEqual(resp.status, 404)
        # No pending record: reject before spending a siteverify call.
        verify_mock.assert_not_awaited()

    async def test_expired_record_gets_404(self) -> None:
        await self._seed(user_id=106, minutes=-1)
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            resp = await self._submit(106)
        self.assertEqual(resp.status, 404)

    async def test_record_expiring_during_turnstile_is_rejected(self) -> None:
        await self._seed(
            user_id=110,
            kind=VERIFICATION_KIND_MODERATION,
            reason="低置信度命中",
        )

        async def expire_while_verifying(**_kwargs) -> tuple[bool, list[str]]:
            async with self.session_factory() as session:
                record = await get_join_verification(session, -100, 110)
                self.assertIsNotNone(record)
                record.deadline_at = now_shanghai_naive() - timedelta(seconds=1)
                await session.commit()
            return True, []

        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(side_effect=expire_while_verifying),
        ) as verify_mock:
            resp = await self._submit(110)

        self.assertEqual(resp.status, 404)
        self.assertIn("过期", (await resp.json())["error"])
        verify_mock.assert_awaited_once()
        self.bot.restrict_chat_member.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 110)
            self.assertIsNotNone(record)
            self.assertLessEqual(record.deadline_at, now_shanghai_naive())

    async def test_global_ban_rejects_restore_and_clears_pending(self) -> None:
        await self._seed(
            user_id=111,
            kind=VERIFICATION_KIND_MODERATION,
            reason="低置信度命中",
        )
        async with self.session_factory() as session:
            await add_global_ban(
                session,
                111,
                reason="管理员封禁",
                source="manual",
                created_by=42,
            )
            await session.commit()

        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ) as verify_mock:
            resp = await self._submit(111)

        self.assertEqual(resp.status, 403)
        self.assertIn("已被管理员封禁", (await resp.json())["error"])
        verify_mock.assert_awaited_once()
        self.bot.restrict_chat_member.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 111))

    async def test_verification_single_use(self) -> None:
        verification_id = await self._seed(user_id=107)

        both_verifying = asyncio.Event()
        verification_count = 0

        async def verify_concurrently(**_kwargs) -> tuple[bool, list[str]]:
            nonlocal verification_count
            verification_count += 1
            if verification_count == 2:
                both_verifying.set()
            await both_verifying.wait()
            return True, []

        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(side_effect=verify_concurrently),
        ) as verify_mock:
            first, second = await asyncio.gather(
                self._submit(107, verification_id=verification_id),
                self._submit(107, verification_id=verification_id),
            )
            repeated = await self._submit(107, verification_id=verification_id)

        self.assertEqual((first.status, second.status), (200, 200))
        self.assertEqual(repeated.status, 200)
        self.assertEqual(verify_mock.await_count, 2)
        self.assertEqual(self.bot.restrict_chat_member.await_count, 1)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 107))

    async def test_hcaptcha_concurrent_replay_is_idempotent_for_same_record(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        verification_id = await self._seed(user_id=127, provider="hcaptcha")
        second_started = asyncio.Event()
        verification_count = 0

        async def verify_concurrently(**_kwargs) -> tuple[bool, list[str]]:
            nonlocal verification_count
            verification_count += 1
            if verification_count == 1:
                await second_started.wait()
                return True, []
            second_started.set()
            return False, ["invalid-or-already-seen-response"]

        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(side_effect=verify_concurrently),
        ) as verify_mock:
            first, second = await asyncio.gather(
                self._submit_hcaptcha(127, verification_id=verification_id),
                self._submit_hcaptcha(127, verification_id=verification_id),
            )

        self.assertEqual((first.status, second.status), (200, 200))
        self.assertEqual(verify_mock.await_count, 2)
        self.bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 127))

    async def test_duplicate_submit_waits_while_permissions_are_being_restored(self) -> None:
        verification_id = await self._seed(user_id=130)
        restore_started = asyncio.Event()
        allow_restore = asyncio.Event()

        async def delayed_restore(*_args, **_kwargs) -> bool:
            restore_started.set()
            await allow_restore.wait()
            return True

        with (
            patch(
                "bot.services.verify_web.verify_turnstile_token",
                new=AsyncMock(return_value=(True, [])),
            ) as verify_mock,
            patch(
                "bot.services.verify_web.restore_member_permissions",
                new=AsyncMock(side_effect=delayed_restore),
            ),
        ):
            first = asyncio.create_task(
                self._submit(130, verification_id=verification_id)
            )
            await restore_started.wait()
            repeated = asyncio.create_task(
                self._submit(130, verification_id=verification_id)
            )
            await asyncio.sleep(1.2)
            self.assertFalse(repeated.done())
            allow_restore.set()
            first_response, repeated_response = await asyncio.gather(first, repeated)

        self.assertEqual((first_response.status, repeated_response.status), (200, 200))
        verify_mock.assert_awaited_once()

    async def test_reused_id_still_runs_new_hcaptcha_verification(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        first_id = await self._seed(user_id=125, provider="hcaptcha")

        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            first = await self._submit_hcaptcha(125, verification_id=first_id)
        self.assertEqual(first.status, 200)

        second_id = await self._seed(user_id=125, provider="hcaptcha")
        self.assertGreater(second_id, first_id)
        await self._force_verification_id(
            group_id=-100,
            user_id=125,
            verification_id=first_id,
        )
        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(return_value=(True, [])),
        ) as verify_mock:
            second = await self._submit_hcaptcha(125, verification_id=first_id)

        self.assertEqual(second.status, 200)
        verify_mock.assert_awaited_once()
        self.assertEqual(self.bot.restrict_chat_member.await_count, 2)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 125))

    async def test_reused_id_cannot_accept_old_completion_on_hcaptcha_replay(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        first_id = await self._seed(user_id=126, provider="hcaptcha")

        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            first = await self._submit_hcaptcha(126, verification_id=first_id)
        self.assertEqual(first.status, 200)

        second_id = await self._seed(user_id=126, provider="hcaptcha")
        self.assertGreater(second_id, first_id)
        await self._force_verification_id(
            group_id=-100,
            user_id=126,
            verification_id=first_id,
        )
        with (
            patch(
                "bot.services.verify_web.verify_hcaptcha_token",
                new=AsyncMock(return_value=(False, ["already-seen-response"])),
            ) as verify_mock,
        ):
            second = await asyncio.wait_for(
                self._submit_hcaptcha(126, verification_id=first_id),
                timeout=1.0,
            )

        self.assertEqual(second.status, 403)
        verify_mock.assert_awaited_once()
        self.assertEqual(self.bot.restrict_chat_member.await_count, 1)
        self.assertEqual(self.server._active_verifications, {})
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 126))

    async def test_old_completed_id_cannot_hide_newer_pending_record(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        old_id = await self._seed(user_id=128)

        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            completed = await self._submit(128, verification_id=old_id)
        self.assertEqual(completed.status, 200)

        other_user_id = await self._seed(user_id=999, group_id=-200)
        new_id = await self._seed(user_id=128, provider="hcaptcha")
        self.assertGreater(other_user_id, old_id)
        self.assertNotEqual(new_id, old_id)

        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(),
        ) as verify_mock:
            stale = await self._submit(128, verification_id=old_id)

        self.assertEqual(stale.status, 409)
        verify_mock.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 128))

    async def test_reused_id_after_new_generation_ended_cannot_use_old_cache(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        old_id = await self._seed(user_id=131, provider="hcaptcha")

        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            completed = await self._submit_hcaptcha(131, verification_id=old_id)
        self.assertEqual(completed.status, 200)

        new_id = await self._seed(user_id=131, provider="hcaptcha")
        self.assertGreater(new_id, old_id)
        await self._force_verification_id(
            group_id=-100,
            user_id=131,
            verification_id=old_id,
        )
        async with self.session_factory() as session:
            self.assertTrue(await delete_join_verification(session, -100, 131))
            await session.commit()
        self.bot.get_chat_member.return_value = SimpleNamespace(status="left")

        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(),
        ) as verify_mock:
            stale = await self._submit_hcaptcha(131, verification_id=old_id)

        self.assertEqual(stale.status, 404)
        verify_mock.assert_not_awaited()

    async def test_invalid_hcaptcha_response_keeps_pending_record(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        verification_id = await self._seed(user_id=129, provider="hcaptcha")

        with patch(
            "bot.services.verify_web.verify_hcaptcha_token",
            new=AsyncMock(return_value=(False, ["invalid-input-response"])),
        ) as verify_mock:
            response = await self._submit_hcaptcha(
                129,
                verification_id=verification_id,
            )

        self.assertEqual(response.status, 403)
        verify_mock.assert_awaited_once()
        self.bot.restrict_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 129))

    async def test_restore_response_loss_is_confirmed_from_member_state(self) -> None:
        verification_id = await self._seed(user_id=123)
        self.bot.restrict_chat_member.side_effect = RuntimeError("response lost")
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="member")
        )

        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            response = await self._submit(
                123,
                verification_id=verification_id,
            )

        self.assertEqual(response.status, 200)
        self.bot.get_chat_member.assert_awaited_once_with(-100, 123)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 123))

    async def test_restore_failure_requeues_verification_for_retry(self) -> None:
        verification_id = await self._seed(user_id=124)
        self.bot.restrict_chat_member.side_effect = RuntimeError("telegram unavailable")
        self.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("lookup failed"))

        with (
            patch(
                "bot.services.verify_web.verify_turnstile_token",
                new=AsyncMock(return_value=(True, [])),
            ),
            patch(
                "bot.services.join_verification.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            response = await self._submit(
                124,
                verification_id=verification_id,
            )

        self.assertEqual(response.status, 502)
        payload = await response.json()
        self.assertGreater(payload["verification_id"], 0)
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 124)
            self.assertIsNotNone(record)
            self.assertEqual(record.id, payload["verification_id"])

    async def test_identity_comes_from_init_data_not_body(self) -> None:
        # A pending user B cannot be verified by user A's signed session.
        await self._seed(user_id=108)
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(True, [])),
        ):
            resp = await self._submit(999)  # signed as another user
        self.assertEqual(resp.status, 404)
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 108))

    async def test_invalid_turnstile_secret_returns_configuration_error(self) -> None:
        await self._seed(user_id=112)
        before = now_shanghai_naive()
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(False, ["invalid-input-secret"])),
        ):
            resp = await self._submit(112)

        self.assertEqual(resp.status, 503)
        self.assertIn("Secret Key 无效", (await resp.json())["error"])
        self.bot.restrict_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 112)
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, before)

    async def test_turnstile_upstream_failure_returns_service_unavailable(self) -> None:
        await self._seed(user_id=113)
        with patch(
            "bot.services.verify_web.verify_turnstile_token",
            new=AsyncMock(return_value=(False, ["siteverify-unavailable"])),
        ):
            resp = await self._submit(113)

        self.assertEqual(resp.status, 503)
        self.assertIn("暂时不可用", (await resp.json())["error"])
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 113))


if __name__ == "__main__":
    unittest.main()
