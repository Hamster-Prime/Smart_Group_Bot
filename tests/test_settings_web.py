import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiogram.types import ChatPermissions
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    Group,
    GroupApiModelQuerySecret,
    GroupMember,
    GroupPermanentMemory,
    KeywordReply,
    ModerationRule,
    ScheduledMessage,
    SpeechStyleSample,
    UserWarning,
)
from bot.services.runtime_config import RuntimeConfigManager, SecretCipher
from bot.services.group_permissions import PERMISSION_FIELDS
from bot.services.verify_web import VerifyWebServer
from bot.utils.telegram import configured_auto_delete_seconds

BOT_TOKEN = "42:TEST_TOKEN"


def _signed_init_data(
    user_id: int,
    *,
    auth_date: int | None = None,
    bot_token: str = BOT_TOKEN,
) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-settings-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Owner"},
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(
        secret,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(pairs)


class SettingsWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=42,
            config_master_key="settings-web-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8480
        self.settings.join_verification_public_base_url = self.settings.miniapp_public_base_url
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-settings-web.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    @staticmethod
    def _headers(user_id: int = 42, **kwargs: object) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id, **kwargs)}"}

    async def _group_document(self, group_id: int) -> dict:
        response = await self.client.get(
            "/api/v1/groups",
            headers=self._headers(),
        )
        self.assertEqual(response.status, 200)
        groups = (await response.json())["groups"]
        return next(group for group in groups if int(group["id"]) == group_id)

    async def test_settings_page_and_assets_are_served_without_turnstile(self) -> None:
        page = await self.client.get("/settings")
        self.assertEqual(page.status, 200)
        page_text = await page.text()
        self.assertIn("Smart Group Bot", page_text)
        self.assertIn("/settings-assets/lucide.min.js", page_text)
        self.assertNotIn("unpkg.com", page_text)
        self.assertIn(
            "frame-ancestors https://web.telegram.org",
            page.headers["Content-Security-Policy"],
        )

        asset = await self.client.get("/settings-assets/app.js")
        self.assertEqual(asset.status, 200)
        self.assertIn("/api/v1/settings", await asset.text())

        icons = await self.client.get("/settings-assets/lucide.min.js")
        self.assertEqual(icons.status, 200)

    async def test_settings_api_requires_fresh_super_admin_session(self) -> None:
        missing = await self.client.get("/api/v1/settings")
        self.assertEqual(missing.status, 401)

        forged = await self.client.get(
            "/api/v1/settings",
            headers=self._headers(bot_token="43:WRONG_TOKEN"),
        )
        self.assertEqual(forged.status, 401)

        expired = await self.client.get(
            "/api/v1/settings",
            headers=self._headers(auth_date=int(time.time()) - 601),
        )
        self.assertEqual(expired.status, 401)

        future = await self.client.get(
            "/api/v1/settings",
            headers=self._headers(auth_date=int(time.time()) + 61),
        )
        self.assertEqual(future.status, 401)

        ordinary_user = await self.client.get(
            "/api/v1/settings",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(ordinary_user.status, 403)

    async def test_group_admin_is_scoped_to_own_authorized_group(self) -> None:
        async with self.session_factory() as session:
            session.add_all(
                [
                    AuthorizedGroup(group_id=-401, authorized_by=42),
                    AuthorizedGroup(group_id=-402, authorized_by=42),
                    Group(id=-401, title="Own Group", settings={}),
                    Group(id=-402, title="Other Group", settings={}),
                ]
            )
            await session.flush()
            session.add(Admin(group_id=-401, user_id=99, role="admin"))
            await session.commit()

        session_response = await self.client.get(
            "/api/v1/session",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(session_response.status, 200)
        self.assertEqual((await session_response.json())["session"]["role"], "group_admin")

        groups_response = await self.client.get(
            "/api/v1/groups",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(groups_response.status, 200)
        self.assertEqual(
            [group["id"] for group in (await groups_response.json())["groups"]],
            [-401],
        )
        global_response = await self.client.get(
            "/api/v1/settings",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(global_response.status, 403)
        guessed = await self.client.get(
            "/api/v1/groups/-402/rules",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(guessed.status, 403)

    async def test_group_admin_can_manage_rules_and_memories(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-501, authorized_by=42))
            session.add(Group(id=-501, title="Managed Group", settings={}))
            await session.flush()
            session.add(Admin(group_id=-501, user_id=99, role="admin"))
            await session.commit()

        created_rule = await self.client.post(
            "/api/v1/groups/-501/rules",
            headers=self._headers(user_id=99),
            json={"rule_type": "keyword", "pattern": "spam", "action": "delete"},
        )
        self.assertEqual(created_rule.status, 200)
        rule_id = (await created_rule.json())["rule"]["id"]
        created_memory = await self.client.post(
            "/api/v1/groups/-501/memories",
            headers=self._headers(user_id=99),
            json={"content": "Alice is the project lead"},
        )
        self.assertEqual(created_memory.status, 200)
        memory_id = (await created_memory.json())["memory"]["id"]

        async with self.session_factory() as session:
            self.assertEqual((await session.get(ModerationRule, rule_id)).group_id, -501)
            self.assertEqual((await session.get(GroupPermanentMemory, memory_id)).group_id, -501)

    async def test_group_admin_can_manage_keyword_replies(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-503, authorized_by=42))
            session.add(Group(id=-503, title="Keyword Group", settings={}))
            await session.flush()
            session.add(Admin(group_id=-503, user_id=99, role="admin"))
            await session.commit()

        created = await self.client.post(
            "/api/v1/groups/-503/keyword-replies",
            headers=self._headers(user_id=99),
            json={
                "keyword": "签到",
                "match_type": "contains",
                "reply_text": "**欢迎签到**\n请查看下方按钮",
                "buttons": [
                    {
                        "text": "查看说明",
                        "action": "url",
                        "value": "https://example.com/help",
                        "row": 0,
                        "style": "danger",
                    }
                ],
                "pin_message": True,
                "auto_delete": False,
            },
        )
        self.assertEqual(created.status, 200)
        document = (await created.json())["keyword_reply"]
        self.assertTrue(document["pin_message"])
        self.assertFalse(document["auto_delete"])
        self.assertIn("\n", document["reply_text"])
        self.assertEqual(document["buttons"][0]["text"], "查看说明")
        self.assertEqual(document["buttons"][0]["style"], "danger")
        entry_id = document["id"]

        async with self.session_factory() as session:
            row = await session.get(KeywordReply, entry_id)
            self.assertEqual(row.group_id, -503)
            self.assertEqual(row.created_by, 99)
            self.assertEqual(row.buttons[0]["action"], "url")
            self.assertEqual(row.buttons[0]["style"], "danger")

        invalid_regex = await self.client.post(
            "/api/v1/groups/-503/keyword-replies",
            headers=self._headers(user_id=99),
            json={"keyword": "([", "match_type": "regex", "reply_text": "x"},
        )
        self.assertEqual(invalid_regex.status, 400)
        self.assertEqual(
            (await invalid_regex.json())["error"]["code"], "invalid_keyword_regex"
        )

        updated = await self.client.patch(
            f"/api/v1/groups/-503/keyword-replies/{entry_id}",
            headers=self._headers(user_id=99),
            json={"enabled": False, "reply_text": "改后的回复"},
        )
        self.assertEqual(updated.status, 200)
        updated_document = (await updated.json())["keyword_reply"]
        self.assertFalse(updated_document["enabled"])
        self.assertEqual(updated_document["buttons"][0]["style"], "danger")

        other_admin = await self.client.delete(
            f"/api/v1/groups/-503/keyword-replies/{entry_id}",
            headers=self._headers(user_id=100),
        )
        self.assertEqual(other_admin.status, 403)

        deleted = await self.client.delete(
            f"/api/v1/groups/-503/keyword-replies/{entry_id}",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(deleted.status, 200)
        listed = await self.client.get(
            "/api/v1/groups/-503/keyword-replies",
            headers=self._headers(user_id=99),
        )
        self.assertEqual((await listed.json())["keyword_replies"], [])

    async def test_group_admin_can_manage_scheduled_messages(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-504, authorized_by=42))
            session.add(Group(id=-504, title="Schedule Group", settings={}))
            await session.flush()
            session.add(Admin(group_id=-504, user_id=99, role="admin"))
            await session.commit()

        created = await self.client.post(
            "/api/v1/groups/-504/scheduled-messages",
            headers=self._headers(user_id=99),
            json={
                "text": "# 每日公告\n请按时签到",
                "buttons": [
                    {
                        "text": "复制口令",
                        "action": "copy",
                        "value": "CHECK-IN",
                        "row": 0,
                    }
                ],
                "schedule_type": "daily",
                "schedule_time": "9:5",
                "pin_message": True,
                "unpin_previous": True,
                "auto_delete": True,
            },
        )
        self.assertEqual(created.status, 200)
        document = (await created.json())["scheduled_message"]
        self.assertEqual(document["schedule_time"], "09:05")
        self.assertTrue(document["pin_message"])
        self.assertIn("\n", document["text"])
        self.assertEqual(document["buttons"][0]["action"], "copy")
        entry_id = document["id"]

        async with self.session_factory() as session:
            row = await session.get(ScheduledMessage, entry_id)
            self.assertEqual(row.group_id, -504)
            self.assertTrue(row.unpin_previous)
            self.assertEqual(row.buttons[0]["value"], "CHECK-IN")

        bad_time = await self.client.post(
            "/api/v1/groups/-504/scheduled-messages",
            headers=self._headers(user_id=99),
            json={"text": "x", "schedule_time": "25:99"},
        )
        self.assertEqual(bad_time.status, 400)
        self.assertEqual(
            (await bad_time.json())["error"]["code"], "invalid_schedule_time"
        )

        updated = await self.client.patch(
            f"/api/v1/groups/-504/scheduled-messages/{entry_id}",
            headers=self._headers(user_id=99),
            json={"schedule_type": "interval", "interval_minutes": 120},
        )
        self.assertEqual(updated.status, 200)
        self.assertEqual(
            (await updated.json())["scheduled_message"]["interval_minutes"], 120
        )

        deleted = await self.client.delete(
            f"/api/v1/groups/-504/scheduled-messages/{entry_id}",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(deleted.status, 200)
        listed = await self.client.get(
            "/api/v1/groups/-504/scheduled-messages",
            headers=self._headers(user_id=99),
        )
        self.assertEqual((await listed.json())["scheduled_messages"], [])

    async def test_group_admin_list_includes_display_names(self) -> None:
        async with self.session_factory() as session:
            session.add_all(
                [
                    AuthorizedGroup(group_id=-505, authorized_by=42),
                    Group(id=-505, title="Names Group", settings={}),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Admin(group_id=-505, user_id=99, role="admin"),
                    Admin(group_id=-505, user_id=100, role="admin"),
                    Admin(group_id=-505, user_id=101, role="admin"),
                    GroupMember(
                        group_id=-505, user_id=99, full_name="张三", username="zhang"
                    ),
                    GroupMember(group_id=-505, user_id=100, full_name="", username="lisi"),
                ]
            )
            await session.commit()
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(full_name="王五", username="wangwu")
            )
        )

        listed = await self.client.get(
            "/api/v1/groups/-505/admins",
            headers=self._headers(),
        )
        self.assertEqual(listed.status, 200)
        admins = {
            item["user_id"]: item["display_name"]
            for item in (await listed.json())["admins"]
        }
        self.assertEqual(admins[99], "张三")
        self.assertEqual(admins[100], "@lisi")
        self.assertEqual(admins[101], "王五")
        self.bot.get_chat_member.assert_awaited_once_with(-505, 101)

    async def test_group_admin_can_manage_group_bans_without_global_access(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-502, authorized_by=42))
            session.add(Group(id=-502, title="Ban Group", settings={}))
            await session.flush()
            session.add(Admin(group_id=-502, user_id=99, role="admin"))
            session.add(
                GroupMember(
                    group_id=-502,
                    user_id=7001,
                    full_name="测试用户",
                    username="test_user",
                )
            )
            await session.commit()

        created = await self.client.post(
            "/api/v1/groups/-502/bans",
            headers=self._headers(user_id=99),
            json={"user_id": 7001},
        )
        self.assertEqual(created.status, 200)
        self.bot.ban_chat_member.assert_awaited_once_with(
            -502,
            7001,
            revoke_messages=True,
        )

        listed = await self.client.get(
            "/api/v1/groups/-502/bans",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(listed.status, 200)
        listed_ban = (await listed.json())["bans"][0]
        self.assertEqual(listed_ban["user_id"], 7001)
        self.assertEqual(listed_ban["display_name"], "测试用户")
        self.assertEqual(listed_ban["username"], "test_user")

        removed = await self.client.delete(
            "/api/v1/groups/-502/bans/7001",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(removed.status, 200)
        self.bot.unban_chat_member.assert_awaited_once_with(-502, 7001, only_if_banned=True)
        self.bot.restrict_chat_member.assert_awaited_once()

    async def test_failed_group_ban_does_not_leave_false_database_state(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-503, authorized_by=42))
            session.add(Group(id=-503, title="Failure Group", settings={}))
            await session.flush()
            session.add(Admin(group_id=-503, user_id=99, role="admin"))
            await session.commit()
        self.bot.ban_chat_member.side_effect = RuntimeError("telegram unavailable")

        response = await self.client.post(
            "/api/v1/groups/-503/bans",
            headers=self._headers(user_id=99),
            json={"user_id": 7002},
        )
        self.assertEqual(response.status, 502)
        self.bot.ban_chat_member.assert_awaited_once_with(
            -503,
            7002,
            revoke_messages=True,
        )
        async with self.session_factory() as session:
            row = await session.scalar(select(UserWarning).where(
                UserWarning.group_id == -503,
                UserWarning.user_id == 7002,
            ))
        self.assertIsNone(row)

    async def test_global_settings_save_is_masked_and_applies_live(self) -> None:
        response = await self.client.get(
            "/api/v1/settings",
            headers=self._headers(),
        )
        self.assertEqual(response.status, 200)
        document = await response.json()
        payload = document["config"]
        payload["bot"]["enable_typing"] = False

        saved = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={
                "revision": document["revision"],
                "config": payload,
                "secret_changes": {
                    "providers.gemini.api_key": {
                        "action": "replace",
                        "value": "provider-secret",
                    }
                },
            },
        )
        self.assertEqual(saved.status, 200)
        saved_document = await saved.json()
        self.assertEqual(saved_document["revision"], 2)
        self.assertFalse(self.settings.bot.enable_typing)
        self.assertEqual(
            saved_document["config"]["models"]["providers"][0]["api_key"],
            "",
        )
        self.assertIn(
            "providers.gemini.api_key",
            saved_document["configured_secrets"],
        )

        conflict = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": 1, "config": payload, "secret_changes": {}},
        )
        self.assertEqual(conflict.status, 409)

    async def test_group_update_preserves_internal_runtime_state(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-100, authorized_by=42))
            session.add(
                Group(
                    id=-100,
                    title="Test Group",
                    settings={
                        "speech_style": {"sample_count": 12},
                        "scheduled_tasks": {
                            "cooldown_topic": {
                                "enabled": False,
                                "activity_revision": 7,
                                "task_brief": "old",
                            }
                        },
                    },
                )
            )
            await session.commit()

        group_document = await self._group_document(-100)
        response = await self.client.put(
            "/api/v1/groups/-100/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {
                    "av_enabled": True,
                    "mute_all_replies": True,
                    "at_reply_mode": True,
                    "tts_mode": "always",
                    "proactive_enabled": False,
                    "proactive_task_brief": "new brief",
                }
            },
        )
        self.assertEqual(response.status, 200)
        async with self.session_factory() as session:
            row = await session.get(Group, -100)
            self.assertEqual(row.settings["speech_style"]["sample_count"], 12)
            task = row.settings["scheduled_tasks"]["cooldown_topic"]
            self.assertEqual(task["activity_revision"], 7)
            self.assertEqual(task["task_brief"], "new brief")
            self.assertTrue(row.settings["av_enabled"])
            self.assertEqual(row.settings["tts_mode"], "always")

    async def test_group_api_model_query_secrets_are_encrypted_and_isolated(self) -> None:
        async with self.session_factory() as session:
            session.add_all(
                [
                    AuthorizedGroup(group_id=-110, authorized_by=42),
                    AuthorizedGroup(group_id=-111, authorized_by=42),
                    Group(id=-110, title="Model API A", settings={}),
                    Group(id=-111, title="Model API B", settings={}),
                ]
            )
            await session.commit()

        group_a = await self._group_document(-110)
        group_b = await self._group_document(-111)
        first_key = "sk-group-a-first"
        group_b_key = "sk-group-b-only"
        saved_a_response = await self.client.put(
            "/api/v1/groups/-110/settings",
            headers=self._headers(),
            json={
                "revision": group_a["revision"],
                "settings": {
                    "api_model_query_enabled": True,
                    "api_model_query_base_url": "https://api-a.example.com/v1/",
                    "api_model_query_http_timeout_sec": 12,
                    "api_model_query_check_timeout_sec": 34,
                },
                "api_model_query_secret_change": {
                    "action": "replace",
                    "value": first_key,
                },
            },
        )
        self.assertEqual(saved_a_response.status, 200)
        saved_a = (await saved_a_response.json())["group"]
        self.assertNotEqual(saved_a["revision"], group_a["revision"])

        saved_b_response = await self.client.put(
            "/api/v1/groups/-111/settings",
            headers=self._headers(),
            json={
                "revision": group_b["revision"],
                "settings": {
                    "api_model_query_enabled": True,
                    "api_model_query_base_url": "https://api-b.example.com",
                },
                "api_model_query_secret_change": {
                    "action": "replace",
                    "value": group_b_key,
                },
            },
        )
        self.assertEqual(saved_b_response.status, 200)
        saved_b = (await saved_b_response.json())["group"]

        groups_response = await self.client.get(
            "/api/v1/groups",
            headers=self._headers(),
        )
        self.assertEqual(groups_response.status, 200)
        groups = {
            int(item["id"]): item
            for item in (await groups_response.json())["groups"]
        }
        public_a = groups[-110]["settings"]
        public_b = groups[-111]["settings"]
        self.assertTrue(public_a["api_model_query_api_key_configured"])
        self.assertTrue(public_b["api_model_query_api_key_configured"])
        self.assertEqual(
            public_a["api_model_query_base_url"],
            "https://api-a.example.com/v1",
        )
        self.assertEqual(
            public_b["api_model_query_base_url"],
            "https://api-b.example.com",
        )
        for public in (public_a, public_b):
            self.assertNotIn("api_model_query_api_key", public)
            self.assertNotIn("api_key", public)
            self.assertNotIn("ciphertext", public)
        public_payload = json.dumps(groups, ensure_ascii=False)
        self.assertNotIn(first_key, public_payload)
        self.assertNotIn(group_b_key, public_payload)

        cipher = SecretCipher(self.settings.config_master_key)
        async with self.session_factory() as session:
            stored_group_a = await session.get(Group, -110)
            stored_group_b = await session.get(Group, -111)
            secret_a = await session.get(GroupApiModelQuerySecret, -110)
            secret_b = await session.get(GroupApiModelQuerySecret, -111)
            config_a = stored_group_a.settings["api_model_query"]
            config_b = stored_group_b.settings["api_model_query"]
            self.assertNotIn("api_key", config_a)
            self.assertNotIn("ciphertext", config_a)
            self.assertNotIn("api_key", config_b)
            self.assertNotIn("ciphertext", config_b)
            self.assertNotEqual(secret_a.ciphertext, first_key)
            self.assertNotEqual(secret_b.ciphertext, group_b_key)
            self.assertNotEqual(secret_a.ciphertext, secret_b.ciphertext)
            self.assertEqual(cipher.decrypt(secret_a.ciphertext), first_key)
            self.assertEqual(cipher.decrypt(secret_b.ciphertext), group_b_key)

        replacement_key = "sk-group-a-replacement"
        replacement_response = await self.client.put(
            "/api/v1/groups/-110/settings",
            headers=self._headers(),
            json={
                "revision": saved_a["revision"],
                "settings": {},
                "api_model_query_secret_change": {
                    "action": "replace",
                    "value": replacement_key,
                },
            },
        )
        self.assertEqual(replacement_response.status, 200)
        replaced_a = (await replacement_response.json())["group"]
        self.assertNotEqual(replaced_a["revision"], saved_a["revision"])

        stale_response = await self.client.put(
            "/api/v1/groups/-110/settings",
            headers=self._headers(),
            json={
                "revision": saved_a["revision"],
                "settings": {},
                "api_model_query_secret_change": {
                    "action": "replace",
                    "value": "sk-stale-write",
                },
            },
        )
        self.assertEqual(stale_response.status, 409)
        self.assertEqual(
            (await stale_response.json())["error"]["code"],
            "group_revision_conflict",
        )

        async with self.session_factory() as session:
            secret_a = await session.get(GroupApiModelQuerySecret, -110)
            secret_b = await session.get(GroupApiModelQuerySecret, -111)
            self.assertEqual(cipher.decrypt(secret_a.ciphertext), replacement_key)
            self.assertEqual(cipher.decrypt(secret_b.ciphertext), group_b_key)

        refreshed_b = await self._group_document(-111)
        self.assertEqual(refreshed_b["revision"], saved_b["revision"])
        self.assertEqual(
            refreshed_b["settings"]["api_model_query_base_url"],
            "https://api-b.example.com",
        )

    async def test_group_api_model_query_enable_and_clear_validation(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-112, authorized_by=42))
            session.add(Group(id=-112, title="Model API Validation", settings={}))
            await session.commit()

        initial = await self._group_document(-112)
        missing_url = await self.client.put(
            "/api/v1/groups/-112/settings",
            headers=self._headers(),
            json={
                "revision": initial["revision"],
                "settings": {"api_model_query_enabled": True},
                "api_model_query_secret_change": {
                    "action": "replace",
                    "value": "sk-rolled-back",
                },
            },
        )
        self.assertEqual(missing_url.status, 400)
        self.assertEqual(
            (await missing_url.json())["error"]["code"],
            "api_model_query_not_configured",
        )
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(GroupApiModelQuerySecret, -112))

        missing_key = await self.client.put(
            "/api/v1/groups/-112/settings",
            headers=self._headers(),
            json={
                "revision": initial["revision"],
                "settings": {
                    "api_model_query_enabled": True,
                    "api_model_query_base_url": "https://api.example.com",
                },
            },
        )
        self.assertEqual(missing_key.status, 400)
        self.assertEqual(
            (await missing_key.json())["error"]["code"],
            "api_model_query_key_required",
        )

        enabled_response = await self.client.put(
            "/api/v1/groups/-112/settings",
            headers=self._headers(),
            json={
                "revision": initial["revision"],
                "settings": {
                    "api_model_query_enabled": True,
                    "api_model_query_base_url": "https://api.example.com",
                },
                "api_model_query_secret_change": {
                    "action": "replace",
                    "value": "sk-enabled",
                },
            },
        )
        self.assertEqual(enabled_response.status, 200)
        enabled = (await enabled_response.json())["group"]

        clear_while_enabled = await self.client.put(
            "/api/v1/groups/-112/settings",
            headers=self._headers(),
            json={
                "revision": enabled["revision"],
                "settings": {},
                "api_model_query_secret_change": {"action": "clear"},
            },
        )
        self.assertEqual(clear_while_enabled.status, 400)
        self.assertEqual(
            (await clear_while_enabled.json())["error"]["code"],
            "api_model_query_key_required",
        )
        unchanged = await self._group_document(-112)
        self.assertEqual(unchanged["revision"], enabled["revision"])
        self.assertTrue(
            unchanged["settings"]["api_model_query_api_key_configured"]
        )
        async with self.session_factory() as session:
            self.assertIsNotNone(await session.get(GroupApiModelQuerySecret, -112))

        cleared_response = await self.client.put(
            "/api/v1/groups/-112/settings",
            headers=self._headers(),
            json={
                "revision": enabled["revision"],
                "settings": {"api_model_query_enabled": False},
                "api_model_query_secret_change": {"action": "clear"},
            },
        )
        self.assertEqual(cleared_response.status, 200)
        cleared = (await cleared_response.json())["group"]
        self.assertFalse(cleared["settings"]["api_model_query_enabled"])
        self.assertFalse(
            cleared["settings"]["api_model_query_api_key_configured"]
        )
        self.assertNotEqual(cleared["revision"], enabled["revision"])
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(GroupApiModelQuerySecret, -112))

    async def test_partial_group_update_preserves_proactive_inheritance(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-200, authorized_by=42))
            session.add(
                Group(
                    id=-200,
                    title="Inherited Group",
                    settings={"scheduled_tasks": {"cooldown_topic": {}}},
                )
            )
            await session.commit()

        group_document = await self._group_document(-200)
        response = await self.client.put(
            "/api/v1/groups/-200/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"tts_mode": "on"},
            },
        )
        self.assertEqual(response.status, 200)
        saved_group = (await response.json())["group"]
        self.assertIsNone(saved_group["settings"]["proactive_enabled"])
        stale = await self.client.put(
            "/api/v1/groups/-200/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"av_enabled": True},
            },
        )
        self.assertEqual(stale.status, 409)
        async with self.session_factory() as session:
            row = await session.get(Group, -200)
            state = row.settings["scheduled_tasks"]["cooldown_topic"]
            self.assertNotIn("enabled", state)

        explicit = await self.client.put(
            "/api/v1/groups/-200/settings",
            headers=self._headers(),
            json={
                "revision": saved_group["revision"],
                "settings": {"proactive_enabled": True},
            },
        )
        self.assertEqual(explicit.status, 200)
        explicit_group = (await explicit.json())["group"]
        inherited = await self.client.put(
            "/api/v1/groups/-200/settings",
            headers=self._headers(),
            json={
                "revision": explicit_group["revision"],
                "settings": {"proactive_enabled": None},
            },
        )
        self.assertEqual(inherited.status, 200)
        self.assertIsNone((await inherited.json())["group"]["settings"]["proactive_enabled"])

    async def test_group_join_verification_supports_override_and_inheritance(self) -> None:
        self.settings.join_verification_hcaptcha_site_key = "h-site"
        self.settings.join_verification_hcaptcha_secret_key = "h-secret"
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-250, authorized_by=42))
            session.add(Group(id=-250, title="Captcha Group", settings={}))
            await session.commit()

        group_document = await self._group_document(-250)
        explicit = await self.client.put(
            "/api/v1/groups/-250/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {
                    "join_verification_enabled": True,
                    "join_verification_provider": "hcaptcha",
                },
            },
        )
        self.assertEqual(explicit.status, 200)
        explicit_group = (await explicit.json())["group"]
        self.assertTrue(explicit_group["settings"]["join_verification_enabled"])
        self.assertEqual(
            explicit_group["settings"]["join_verification_provider"],
            "hcaptcha",
        )

        inherited = await self.client.put(
            "/api/v1/groups/-250/settings",
            headers=self._headers(),
            json={
                "revision": explicit_group["revision"],
                "settings": {
                    "join_verification_enabled": None,
                    "join_verification_provider": None,
                },
            },
        )
        self.assertEqual(inherited.status, 200)
        inherited_settings = (await inherited.json())["group"]["settings"]
        self.assertIsNone(inherited_settings["join_verification_enabled"])
        self.assertIsNone(inherited_settings["join_verification_provider"])

    async def test_group_welcome_message_roundtrip_and_clear(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-260, authorized_by=42))
            session.add(Group(id=-260, title="Welcome Group", settings={}))
            await session.commit()

        group_document = await self._group_document(-260)
        saved = await self.client.put(
            "/api/v1/groups/-260/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {
                    "welcome_message": "**欢迎**\n{mention} 加入！",
                    "welcome_buttons": [
                        {
                            "text": "群规",
                            "action": "url",
                            "value": "https://example.com/rules",
                            "row": 0,
                            "style": "success",
                        }
                    ],
                },
            },
        )
        self.assertEqual(saved.status, 200)
        saved_group = (await saved.json())["group"]
        self.assertEqual(
            saved_group["settings"]["welcome_message"], "**欢迎**\n{mention} 加入！"
        )
        self.assertEqual(saved_group["settings"]["welcome_buttons"][0]["text"], "群规")
        self.assertEqual(
            saved_group["settings"]["welcome_buttons"][0]["style"], "success"
        )
        async with self.session_factory() as session:
            stored = await session.get(Group, -260)
            self.assertEqual(
                stored.settings.get("welcome_message"), "**欢迎**\n{mention} 加入！"
            )
            self.assertEqual(
                stored.settings["welcome_buttons"][0]["style"], "success"
            )

        cleared = await self.client.put(
            "/api/v1/groups/-260/settings",
            headers=self._headers(),
            json={
                "revision": saved_group["revision"],
                "settings": {"welcome_message": "", "welcome_buttons": []},
            },
        )
        self.assertEqual(cleared.status, 200)
        cleared_group = (await cleared.json())["group"]
        self.assertEqual(cleared_group["settings"]["welcome_message"], "")
        self.assertEqual(cleared_group["settings"]["welcome_buttons"], [])
        async with self.session_factory() as session:
            stored = await session.get(Group, -260)
            self.assertNotIn("welcome_message", stored.settings or {})
            self.assertNotIn("welcome_buttons", stored.settings or {})

    async def test_group_default_permissions_load_and_schedule_roundtrip(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-261, authorized_by=42))
            session.add(Group(id=-261, title="Permissions Group", settings={}))
            await session.commit()

        live_permissions = {
            field: field != "can_pin_messages" for field in PERMISSION_FIELDS
        }
        self.bot.get_chat = AsyncMock(
            return_value=SimpleNamespace(
                permissions=ChatPermissions(**live_permissions)
            )
        )
        loaded = await self.client.get(
            "/api/v1/groups/-261/default-permissions",
            headers=self._headers(),
        )
        self.assertEqual(loaded.status, 200)
        loaded_body = await loaded.json()
        self.assertFalse(loaded_body["configured"])
        self.assertFalse(
            loaded_body["default_permissions"]["base"]["can_pin_messages"]
        )
        self.assertEqual(
            len(loaded_body["permission_fields"]),
            len(PERMISSION_FIELDS),
        )

        config = loaded_body["default_permissions"]
        config["schedule_enabled"] = True
        config["windows"] = [
            {
                "id": "night",
                "name": "夜间禁图",
                "enabled": True,
                "start": "23:00",
                "end": "07:00",
                "days": list(range(7)),
                "priority": 0,
                "overrides": {"can_send_photos": False},
            }
        ]
        group_document = await self._group_document(-261)
        saved = await self.client.put(
            "/api/v1/groups/-261/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"default_permissions": config},
            },
        )
        self.assertEqual(saved.status, 200)
        public = (await saved.json())["group"]["settings"]["default_permissions"]
        self.assertTrue(public["schedule_enabled"])
        self.assertFalse(public["windows"][0]["overrides"]["can_send_photos"])
        async with self.session_factory() as session:
            stored = await session.get(Group, -261)
            self.assertEqual(
                set(stored.settings["default_permissions"]["base"]),
                set(PERMISSION_FIELDS),
            )

    async def test_invalid_legacy_group_permissions_can_be_repaired_from_telegram(self) -> None:
        legacy_base = {field: True for field in PERMISSION_FIELDS}
        legacy_base.pop("can_edit_tag")
        legacy_base["can_send_photos"] = False
        legacy = {
            "version": 1,
            "timezone": "Asia/Shanghai",
            "schedule_enabled": True,
            "base": legacy_base,
            "windows": [
                {
                    "id": "night",
                    "name": "夜间禁图",
                    "enabled": True,
                    "start": "23:00",
                    "end": "07:00",
                    "days": list(range(7)),
                    "priority": 0,
                    "overrides": {"can_send_photos": False},
                }
            ],
        }
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-262, authorized_by=42))
            session.add(
                Group(
                    id=-262,
                    title="Legacy Permissions",
                    settings={"default_permissions": legacy},
                )
            )
            await session.commit()
        live_permissions = {field: True for field in PERMISSION_FIELDS}
        live_permissions["can_edit_tag"] = False
        self.bot.get_chat = AsyncMock(
            return_value=SimpleNamespace(
                permissions=ChatPermissions(**live_permissions)
            )
        )

        loaded = await self.client.get(
            "/api/v1/groups/-262/default-permissions",
            headers=self._headers(),
        )

        self.assertEqual(loaded.status, 200)
        body = await loaded.json()
        self.assertTrue(body["repaired"])
        self.assertFalse(body["configured"])
        self.assertFalse(body["default_permissions"]["base"]["can_edit_tag"])
        self.assertFalse(body["default_permissions"]["base"]["can_send_photos"])
        self.assertEqual(body["default_permissions"]["windows"][0]["id"], "night")

    async def test_per_category_auto_delete_seconds_apply_live(self) -> None:
        document = self.manager.api_document()
        payload = document["config"]
        payload["bot"]["auto_delete_seconds"] = 30
        payload["bot"]["auto_delete_categories"] = ["welcome", "keyword"]
        payload["bot"]["auto_delete_category_seconds"] = {"welcome": 90, "keyword": 0}
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": document["revision"], "config": payload},
        )
        self.assertEqual(response.status, 200)
        saved = (await response.json())["config"]
        # Zero means inherit and must not persist as an override.
        self.assertEqual(
            saved["bot"]["auto_delete_category_seconds"], {"welcome": 90}
        )
        self.assertEqual(
            configured_auto_delete_seconds(self.settings, "welcome"), 90
        )
        self.assertEqual(
            configured_auto_delete_seconds(self.settings, "keyword"), 30
        )
        self.assertEqual(
            configured_auto_delete_seconds(self.settings, "moderation"), 0
        )

    async def test_mimic_target_and_profile_are_visually_configurable(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-300, authorized_by=42))
            session.add(Group(id=-300, title="Style Group", settings={}))
            session.add(
                SpeechStyleSample(
                    group_id=-300,
                    user_id=1,
                    content="legacy sample",
                )
            )
            await session.commit()

        group_document = await self._group_document(-300)
        response = await self.client.put(
            "/api/v1/groups/-300/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {
                    "mimic_target_user_id": 777,
                    "mimic_target_user_name": "Target User",
                    "mimic_profile_text": "短句，语气直接。",
                }
            },
        )
        self.assertEqual(response.status, 200)
        public = (await response.json())["group"]["settings"]
        self.assertEqual(public["mimic_target_user_id"], 777)
        self.assertEqual(public["mimic_target_user_name"], "Target User")
        self.assertEqual(public["mimic_profile_text"], "短句，语气直接。")

        async with self.session_factory() as session:
            samples = await session.execute(
                select(SpeechStyleSample).where(
                    SpeechStyleSample.group_id == -300
                )
            )
            self.assertEqual(samples.scalars().all(), [])


    async def test_call_admin_and_vote_ban_group_settings_roundtrip(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-270, authorized_by=42))
            session.add(Group(id=-270, title="CallAdmin Group", settings={}))
            await session.commit()

        group_document = await self._group_document(-270)
        self.assertIsNone(group_document["settings"]["raid_guard_pin_message"])
        self.assertIsNone(group_document["settings"]["call_admin_enabled"])
        self.assertIsNone(group_document["settings"]["call_admin_pin_message"])
        self.assertEqual(group_document["settings"]["call_admin_targets"], [])
        self.assertIsNone(group_document["settings"]["vote_ban_enabled"])
        self.assertIsNone(group_document["settings"]["vote_ban_pin_message"])
        self.assertIsNone(group_document["settings"]["vote_ban_trigger_limit"])
        self.assertIsNone(group_document["settings"]["vote_ban_trigger_window_seconds"])

        saved = await self.client.put(
            "/api/v1/groups/-270/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {
                    "raid_guard_pin_message": True,
                    "call_admin_enabled": True,
                    "call_admin_pin_message": True,
                    "call_admin_targets": [9, 7, 7],
                    "vote_ban_enabled": True,
                    "vote_ban_pin_message": False,
                    "vote_ban_threshold": 8,
                    "vote_ban_duration_seconds": 900,
                    "vote_ban_trigger_limit": 4,
                    "vote_ban_trigger_window_seconds": 7200,
                },
            },
        )
        self.assertEqual(saved.status, 200)
        public = (await saved.json())["group"]["settings"]
        self.assertTrue(public["raid_guard_pin_message"])
        self.assertTrue(public["call_admin_enabled"])
        self.assertTrue(public["call_admin_pin_message"])
        self.assertEqual(public["call_admin_targets"], [7, 9])
        self.assertTrue(public["vote_ban_enabled"])
        self.assertFalse(public["vote_ban_pin_message"])
        self.assertEqual(public["vote_ban_threshold"], 8)
        self.assertEqual(public["vote_ban_duration_seconds"], 900)
        self.assertEqual(public["vote_ban_trigger_limit"], 4)
        self.assertEqual(public["vote_ban_trigger_window_seconds"], 7200)

        # Empty targets = "all admins" and clears the stored key; nulls
        # clear the overrides back to inheritance.
        cleared = await self.client.put(
            "/api/v1/groups/-270/settings",
            headers=self._headers(),
            json={
                "revision": (await saved.json())["group"]["revision"],
                "settings": {
                    "raid_guard_pin_message": None,
                    "call_admin_enabled": None,
                    "call_admin_pin_message": None,
                    "call_admin_targets": [],
                    "vote_ban_enabled": None,
                    "vote_ban_pin_message": None,
                    "vote_ban_threshold": None,
                    "vote_ban_duration_seconds": None,
                    "vote_ban_trigger_limit": None,
                    "vote_ban_trigger_window_seconds": None,
                },
            },
        )
        self.assertEqual(cleared.status, 200)
        async with self.session_factory() as session:
            stored = await session.get(Group, -270)
            for key in (
                "raid_guard_pin_message",
                "call_admin_enabled",
                "call_admin_pin_message",
                "call_admin_targets",
                "vote_ban_enabled",
                "vote_ban_pin_message",
                "vote_ban_threshold",
                "vote_ban_duration_seconds",
                "vote_ban_trigger_limit",
                "vote_ban_trigger_window_seconds",
            ):
                self.assertNotIn(key, stored.settings or {})

    async def test_vote_ban_threshold_bounds_rejected(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-271, authorized_by=42))
            session.add(Group(id=-271, title="Bounds Group", settings={}))
            await session.commit()
        group_document = await self._group_document(-271)
        response = await self.client.put(
            "/api/v1/groups/-271/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"vote_ban_threshold": 1},
            },
        )
        self.assertEqual(response.status, 400)

        response = await self.client.put(
            "/api/v1/groups/-271/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"vote_ban_pin_message_typo": True},
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(
            (await response.json())["error"]["code"], "unknown_group_settings"
        )

        response = await self.client.put(
            "/api/v1/groups/-271/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"call_admin_targets": [0]},
            },
        )
        self.assertEqual(response.status, 400)

        response = await self.client.put(
            "/api/v1/groups/-271/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"vote_ban_trigger_limit": 0},
            },
        )
        self.assertEqual(response.status, 400)

        response = await self.client.put(
            "/api/v1/groups/-271/settings",
            headers=self._headers(),
            json={
                "revision": group_document["revision"],
                "settings": {"vote_ban_trigger_window_seconds": 59},
            },
        )
        self.assertEqual(response.status, 400)

    async def test_telegram_admins_endpoint_group_admin_access(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-272, authorized_by=42))
            session.add(Group(id=-272, title="Admins Group", settings={}))
            await session.flush()
            session.add(Admin(group_id=-272, user_id=99, role="admin"))
            session.add(
                GroupMember(
                    group_id=-272, user_id=99, full_name="管理甲", username="ga"
                )
            )
            await session.commit()

        # No live Telegram lookup on the stub bot: falls back to the local
        # admin table with roster display names.
        response = await self.client.get(
            "/api/v1/groups/-272/telegram-admins",
            headers=self._headers(99),
        )
        self.assertEqual(response.status, 200)
        admins = (await response.json())["admins"]
        self.assertEqual(admins[0]["user_id"], 99)
        self.assertEqual(admins[0]["display_name"], "管理甲")

        denied = await self.client.get(
            "/api/v1/groups/-272/telegram-admins",
            headers=self._headers(1234),
        )
        self.assertEqual(denied.status, 403)

    async def test_auto_delete_mode_map_roundtrip(self) -> None:
        document = self.manager.api_document()
        payload = document["config"]
        payload["bot"]["auto_delete_categories"] = ["moderation", "vote"]
        payload["bot"]["auto_delete_category_mode"] = {
            "moderation": "button",
            "vote": "timer",
        }
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": document["revision"], "config": payload},
        )
        self.assertEqual(response.status, 200)
        saved = (await response.json())["config"]
        # "timer" is the default and must not persist.
        self.assertEqual(
            saved["bot"]["auto_delete_category_mode"], {"moderation": "button"}
        )
        self.assertEqual(
            self.settings.bot.auto_delete_category_mode, {"moderation": "button"}
        )


if __name__ == "__main__":
    unittest.main()
