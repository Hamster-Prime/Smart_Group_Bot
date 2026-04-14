import unittest

from bot.services.skills.webfetch import WebFetchSkill


class WebFetchSkillExtractionTests(unittest.TestCase):
    def test_html_to_text_strips_script_content(self) -> None:
        raw_html = """
        <html>
          <head>
            <title>Top GitHub Repositories in 2026</title>
          </head>
          <body>
            <script>
              function loadThirdPartyScripts() {
                var src = "https://www.googletagmanager.com/gtm.js";
              }
            </script>
            <main>
              Discover the top GitHub repositories to watch this year.
            </main>
          </body>
        </html>
        """

        title, content = WebFetchSkill._html_to_text(raw_html)

        self.assertEqual(title, "Top GitHub Repositories in 2026")
        self.assertIn("Discover the top GitHub repositories", content)
        self.assertNotIn("loadThirdPartyScripts", content)
        self.assertNotIn("googletagmanager", content)

    def test_html_to_text_prefers_meta_description(self) -> None:
        raw_html = """
        <html>
          <head>
            <title>Example Article</title>
            <meta name="description" content="A concise summary from metadata.">
          </head>
          <body>
            <main>Long body that should not replace a cleaner description.</main>
          </body>
        </html>
        """

        _, content = WebFetchSkill._html_to_text(raw_html)

        self.assertEqual(content, "A concise summary from metadata.")
