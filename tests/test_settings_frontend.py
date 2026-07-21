import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "bot" / "web" / "static" / "app.js"
_INDEX_HTML = _ROOT / "bot" / "web" / "static" / "index.html"


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


if __name__ == "__main__":
    unittest.main()
