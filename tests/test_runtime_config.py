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
    SecretCipher,
    build_legacy_runtime_config,
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
        self.assertEqual(self.settings.bot.main_model.max_tokens, 2048)
        self.assertEqual(self.settings.bot.max_context_tokens, 256000)
        self.assertEqual(self.settings.bot.max_output_tokens, 2048)
        self.assertEqual(self.settings.bot.reply_batch_timeout_seconds, 45.0)
        self.assertTrue(self.settings.raid_guard_pin_message)
        self.assertFalse(self.settings.call_admin_pin_message)
        self.assertTrue(self.settings.vote_ban_pin_message)
        self.assertTrue(self.manager.config.raid_guard.pin_message)
        self.assertFalse(self.manager.config.call_admin.pin_message)
        self.assertTrue(self.manager.config.vote_ban.pin_message)
        self.assertEqual(self.settings.vote_ban_trigger_limit, 3)
        self.assertEqual(self.settings.vote_ban_trigger_window_seconds, 3600)
        self.assertFalse(self.manager.config.movie_info.enabled)
        self.assertEqual(self.manager.config.movie_info.http_timeout_sec, 6.0)
        self.assertEqual(self.manager.config.movie_info.max_results, 6)
        self.assertEqual(self.manager.config.movie_info.default_language, "zh-CN")
        self.assertEqual(self.manager.config.movie_info.default_region, "CN")
        self.assertFalse(self.settings.movie_info_enabled)
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            self.assertIsNotNone(row)
            self.assertEqual(row.revision, 1)
            self.assertEqual(row.payload["models"]["providers"][0]["api_key"], "")
            self.assertEqual(row.payload["movie_info"]["tmdb_read_access_token"], "")

    async def test_first_start_imports_activity_pin_and_auto_delete_settings(self) -> None:
        legacy = Settings(_env_file=None)
        legacy.raid_guard_pin_message = False
        legacy.call_admin_enabled = False
        legacy.call_admin_pin_message = True
        legacy.call_admin_cooldown_seconds = 123
        legacy.vote_ban_enabled = True
        legacy.vote_ban_pin_message = False
        legacy.vote_ban_threshold = 9
        legacy.vote_ban_duration_seconds = 2400
        legacy.vote_ban_trigger_limit = 7
        legacy.vote_ban_trigger_window_seconds = 5400
        legacy.bot.auto_delete_categories = ["reply", "call_admin", "vote"]
        legacy.bot.auto_delete_category_seconds = {"vote": 90}
        legacy.bot.auto_delete_category_mode = {"call_admin": "button"}
        legacy.bot.drop_pending_updates = True

        imported = build_legacy_runtime_config(
            "/tmp/nonexistent-smart-group-bot.toml",
            settings=legacy,
            raw_env={},
        )

        self.assertFalse(imported.raid_guard.pin_message)
        self.assertFalse(imported.call_admin.enabled)
        self.assertTrue(imported.call_admin.pin_message)
        self.assertEqual(imported.call_admin.cooldown_seconds, 123)
        self.assertTrue(imported.vote_ban.enabled)
        self.assertFalse(imported.vote_ban.pin_message)
        self.assertEqual(imported.vote_ban.vote_threshold, 9)
        self.assertEqual(imported.vote_ban.duration_seconds, 2400)
        self.assertEqual(imported.vote_ban.trigger_limit, 7)
        self.assertEqual(imported.vote_ban.trigger_window_seconds, 5400)
        self.assertEqual(
            imported.bot.auto_delete_categories,
            ["reply", "call_admin", "vote"],
        )
        self.assertEqual(imported.bot.auto_delete_category_seconds, {"vote": 90})
        self.assertEqual(
            imported.bot.auto_delete_category_mode,
            {"call_admin": "button"},
        )
        self.assertFalse(imported.bot.drop_pending_updates)

    async def test_activity_pin_settings_save_apply_and_stay_strict(self) -> None:
        payload = self.manager.config.public_payload()
        payload["raid_guard"]["pin_message"] = False
        payload["call_admin"]["pin_message"] = True
        payload["vote_ban"]["pin_message"] = False

        await self.manager.save(payload, expected_revision=1, updated_by=42)

        self.assertFalse(self.settings.raid_guard_pin_message)
        self.assertTrue(self.settings.call_admin_pin_message)
        self.assertFalse(self.settings.vote_ban_pin_message)
        document = self.manager.api_document()["config"]
        self.assertFalse(document["raid_guard"]["pin_message"])
        self.assertTrue(document["call_admin"]["pin_message"])
        self.assertFalse(document["vote_ban"]["pin_message"])

        invalid = self.manager.config.public_payload()
        invalid["vote_ban"]["pin_message_typo"] = True
        with self.assertRaises(ValueError):
            await self.manager.save(invalid, expected_revision=2, updated_by=42)
        self.assertEqual(self.manager.revision, 2)

    async def test_legacy_movie_info_settings_are_imported(self) -> None:
        legacy = Settings(
            _env_file=None,
            movie_info_enabled=True,
            movie_info_http_timeout_sec=5.5,
            movie_info_max_results=9,
            movie_info_default_language="en-US",
            movie_info_default_region="US",
            movie_info_tmdb_read_access_token="legacy-tmdb-token",
            movie_info_imdb_data_set_id="legacy-data-set",
            movie_info_imdb_revision_id="legacy-revision",
            movie_info_imdb_asset_id="legacy-asset",
            movie_info_imdb_api_key="legacy-imdb-key",
            movie_info_imdb_aws_access_key_id="legacy-access-key-id",
            movie_info_imdb_aws_secret_access_key="legacy-secret-key",
            movie_info_imdb_aws_session_token="legacy-session-token",
        )

        imported = build_legacy_runtime_config(
            "/tmp/nonexistent-smart-group-bot.toml",
            settings=legacy,
            raw_env={},
        )

        self.assertTrue(imported.movie_info.enabled)
        self.assertEqual(imported.movie_info.http_timeout_sec, 5.5)
        self.assertEqual(imported.movie_info.max_results, 9)
        self.assertEqual(imported.movie_info.default_language, "en-US")
        self.assertEqual(imported.movie_info.default_region, "US")
        self.assertEqual(imported.movie_info.imdb_data_set_id, "legacy-data-set")
        self.assertEqual(imported.movie_info.imdb_revision_id, "legacy-revision")
        self.assertEqual(imported.movie_info.imdb_asset_id, "legacy-asset")
        self.assertEqual(
            imported.extract_secrets(),
            {
                "movie_info.tmdb_read_access_token": "legacy-tmdb-token",
                "movie_info.imdb_api_key": "legacy-imdb-key",
                "movie_info.imdb_aws_access_key_id": "legacy-access-key-id",
                "movie_info.imdb_aws_secret_access_key": "legacy-secret-key",
                "movie_info.imdb_aws_session_token": "legacy-session-token",
            },
        )

    async def test_deprecated_drop_pending_setting_is_normalized_on_save(self) -> None:
        payload = self.manager.config.public_payload()
        payload["bot"]["drop_pending_updates"] = True

        await self.manager.save(
            payload,
            expected_revision=1,
            updated_by=42,
        )

        self.assertFalse(self.manager.config.bot.drop_pending_updates)
        self.assertFalse(self.settings.bot.drop_pending_updates)
        self.assertNotIn(
            "bot.drop_pending_updates",
            self.manager.api_document()["restart_required_paths"],
        )
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            self.assertFalse(row.payload["bot"]["drop_pending_updates"])

    async def test_deprecated_drop_pending_row_migrates_without_revision_bump(self) -> None:
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            payload = dict(row.payload)
            bot_payload = dict(payload["bot"])
            bot_payload["drop_pending_updates"] = True
            payload["bot"] = bot_payload
            payload["sub2api"] = {
                "enabled": True,
                "base_url": "https://retired.example.com",
                "http_timeout_sec": 15.0,
                "check_timeout_sec": 45.0,
            }
            row.payload = payload
            row.revision = 7
            session.add(
                RuntimeConfigSecret(
                    name="sub2api.api_key",
                    ciphertext=SecretCipher(
                        self.settings.config_master_key
                    ).encrypt("retired-global-key"),
                    updated_by=42,
                )
            )
            await session.commit()

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

        self.assertEqual(reloaded.revision, 7)
        self.assertFalse(reloaded.config.bot.drop_pending_updates)
        self.assertFalse(reloaded_settings.bot.drop_pending_updates)
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            self.assertEqual(row.revision, 7)
            self.assertFalse(row.payload["bot"]["drop_pending_updates"])
            self.assertNotIn("sub2api", row.payload)
            self.assertIsNone(
                await session.get(RuntimeConfigSecret, "sub2api.api_key")
            )

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

    async def test_movie_info_settings_apply_and_secrets_are_masked(self) -> None:
        payload = self.manager.config.public_payload()
        payload["movie_info"].update(
            {
                "enabled": True,
                "http_timeout_sec": 5.5,
                "max_results": 8,
                "default_language": "ja-JP",
                "default_region": "JP",
                "imdb_data_set_id": "dataset-1",
                "imdb_revision_id": "revision-2",
                "imdb_asset_id": "asset-3",
            }
        )
        secrets = {
            "movie_info.tmdb_read_access_token": "tmdb-token",
            "movie_info.imdb_api_key": "imdb-api-key",
            "movie_info.imdb_aws_access_key_id": "aws-access-key-id",
            "movie_info.imdb_aws_secret_access_key": "aws-secret-access-key",
            "movie_info.imdb_aws_session_token": "aws-session-token",
        }

        await self.manager.save(
            payload,
            expected_revision=1,
            updated_by=42,
            secret_changes={
                path: {"action": "replace", "value": value}
                for path, value in secrets.items()
            },
        )

        self.assertTrue(self.settings.movie_info_enabled)
        self.assertEqual(self.settings.movie_info_http_timeout_sec, 5.5)
        self.assertEqual(self.settings.movie_info_max_results, 8)
        self.assertEqual(self.settings.movie_info_default_language, "ja-JP")
        self.assertEqual(self.settings.movie_info_default_region, "JP")
        self.assertEqual(self.settings.movie_info_imdb_data_set_id, "dataset-1")
        self.assertEqual(self.settings.movie_info_imdb_revision_id, "revision-2")
        self.assertEqual(self.settings.movie_info_imdb_asset_id, "asset-3")
        self.assertEqual(
            self.settings.movie_info_tmdb_read_access_token,
            "tmdb-token",
        )
        self.assertEqual(self.settings.movie_info_imdb_api_key, "imdb-api-key")
        self.assertEqual(
            self.settings.movie_info_imdb_aws_access_key_id,
            "aws-access-key-id",
        )
        self.assertEqual(
            self.settings.movie_info_imdb_aws_secret_access_key,
            "aws-secret-access-key",
        )
        self.assertEqual(
            self.settings.movie_info_imdb_aws_session_token,
            "aws-session-token",
        )

        document = self.manager.api_document()
        self.assertTrue(set(secrets).issubset(document["configured_secrets"]))
        for path in (
            "tmdb_read_access_token",
            "imdb_api_key",
            "imdb_aws_access_key_id",
            "imdb_aws_secret_access_key",
            "imdb_aws_session_token",
        ):
            self.assertEqual(document["config"]["movie_info"][path], "")

        async with self.session_factory() as session:
            config_row = await session.get(RuntimeConfigRecord, 1)
            self.assertEqual(
                config_row.payload["movie_info"]["imdb_data_set_id"],
                "dataset-1",
            )
            self.assertEqual(config_row.payload["movie_info"]["imdb_api_key"], "")
            rows = (await session.execute(select(RuntimeConfigSecret))).scalars().all()
            encrypted = {row.name: row.ciphertext for row in rows}
        for path, value in secrets.items():
            self.assertIn(path, encrypted)
            self.assertNotIn(value, encrypted[path])

    async def test_movie_info_cannot_be_enabled_without_a_complete_provider(self) -> None:
        payload = self.manager.config.public_payload()
        payload["movie_info"]["enabled"] = True

        with self.assertRaisesRegex(ValueError, "需配置 TMDB Read Access Token"):
            await self.manager.save(
                payload,
                expected_revision=1,
                updated_by=42,
            )

        self.assertFalse(self.settings.movie_info_enabled)
        self.assertEqual(self.manager.revision, 1)

    async def test_movie_info_locale_is_normalized_and_validated(self) -> None:
        payload = self.manager.config.public_payload()
        payload["movie_info"]["default_language"] = "EN-us"
        payload["movie_info"]["default_region"] = "us"

        await self.manager.save(payload, expected_revision=1, updated_by=42)

        self.assertEqual(self.settings.movie_info_default_language, "en-US")
        self.assertEqual(self.settings.movie_info_default_region, "US")

        invalid = self.manager.config.public_payload()
        invalid["movie_info"]["default_region"] = "USA"
        with self.assertRaisesRegex(ValueError, "两字母代码"):
            await self.manager.save(
                invalid,
                expected_revision=2,
                updated_by=42,
            )

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

    async def test_hcaptcha_can_be_selected_and_keeps_turnstile_secrets(self) -> None:
        payload = self.manager.config.public_payload()
        payload["verification"].update(
            {
                "enabled": True,
                "provider": "hcaptcha",
                "hcaptcha_site_key": "h-site",
                "turnstile_site_key": "cf-site",
            }
        )
        await self.manager.save(
            payload,
            expected_revision=1,
            updated_by=42,
            secret_changes={
                "verification.hcaptcha_secret_key": {
                    "action": "replace",
                    "value": "h-secret",
                },
                "verification.turnstile_secret_key": {
                    "action": "replace",
                    "value": "cf-secret",
                },
            },
        )
        self.assertEqual(self.settings.join_verification_provider, "hcaptcha")
        self.assertEqual(self.settings.join_verification_hcaptcha_secret_key, "h-secret")
        self.assertEqual(self.manager.config.verification.turnstile_secret_key, "cf-secret")

    async def test_legacy_auto_delete_minutes_migrate_to_seconds(self) -> None:
        payload = self.manager.config.public_payload()
        payload["bot"].pop("auto_delete_seconds", None)
        payload["bot"]["auto_delete_minutes"] = 3
        await self.manager.save(payload, expected_revision=1, updated_by=42)
        self.assertEqual(self.settings.bot.auto_delete_seconds, 180)

    async def test_turnstile_secret_cannot_equal_site_key(self) -> None:
        payload = self.manager.config.public_payload()
        payload["verification"]["enabled"] = True
        payload["verification"]["turnstile_site_key"] = "same-key"

        with self.assertRaisesRegex(ValueError, "不能与 Site Key 相同"):
            await self.manager.save(
                payload,
                expected_revision=1,
                updated_by=42,
                secret_changes={
                    "verification.turnstile_secret_key": {
                        "action": "replace",
                        "value": "same-key",
                    }
                },
            )

        self.assertEqual(self.manager.revision, 1)

    async def test_existing_same_key_config_can_save_unrelated_changes(self) -> None:
        self.manager.config.verification.turnstile_site_key = "same-key"
        self.manager.config.verification.turnstile_secret_key = "same-key"
        payload = self.manager.config.public_payload()
        payload["bot"]["enable_typing"] = False

        await self.manager.save(
            payload,
            expected_revision=1,
            updated_by=42,
        )

        self.assertFalse(self.settings.bot.enable_typing)
        self.assertEqual(self.manager.revision, 2)

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
                "movie_info.tmdb_read_access_token": {
                    "action": "replace",
                    "value": "tmdb-read-token",
                }
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
        self.assertEqual(
            reloaded.config.movie_info.tmdb_read_access_token,
            "tmdb-read-token",
        )

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
