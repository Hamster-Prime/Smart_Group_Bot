import unittest

from bot.utils.security import clean_multiline_text


class SecurityUtilsTests(unittest.TestCase):
    def test_clean_multiline_text_preserves_structure(self) -> None:
        source = "### 今日新闻\n1. 第一条\n2. 第二条\n\n#### 科技\n更多内容"

        cleaned = clean_multiline_text(source, max_len=400)

        self.assertIn("### 今日新闻\n1. 第一条\n2. 第二条", cleaned)
        self.assertIn("\n\n#### 科技\n", cleaned)


if __name__ == "__main__":
    unittest.main()
