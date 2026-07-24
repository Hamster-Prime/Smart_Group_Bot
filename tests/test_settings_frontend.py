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

    def test_button_style_assets_share_a_new_cache_buster(self) -> None:
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
        self.assertIn("button-styles", script_version.group(1))

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

    def test_group_settings_are_split_into_collapsible_categories(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        section_keys = (
            "reply-media",
            "onboarding",
            "permissions",
            "safety",
            "management",
            "proactive-style",
            "resources",
        )

        self.assertIn("data-group-settings-section", source)
        self.assertIn('data-action="toggle-group-card"', source)
        for key in section_keys:
            self.assertIn(f'key: "{key}"', source)

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
        save_group = source.split("async function saveGroup", 1)[1].split(
            "function providerReferences", 1
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

    def test_resource_drafts_and_dynamic_group_changes_are_announced(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        self.assertIn("additionalDirty: groupResourceDraftDirty(group.id)", source)
        self.assertIn('dirtyLabel: "有草稿"', source)
        self.assertIn('marker.setAttribute("role", "status");', source)

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


if __name__ == "__main__":
    unittest.main()
