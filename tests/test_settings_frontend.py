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


if __name__ == "__main__":
    unittest.main()
