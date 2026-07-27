import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "bot" / "web" / "static" / "app.js"
_INDEX_HTML = _ROOT / "bot" / "web" / "static" / "index.html"
_STYLES_CSS = _ROOT / "bot" / "web" / "static" / "styles.css"


class SettingsFrontendRegressionTests(unittest.TestCase):
    def test_welcome_buttons_stay_typed_on_input_and_change(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        input_handler = source.split(
            'content.addEventListener("input"', 1
        )[1].split('content.addEventListener("change"', 1)[0]
        change_handler = source.split(
            'content.addEventListener("change"', 1
        )[1].split('content.addEventListener("focusout"', 1)[0]
        typed_handler = "if (handleGroupTemplateButtonsControl(target)) return;"

        self.assertIn(typed_handler, input_handler)
        self.assertIn(typed_handler, change_handler)

    def test_settings_script_has_a_webview_cache_buster(self) -> None:
        source = _INDEX_HTML.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r'<script src="/settings-assets/app\.js\?v=[^"]+" defer></script>',
        )

    def test_settings_stylesheet_has_a_webview_cache_buster(self) -> None:
        source = _INDEX_HTML.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r'<link rel="stylesheet" href="/settings-assets/styles\.css\?v=[^"]+">',
        )

    def test_settings_assets_share_the_unified_layout_cache_buster(self) -> None:
        source = _INDEX_HTML.read_text(encoding="utf-8")
        script_version = re.search(
            r'/settings-assets/app\.js\?v=([^"]+)', source
        )
        style_version = re.search(
            r'/settings-assets/styles\.css\?v=([^"]+)', source
        )

        self.assertIsNotNone(script_version)
        self.assertIsNotNone(style_version)
        self.assertEqual(script_version.group(1), style_version.group(1))
        self.assertIn("unified-save-layout", script_version.group(1))

    def test_global_policy_lists_support_independent_search(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        styles = _STYLES_CSS.read_text(encoding="utf-8")
        input_handler = source.split(
            'content.addEventListener("input"', 1
        )[1].split('content.addEventListener("change"', 1)[0]
        filter_helper = source.split(
            "function filteredGlobalAccessItems", 1
        )[1].split("function globalAccessRows", 1)[0]
        load_access = source.split("async function loadAccess", 1)[1].split(
            "function stripSecrets", 1
        )[0]

        self.assertIn('"global-bans": ""', source)
        self.assertIn('"global-exemptions": ""', source)
        self.assertIn(
            'globalAccessSearch("global-bans", "搜索用户 ID 或封禁原因"',
            source,
        )
        self.assertIn(
            'globalAccessSearch("global-exemptions", "搜索用户 ID"',
            source,
        )
        self.assertIn('${item.user_id} ${item.reason || ""} ${item.source || ""}', filter_helper)
        self.assertIn("String(item.user_id)", filter_helper)
        self.assertIn('target.matches("[data-access-search]")', input_handler)
        self.assertIn('state.listPages.set(`access:${type}`, 1)', input_handler)
        self.assertIn("refreshGlobalAccessList(type)", input_handler)
        self.assertIn('loadGlobalRegistry("global-bans", "global_bans")', load_access)
        self.assertIn(
            'loadGlobalRegistry("global-exemptions", "global_exemptions")',
            load_access,
        )
        self.assertIn("while (true)", load_access)
        self.assertNotIn("page < 100", load_access)
        self.assertIn(
            "if (state.accessLoadToken !== requestToken) return all;",
            load_access,
        )
        self.assertIn('"global-exemptions": "取消豁免"', source)
        self.assertIn("未找到匹配的全局封禁记录", source)
        self.assertIn("未找到匹配的全局豁免记录", source)
        self.assertIn(".access-list-toolbar", styles)

    def test_template_button_text_roundtrips_optional_style_as_fifth_column(
        self,
    ) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        normalize = source.split("function normalizeTemplateButtons", 1)[1].split(
            "function escapeTemplateButtonPart", 1
        )[0]
        serialize = source.split("function templateButtonsToText", 1)[1].split(
            "function parseTemplateButtonsText", 1
        )[0]
        parse = source.split("function parseTemplateButtonsText", 1)[1].split(
            "function templateButtonStyleLegend", 1
        )[0]

        self.assertIn("TEMPLATE_BUTTON_STYLES.includes(style)", normalize)
        self.assertIn("if (button.style) parts.push(button.style);", serialize)
        self.assertIn("if (parts.length > 5)", parse)
        self.assertIn("const style = (normalizedParts[4] || \"\").toLowerCase();", parse)
        self.assertIn("...(style ? { style } : {})", parse)

    def test_welcome_and_keyword_buttons_explain_supported_colors(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        styles = _STYLES_CSS.read_text(encoding="utf-8")

        self.assertIn("按钮名 | 操作 | 内容 | 行号 | 颜色（可选）", source)
        self.assertGreaterEqual(source.count("primary/success/danger（可选）"), 2)
        self.assertIn('aria-label="按钮颜色选项"', source)
        for style in ("primary", "success", "danger"):
            self.assertIn(f".template-button-style-swatch.{style}", styles)

    def test_role_cards_expose_total_deadline_with_builtin_defaults(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        self.assertIn(
            'field(`models.${roleName}.total_deadline_sec`, "总时限（秒）"',
            source,
        )
        self.assertIn("0 使用内置默认（${meta.deadlineDefault} 秒）", source)
        # Per-role hints must mirror _LLM_STAGE_DEADLINES in bot/services/llm.py.
        for role, deadline in (
            ("main", 120),
            ("vision", 90),
            ("decision", 35),
            ("moderation", 35),
            ("compress", 90),
            ("embed", 60),
        ):
            self.assertRegex(
                source,
                rf"{role}: \{{[^}}]*deadlineDefault: {deadline}[^}}]*\}}",
            )

    def test_group_settings_are_split_into_collapsible_categories(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        section_keys = (
            "reply-media",
            "model-api",
            "onboarding",
            "permissions",
            "safety",
            "management",
            "proactive-style",
            "automation",
            "rules-memory",
            "member-lists",
        )

        self.assertIn("data-group-settings-section", source)
        self.assertIn('data-action="toggle-group-card"', source)
        for key in section_keys:
            self.assertIn(f'key: "{key}"', source)

    def test_activity_pin_options_have_global_defaults_and_group_inheritance(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        normalize = source.split("function normalizeGroupSettings", 1)[1].split(
            "function normalizeDefaultPermissions", 1
        )[0]
        editable = source.split("const GROUP_EDITABLE_KEYS", 1)[1].split(
            "]);", 1
        )[0]

        controls = {
            "raid_guard_pin_message": (
                "raid_guard.pin_message",
                "爆破防护活动自动置顶",
            ),
            "call_admin_pin_message": (
                "call_admin.pin_message",
                "呼叫管理员通知自动置顶",
            ),
            "vote_ban_pin_message": (
                "vote_ban.pin_message",
                "民主投票活动自动置顶",
            ),
        }
        for key, (global_path, label) in controls.items():
            self.assertIn(f'toggle("{global_path}"', source)
            self.assertIn(f'"{key}"', editable)
            self.assertIn(
                f'{key}: settings.{key} == null ? null : Boolean(settings.{key})',
                normalize,
            )
            self.assertIn(
                f'data-group-key="{key}" data-kind="nullable-boolean"', source
            )
            self.assertIn(label, source)

        self.assertIn("管理员可标记已处理并取消置顶", source)
        self.assertIn("投票完成、管理员中止或直接封禁、票数不足超时后取消置顶", source)
        self.assertIn("自动或手动结束防护时取消置顶", source)

    def test_link_preview_switches_default_on_and_serialize_as_booleans(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        normalize = source.split("function normalizeGroupSettings", 1)[1].split(
            "function normalizeDefaultPermissions", 1
        )[0]
        apply_document = source.split("function applySettingsDocument", 1)[1].split(
            "function applyGroupsDocument", 1
        )[0]
        entry_fields = source.split("const ENTRY_FORM_FIELDS", 1)[1].split(
            "function readEntryFormValues", 1
        )[0]
        group_section_keys = source.split(
            "const GROUP_SECTION_SETTING_KEYS", 1
        )[1].split("const RESOURCE_TYPE_META", 1)[0]
        group_editable_keys = source.split("const GROUP_EDITABLE_KEYS", 1)[1].split(
            "]);", 1
        )[0]

        self.assertIn(
            'toggle("bot.disable_link_preview", "关闭 AI 回复链接预览"', source
        )
        self.assertIn("state.config.bot.disable_link_preview = true;", apply_document)
        self.assertIn(
            "welcome_disable_link_preview: settings.welcome_disable_link_preview !== false,",
            normalize,
        )
        self.assertIn(
            'groupToggle(group, "welcome_disable_link_preview", "关闭欢迎语链接预览"',
            source,
        )
        self.assertIn('"welcome_disable_link_preview"', group_section_keys)
        self.assertIn('"welcome_disable_link_preview"', group_editable_keys)
        self.assertEqual(
            source.count('name="disable_link_preview" type="checkbox"'), 4
        )
        self.assertEqual(
            source.count('name="disable_link_preview" type="checkbox" checked'), 2
        )
        self.assertEqual(source.count("item.disable_link_preview !== false"), 2)
        self.assertEqual(
            source.count(
                'disable_link_preview: Boolean(snapshotField(snapshot, "disable_link_preview", true))'
            ),
            2,
        )
        self.assertEqual(
            source.count(
                "disable_link_preview: values.disable_link_preview !== false,"
            ),
            2,
        )
        self.assertEqual(entry_fields.count('"disable_link_preview"'), 2)

    def test_group_disclosure_state_is_captured_before_rerender(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        render_content = source.split("function renderContent", 1)[1].split(
            "function render()", 1
        )[0]

        self.assertIn("groupCardOpen: new Map()", source)
        self.assertIn("groupSectionOpen: new Map()", source)
        self.assertIn("captureGroupDisclosureStates();", render_content)

    def test_invalid_group_control_reveals_its_collapsed_section(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        save_group = source.split("async function validateGroupForSave", 1)[1].split(
            "function snapshotField", 1
        )[0]

        self.assertIn("revealGroupControl(invalid);", save_group)
        self.assertLess(
            save_group.index("revealGroupControl(invalid);"),
            save_group.index("invalid.reportValidity();"),
        )

    def test_group_category_layout_has_mobile_disclosure_styles(self) -> None:
        source = _STYLES_CSS.read_text(encoding="utf-8")

        self.assertIn(".group-settings-section > summary", source)
        self.assertIn(".group-settings-section-body", source)
        self.assertIn(".group-card-toggle[aria-expanded=\"true\"]", source)

    def test_mobile_drawer_moves_focus_and_makes_background_inert(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        html = _INDEX_HTML.read_text(encoding="utf-8")
        drawer = source.split("function setMobileSidebarOpen", 1)[1].split(
            'saveButton.addEventListener("click"', 1
        )[0]
        interaction_lock = source.split(
            "function syncWorkspaceInteractionLock", 1
        )[1].split("function updateChrome", 1)[0]

        self.assertIn("drawerOpen", interaction_lock)
        self.assertIn("topbarActions.inert", interaction_lock)
        self.assertIn('sidebar.querySelector(".nav-button.active")', drawer)
        self.assertIn('event.key === "Tab"', drawer)
        self.assertIn("focusable[next].focus();", drawer)
        self.assertIn('id="sidebar-close"', html)
        self.assertIn('sidebarClose.addEventListener("click"', source)

    def test_narrow_mobile_layout_keeps_save_and_resource_actions_in_bounds(
        self,
    ) -> None:
        source = _STYLES_CSS.read_text(encoding="utf-8")

        self.assertIn("@media (max-width: 520px)", source)
        self.assertIn("#save-button {\n    min-width: 104px;", source)
        self.assertIn("@media (max-width: 420px)", source)
        self.assertIn(
            ".rule-resource-form > .mini-icon-button {\n    grid-column: 1 / -1;",
            source,
        )

    def test_resource_drafts_and_dynamic_group_changes_are_announced(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        for category in ("automation", "rules-memory"):
            self.assertIn(
                f'additionalDirty: groupResourceDraftDirty(group.id, "{category}")',
                source,
            )
        self.assertIn('dirtyLabel: "待保存"', source)
        self.assertIn('marker.setAttribute("role", "status");', source)

    def test_one_top_level_button_saves_config_groups_and_resource_drafts(
        self,
    ) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        html = _INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn('saveButton.addEventListener("click", saveAllChanges);', source)
        self.assertIn("const configNeedsSave", source)
        self.assertIn("const groupsToSave", source)
        self.assertIn("collectDeferredResourceOperations()", source)
        self.assertIn("saveButton.hidden = !state.session;", source)
        self.assertNotIn('data-action="save-group"', source)
        self.assertIn("保存全部", html)

    def test_configuration_resource_add_buttons_only_stage_local_drafts(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        submit_handler = source.split(
            'content.addEventListener("submit"', 1
        )[1].split("function switchTab", 1)[0]

        for resource_type in (
            "keyword-replies",
            "scheduled-messages",
            "rules",
            "memories",
        ):
            self.assertIn(
                f'data-resource-form="{resource_type}" data-save-scope="deferred"',
                source,
            )
        self.assertIn('if (form.dataset.saveScope === "deferred")', submit_handler)
        self.assertIn("stageDeferredResource(form);", submit_handler)
        self.assertIn("加入待保存列表", source)

    def test_configuration_resources_have_no_inline_save_buttons(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        self.assertNotIn('type="submit" aria-label="保存"', source)
        self.assertNotIn('aria-label="保存群规"', source)
        self.assertNotIn('aria-label="保存记忆"', source)
        self.assertIn("pendingResourceDeletes", source)
        self.assertIn('data-action="undo-delete-group-resource"', source)

    def test_resource_sections_are_named_and_directly_navigable(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        for label in (
            "关键词回复与定时消息",
            "群规与永久记忆",
            "成员记录与名单",
        ):
            self.assertIn(label, source)
        self.assertIn('class="group-quick-nav"', source)
        self.assertIn('data-action="jump-group-section"', source)

    def test_rerender_preserves_height_and_a_stable_scroll_anchor(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        render_content = source.split("function renderContent", 1)[1].split(
            "function render()", 1
        )[0]

        self.assertIn("captureContentScrollAnchor()", render_content)
        self.assertIn("content.style.minHeight", render_content)
        self.assertIn("window.scrollBy", render_content)

    def test_partial_save_only_clears_successful_resource_operations(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        save_all = source.split("async function saveAllChanges", 1)[1].split(
            "function providerReferences", 1
        )[0]
        apply_saved = source.split(
            "function applySavedResourceOperation", 1
        )[1].split("async function persistResourceOperation", 1)[0]

        self.assertIn("失败并已保留草稿", save_all)
        self.assertNotIn("clearResourceFormDrafts()", save_all)
        self.assertIn("state.pendingResourceDeletes.delete", apply_saved)
        self.assertIn("state.pendingResourceCreates.delete", apply_saved)

    def test_group_admin_save_path_never_requires_global_config_access(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        self.assertIn(
            "const configNeedsSave = state.session?.can_manage_global && configDirty();",
            source,
        )
        self.assertIn(
            'if (state.session?.can_manage_global) requests.unshift(apiFetch("/api/v1/settings"));',
            source,
        )

    def test_resource_save_merges_api_results_without_full_reload(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        persist = source.split(
            "async function persistResourceOperation", 1
        )[1].split("async function saveAllChanges", 1)[0]
        save_all = source.split("async function saveAllChanges", 1)[1].split(
            "function providerReferences", 1
        )[0]

        self.assertNotIn("loadGroupResources(", persist)
        self.assertNotIn("loadGroupResources(", save_all)
        self.assertIn("applySavedResourceOperation(operation, result);", persist)
        self.assertIn("Object.assign(item, document);", source)

    def test_immediate_actions_are_explicit_and_do_not_reload_all_resources(
        self,
    ) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        click_handler = source.split(
            'content.addEventListener("click"', 1
        )[1].split("const ENTRY_FORM_FIELDS", 1)[0]
        delete_handler = click_handler.split(
            'if (action === "delete-group-resource")', 1
        )[1].split('if (action === "undo-delete-group-resource")', 1)[0]
        submit_handler = source.split(
            'content.addEventListener("submit"', 1
        )[1].split("function switchTab", 1)[0]

        self.assertIn("immediate-resource-form", source)
        self.assertIn("确认即时操作", click_handler)
        self.assertIn("不会等待“保存全部”", submit_handler)
        self.assertIn("applyImmediateResourceDelete", click_handler)
        self.assertIn("applyImmediateResourceCreate", submit_handler)
        self.assertNotIn("loadGroupResources", delete_handler)
        self.assertNotIn("await loadGroupResources", submit_handler)
        self.assertIn("setRequestFormPending(form, true);", submit_handler)
        self.assertIn("form.dataset.requestPending", submit_handler)
        self.assertIn("beginImmediateMutation();", submit_handler)
        self.assertIn("endImmediateMutation();", submit_handler)

    def test_saving_makes_the_editing_surface_inert(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        interaction_lock = source.split(
            "function syncWorkspaceInteractionLock", 1
        )[1].split("function updateChrome", 1)[0]
        save_all = source.split("async function saveAllChanges", 1)[1].split(
            "function providerReferences", 1
        )[0]

        for busy_state in (
            "state.loading",
            "state.saving",
            "state.reloadingGroups",
            "state.immediateMutations > 0",
        ):
            self.assertIn(busy_state, interaction_lock)
        self.assertIn("content.inert = locked || drawerOpen;", interaction_lock)
        self.assertIn("desktopNav.inert = locked;", interaction_lock)
        self.assertIn("state.saving = true;", save_all)
        self.assertIn("state.saving = false;", save_all)
        self.assertIn("updateChrome();", save_all)

    def test_group_reload_locks_editing_until_the_request_finishes(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        reload_groups = source.split("async function reloadGroups", 1)[1].split(
            "async function loadCallAdminTargets", 1
        )[0]

        self.assertIn("state.reloadingGroups = true;", reload_groups)
        self.assertIn("state.reloadingGroups = false;", reload_groups)
        self.assertIn("updateChrome();", reload_groups)
        self.assertIn("finally", reload_groups)

    def test_resource_refresh_preserves_drafts_and_stale_gets_lose(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        loader = source.split("async function loadGroupResources", 1)[1].split(
            "async function loadAccess", 1
        )[0]
        click_handler = source.split(
            'if (action === "load-group-resources")', 1
        )[1].split('if (action === "load-access")', 1)[0]

        self.assertIn("const mutationEpoch", loader)
        self.assertIn("const staleAfterMutation", loader)
        self.assertIn("!staleAfterMutation", loader)
        self.assertIn("preservingDrafts", click_handler)
        self.assertNotIn("discardGroupResourceDrafts", click_handler)

    def test_resource_mutations_advance_the_stale_request_epoch(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        immediate = source.split("function applyImmediateResourceCreate", 1)[1].split(
            "function resourcePendingDelete", 1
        )[0]
        deferred = source.split("function applySavedResourceOperation", 1)[1].split(
            "async function persistResourceOperation", 1
        )[0]

        self.assertIn("bumpGroupResourceMutationEpoch(groupId);", immediate)
        self.assertIn("warning.is_banned = true;", immediate)
        self.assertGreaterEqual(
            deferred.count("bumpGroupResourceMutationEpoch(operation.groupId);"),
            3,
        )

    def test_permission_load_ignores_replaced_or_edited_group_state(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        loader = source.split("async function loadGroupPermissions", 1)[1].split(
            "async function loadGroupResources", 1
        )[0]

        self.assertIn('const requestToken = Symbol("group-permissions")', loader)
        self.assertIn("currentGroup !== group", loader)
        self.assertIn(
            "!sameValue(currentGroup.settings.default_permissions, initialValue)",
            loader,
        )
        self.assertIn("state.groupPermissionLoads.delete(key);", loader)
        self.assertIn("state.groupPermissionLoads.size > 0", source)

    def test_created_resource_is_upserted_after_the_temporary_row_is_removed(
        self,
    ) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        apply_saved = source.split(
            "function applySavedResourceOperation", 1
        )[1].split("async function persistResourceOperation", 1)[0]

        self.assertIn("const temporaryIndex", apply_saved)
        self.assertIn("list.splice(temporaryIndex, 1);", apply_saved)
        self.assertGreaterEqual(
            apply_saved.count(
                "upsertResourceRow(operation.groupId, operation.type, document);"
            ),
            2,
        )

    def test_lazy_validation_reveals_the_requested_group_section(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        reveal = source.split("function revealGroupSection", 1)[1].split(
            "function revealGroupControl", 1
        )[0]

        self.assertIn("state.groupCardOpen.set(groupKey, true);", reveal)
        self.assertIn("state.groupSectionOpen.set", reveal)
        self.assertIn("renderContent();", reveal)
        self.assertIn("body.hidden = false;", reveal)

    def test_revoking_a_group_is_blocked_while_that_group_has_drafts(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        delete_access = source.split('if (action === "delete-access")', 1)[1].split(
            'if (action === "delete-group-resource")', 1
        )[0]

        self.assertIn('if (type === "authorized-groups")', delete_access)
        self.assertIn("groupDirty(targetGroup)", delete_access)
        self.assertIn("groupResourceDraftDirty(id)", delete_access)
        self.assertIn("请先“保存全部”", delete_access)
        self.assertIn("state.groups.splice(groupIndex, 1);", delete_access)
        self.assertIn("state.groupBaselines.delete(groupKey);", delete_access)

    def test_discarding_resource_drafts_removes_temporary_rows(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        clear_drafts = source.split("function clearResourceFormDrafts", 1)[1].split(
            "function discardGroupResourceDrafts", 1
        )[0]
        discard_group = source.split(
            "function discardGroupResourceDrafts", 1
        )[1].split("function restoreGroupTemplateButtonValidity", 1)[0]

        for function_body in (clear_drafts, discard_group):
            self.assertIn("state.pendingResourceCreates", function_body)
            self.assertIn("list.splice(index, 1);", function_body)

    def test_invalid_welcome_button_draft_blocks_lazy_group_save(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        validator = source.split(
            "async function validateGroupForSave", 1
        )[1].split("function snapshotField", 1)[0]

        self.assertIn("const templateDraft", validator)
        self.assertIn("if (templateDraft?.error)", validator)
        self.assertIn('groupSectionStateKey(groupId, "onboarding")', validator)
        self.assertIn("return false;", validator)

    def test_movie_info_settings_and_secret_stripping_are_wired(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        integrations = source.split("function renderIntegrations", 1)[1].split(
            "function renderLogging", 1
        )[0]
        strip_secrets = source.split("function stripSecrets", 1)[1].split(
            "function validateConfig", 1
        )[0]

        for path in (
            "movie_info.enabled",
            "movie_info.http_timeout_sec",
            "movie_info.max_results",
            "movie_info.default_language",
            "movie_info.default_region",
            "movie_info.imdb_data_set_id",
            "movie_info.imdb_revision_id",
            "movie_info.imdb_asset_id",
        ):
            self.assertIn(f'"{path}"', integrations)

        for path in (
            "movie_info.tmdb_read_access_token",
            "movie_info.imdb_api_key",
            "movie_info.imdb_aws_access_key_id",
            "movie_info.imdb_aws_secret_access_key",
            "movie_info.imdb_aws_session_token",
        ):
            self.assertIn(f'secretField("{path}"', integrations)
            self.assertIn(f'payload.{path} = "";', strip_secrets)

    def test_model_api_settings_are_group_scoped_and_sub2api_ui_is_removed(
        self,
    ) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        integrations = source.split("function renderIntegrations", 1)[1].split(
            "function renderLogging", 1
        )[0]
        strip_secrets = source.split("function stripSecrets", 1)[1].split(
            "function validateConfig", 1
        )[0]

        self.assertIn('{ key: "model-api", label: "模型 API"', source)
        self.assertIn('key: "model-api"', source)
        self.assertIn("兼容 OpenAI Chat Completions API", source)
        self.assertIn("或包含 <code>/v1</code> 均可", source)
        self.assertNotIn("Sub2API", integrations)
        self.assertNotIn("影片信息与 Sub2API 接入", source)
        self.assertIn('payload.sub2api.api_key = "";', strip_secrets)

    def test_group_model_api_secret_draft_is_private_and_part_of_save_state(
        self,
    ) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        group_dirty = source.split("function groupDirty", 1)[1].split(
            "function anyGroupDirty", 1
        )[0]
        group_section_dirty = source.split(
            "function groupSectionDirty", 1
        )[1].split("function updateRenderedGroupSectionDirty", 1)[0]
        persist_group = source.split(
            "async function persistGroupChanges", 1
        )[1].split("function applySavedResourceOperation", 1)[0]

        self.assertIn("groupApiModelQuerySecretChanges: new Map()", source)
        self.assertIn("state.groupApiModelQuerySecretChanges.has", group_dirty)
        self.assertIn("state.groupApiModelQuerySecretChanges.has", group_section_dirty)
        self.assertIn("api_model_query_secret_change", persist_group)
        self.assertIn("state.groupApiModelQuerySecretChanges.delete", persist_group)
        self.assertIn(
            'type="password" data-group-api-model-query-secret-input', source
        )
        self.assertIn('data-group-id="${attr(group.id)}" value=""', source)
        self.assertIn("留空会保留已保存值", source)
        self.assertIn('action: "clear"', source)

    def test_group_model_api_enablement_is_validated_against_effective_secret(
        self,
    ) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        validator = source.split(
            "async function validateGroupForSave", 1
        )[1].split("function snapshotField", 1)[0]

        self.assertIn("api_model_query_enabled", validator)
        self.assertIn('apiModelQuerySecretChange?.action === "clear"', validator)
        self.assertIn("api_model_query_base_url", validator)
        self.assertIn("groupApiModelQuerySecretConfigured(group)", validator)
        self.assertGreaterEqual(validator.count('revealGroupSection(groupId, "model-api")'), 3)

    def test_group_model_api_secret_drafts_are_discarded_on_reload(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        reload_groups = source.split(
            'if (action === "reload-groups")', 1
        )[1].split("});", 1)[0]
        reload_all = source.split(
            'reloadButton.addEventListener("click"', 1
        )[1].split('window.addEventListener("beforeunload"', 1)[0]

        self.assertIn("state.groupApiModelQuerySecretChanges.clear();", reload_groups)
        self.assertIn("state.groupApiModelQuerySecretChanges.clear();", reload_all)


if __name__ == "__main__":
    unittest.main()
