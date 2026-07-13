import os
import tempfile
import unittest

from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import RuntimeConfigRecord, RuntimeConfigSecret
from bot.services.runtime_config import (
    RuntimeConfigConflictError,
    RuntimeConfigManager,
)


class RuntimeConfigManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token="42:TEST_TOKEN",
            super_admin_id=42,
            config_master_key="unit-test-master-key",
        )
        self.settings.bot.token = self.settings.bot_token
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-smart-group-bot.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def test_first_start_persists_defaults_and_applies_them(self) -> None:
        self.assertEqual(self.manager.revision, 1)
        self.assertEqual(self.settings.bot.main_model.model, "gemini/gemini-2.0-flash")
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            self.assertIsNotNone(row)
            self.assertEqual(row.revision, 1)
            self.assertEqual(row.payload["models"]["providers"][0]["api_key"], "")

    async def test_secret_is_encrypted_masked_and_preserved_on_regular_save(self) -> None:
        payload = self.manager.config.public_payload()
        await self.manager.save(
            payload,
            expected_revision=1,
            updated_by=42,
            secret_changes={
                "providers.gemini.api_key": {
                    "action": "replace",
                    "value": "top-secret-key",
                }
            },
        )

        document = self.manager.api_document()
        self.assertIn("providers.gemini.api_key", document["configured_secrets"])
        self.assertEqual(
            document["config"]["models"]["providers"][0]["api_key"],
            "",
        )
        async with self.session_factory() as session:
            result = await session.execute(select(RuntimeConfigSecret))
            row = result.scalar_one()
            self.assertNotIn("top-secret-key", row.ciphertext)

        payload = self.manager.config.public_payload()
        payload["bot"]["enable_typing"] = False
        await self.manager.save(
            payload,
            expected_revision=2,
            updated_by=42,
        )
        self.assertFalse(self.settings.bot.enable_typing)
        self.assertEqual(
            self.manager.config.models.providers[0].api_key,
            "top-secret-key",
        )

    async def test_secret_can_be_cleared_explicitly(self) -> None:
        await self.manager.save(
            self.manager.config.public_payload(),
            expected_revision=1,
            updated_by=42,
            secret_changes={
                "providers.gemini.api_key": {
                    "action": "replace",
                    "value": "secret",
                }
            },
        )
        await self.manager.save(
            self.manager.config.public_payload(),
            expected_revision=2,
            updated_by=42,
            secret_changes={
                "providers.gemini.api_key": {"action": "clear"}
            },
        )
        self.assertNotIn(
            "providers.gemini.api_key",
            self.manager.api_document()["configured_secrets"],
        )
        async with self.session_factory() as session:
            result = await session.execute(select(RuntimeConfigSecret))
            self.assertEqual(result.scalars().all(), [])

    async def test_revision_conflict_does_not_mutate_live_settings(self) -> None:
        payload = self.manager.config.public_payload()
        payload["bot"]["enable_streaming"] = False
        with self.assertRaises(RuntimeConfigConflictError):
            await self.manager.save(
                payload,
                expected_revision=999,
                updated_by=42,
            )
        self.assertTrue(self.settings.bot.enable_streaming)
        self.assertEqual(self.manager.revision, 1)

    async def test_join_verification_cannot_be_enabled_incompletely(self) -> None:
        payload = self.manager.config.public_payload()
        payload["verification"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "Turnstile Site Key"):
            await self.manager.save(
                payload,
                expected_revision=1,
                updated_by=42,
            )
        self.assertFalse(self.settings.join_verification_enabled)
        self.assertEqual(self.manager.revision, 1)

    async def test_invalid_moderation_prompt_is_rejected_before_save(self) -> None:
        payload = self.manager.config.public_payload()
        payload["prompts"]["moderation"] = "rules={rules_json}\njson={broken}"
        with self.assertRaisesRegex(ValueError, "花括号或占位符无效"):
            await self.manager.save(
                payload,
                expected_revision=1,
                updated_by=42,
            )
        self.assertEqual(self.manager.revision, 1)

    async def test_reload_restores_encrypted_secret(self) -> None:
        await self.manager.save(
            self.manager.config.public_payload(),
            expected_revision=1,
            updated_by=42,
            secret_changes={
                "sub2api.api_key": {"action": "replace", "value": "gateway-key"}
            },
        )
        reloaded_settings = Settings(
            _env_file=None,
            bot_token="42:TEST_TOKEN",
            super_admin_id=42,
            config_master_key="unit-test-master-key",
        )
        reloaded_settings.bot.token = reloaded_settings.bot_token
        reloaded = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=reloaded_settings,
            legacy_raw_env={},
        )
        await reloaded.initialize()
        self.assertEqual(reloaded.revision, 2)
        self.assertEqual(reloaded.config.sub2api.api_key, "gateway-key")

    async def test_invalid_legacy_provider_does_not_create_config_row(self) -> None:
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            await session.delete(row)
            await session.commit()

        invalid = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-smart-group-bot.toml",
            legacy_raw_env={"MODEL_PROVIDER_BROKEN_API_KEY": "secret"},
        )
        with self.assertRaisesRegex(ValueError, "PROVIDER is required"):
            await invalid.initialize()
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(RuntimeConfigRecord, 1))


if __name__ == "__main__":
    unittest.main()
