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

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    Group,
    GroupPermanentMemory,
    KeywordReply,
    ModerationRule,
    ScheduledMessage,
    SpeechStyleSample,
    UserWarning,
)
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

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
                    Admin(group_id=-401, user_id=99, role="admin"),
                ]
            )
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
            session.add(Admin(group_id=-503, user_id=99, role="admin"))
            await session.commit()

        created = await self.client.post(
            "/api/v1/groups/-503/keyword-replies",
            headers=self._headers(user_id=99),
            json={
                "keyword": "签到",
                "match_type": "contains",
                "reply_text": "欢迎签到！",
                "pin_message": True,
                "auto_delete": False,
            },
        )
        self.assertEqual(created.status, 200)
        document = (await created.json())["keyword_reply"]
        self.assertTrue(document["pin_message"])
        self.assertFalse(document["auto_delete"])
        entry_id = document["id"]

        async with self.session_factory() as session:
            row = await session.get(KeywordReply, entry_id)
            self.assertEqual(row.group_id, -503)
            self.assertEqual(row.created_by, 99)

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
        self.assertFalse((await updated.json())["keyword_reply"]["enabled"])

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
            session.add(Admin(group_id=-504, user_id=99, role="admin"))
            await session.commit()

        created = await self.client.post(
            "/api/v1/groups/-504/scheduled-messages",
            headers=self._headers(user_id=99),
            json={
                "text": "每日公告",
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
        entry_id = document["id"]

        async with self.session_factory() as session:
            row = await session.get(ScheduledMessage, entry_id)
            self.assertEqual(row.group_id, -504)
            self.assertTrue(row.unpin_previous)

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

    async def test_group_admin_can_manage_group_bans_without_global_access(self) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-502, authorized_by=42))
            session.add(Group(id=-502, title="Ban Group", settings={}))
            session.add(Admin(group_id=-502, user_id=99, role="admin"))
            await session.commit()

        created = await self.client.post(
            "/api/v1/groups/-502/bans",
            headers=self._headers(user_id=99),
            json={"user_id": 7001},
        )
        self.assertEqual(created.status, 200)
        self.bot.ban_chat_member.assert_awaited_once_with(-502, 7001)

        listed = await self.client.get(
            "/api/v1/groups/-502/bans",
            headers=self._headers(user_id=99),
        )
        self.assertEqual(listed.status, 200)
        self.assertEqual((await listed.json())["bans"][0]["user_id"], 7001)

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
            session.add(Admin(group_id=-503, user_id=99, role="admin"))
            await session.commit()
        self.bot.ban_chat_member.side_effect = RuntimeError("telegram unavailable")

        response = await self.client.post(
            "/api/v1/groups/-503/bans",
            headers=self._headers(user_id=99),
            json={"user_id": 7002},
        )
        self.assertEqual(response.status, 502)
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


if __name__ == "__main__":
    unittest.main()
