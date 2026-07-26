import json
import unittest
from unittest.mock import AsyncMock, patch

from bot.services.skills import routeros_doc as routeros_doc_module
from bot.services.skills.base import SkillContext
from bot.services.skills.routeros_doc import (
    RouterOSDocSkill,
    _html_to_markdown,
    _normalize_location,
    _title_from_markdown,
)


_SITEMAP_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://manual.mikrotik.com/docs/virtual-private-networks/wireguard</loc></url>
  <url><loc>https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/peers/</loc></url>
  <url><loc>https://manual.mikrotik.com/docs/cli-reference/interface/wireguard</loc></url>
  <url><loc>https://manual.mikrotik.com/docs/cli-reference/ip/firewall/filter</loc></url>
  <url><loc>https://manual.mikrotik.com/docs/cli-reference/ip/firewall/nat</loc></url>
  <url><loc>https://manual.mikrotik.com/changelog/changelog-2026-07-18</loc></url>
  <url><loc>https://manual.mikrotik.com/changelog/changelog-2026-06-27</loc></url>
  <url><loc>https://manual.mikrotik.com/blog/routeros-7</loc></url>
  <url><loc>https://evil.example/docs/virtual-private-networks/wireguard</loc></url>
</urlset>
"""

_INDEX_DOCS = [
    {
        "url": "/docs/virtual-private-networks/wireguard",
        "type": 0,
        "title": "<strong>WireGuard</strong>",
        "sectionRef": "",
        "content": "WireGuard is a modern VPN interface; allowed-address is documented below.",
    },
    {
        "url": "/docs/virtual-private-networks/wireguard",
        "type": 1,
        "title": "Peers",
        "sectionRef": "peers",
        "content": "Configure peer allowed-address and public-key values.",
    },
    {
        "url": "/docs/virtual-private-networks/wireguard#peer-properties",
        "type": 1,
        "title": "Peer properties",
        "sectionRef": "ignored-because-url-has-anchor",
        "content": "The allowed-address property controls routed prefixes.",
    },
    {
        "url": "/docs/cli-reference/interface/wireguard",
        "type": 0,
        "title": "interface/wireguard",
        "sectionRef": "",
        "content": "RouterOS CLI command reference.",
    },
    {
        "url": "https://evil.example/docs/virtual-private-networks/wireguard",
        "type": 0,
        "title": "Forged WireGuard page",
        "sectionRef": "",
        "content": "allowed-address",
    },
    {
        "url": "/docs/tags/wireguard",
        "type": 0,
        "title": "Tag page",
        "sectionRef": "",
        "content": "allowed-address",
    },
]


def _index_json() -> str:
    return json.dumps({"searchDocs": _INDEX_DOCS}, ensure_ascii=False)


class RouterOSDocHtmlTests(unittest.TestCase):
    def test_title_extraction_removes_self_link_markdown(self) -> None:
        self.assertEqual(
            _title_from_markdown(
                "# [WireGuard](https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/#wireguard)\n\nBody",
                "docs/virtual-private-networks/wireguard",
            ),
            "WireGuard",
        )

    def test_html_to_markdown_preserves_docusaurus_structure(self) -> None:
        raw_html = """
        <html><head><title>outside</title></head><body><p>outside article</p>
        <article>
          <div class="theme-doc-markdown markdown">
            <h1>WireGuard<a class="hash-link" href="#wireguard">\u200b</a></h1>
            <p>Set <strong>listen-port</strong> with <code>listen-port=13231</code>.</p>
            <div class="theme-admonition theme-admonition-warning alert alert--danger">
              <div class="admonitionHeading_node_modules">Warning heading</div>
              <div><p>Keep the private key secret.</p></div>
            </div>
            <ul><li>Peer<ul><li>Allowed address</li></ul></li></ul>
            <p><a href="../ipsec/">IPsec page</a></p>
            <pre><code class="language-routeros">/interface/wireguard/add\n  listen-port=13231\n</code></pre>
            <table>
              <tr><th>Property</th><th>Value</th></tr>
              <tr><td>allowed-address</td><td>10.0.0.0/24|fd00::/64</td></tr>
            </table>
            <script>do_not_include()</script>
          </div>
          <nav>pagination must be skipped</nav>
        </article>
        </body></html>
        """

        markdown = _html_to_markdown(
            raw_html,
            base_url="https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/",
        )

        self.assertIn("# WireGuard", markdown)
        self.assertNotIn("\u200b", markdown)
        self.assertIn("Set **listen-port** with `listen-port=13231`.", markdown)
        self.assertIn("> **WARNING:**", markdown)
        self.assertIn("Keep the private key secret.", markdown)
        self.assertIn("- Peer\n  - Allowed address", markdown)
        self.assertIn(
            "[IPsec page](https://manual.mikrotik.com/docs/virtual-private-networks/ipsec/)",
            markdown,
        )
        self.assertIn("```routeros\n/interface/wireguard/add\n  listen-port=13231", markdown)
        self.assertIn("| Property | Value |", markdown)
        self.assertIn(r"| allowed-address | 10.0.0.0/24\|fd00::/64 |", markdown)
        self.assertNotIn("outside article", markdown)
        self.assertNotIn("do_not_include", markdown)
        self.assertNotIn("pagination must be skipped", markdown)

    def test_html_to_markdown_keeps_title_inside_docusaurus_header(self) -> None:
        markdown = _html_to_markdown(
            """
            <article>
              <div class="theme-doc-markdown markdown">
                <header><h1>First Time Configuration<a class="hash-link">\u200b</a></h1></header>
                <p>Configure an IP address.</p>
              </div>
              <nav>pagination</nav>
            </article>
            """,
            base_url="https://manual.mikrotik.com/docs/getting-started/first-time-configuration/",
        )

        self.assertIn("# First Time Configuration", markdown)
        self.assertIn("Configure an IP address.", markdown)
        self.assertNotIn("pagination", markdown)

    def test_location_normalization_accepts_only_official_safe_paths(self) -> None:
        self.assertEqual(
            _normalize_location(
                "https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/#peers"
            ),
            "docs/virtual-private-networks/wireguard",
        )
        for value in (
            "https://evil.example/docs/wireguard",
            "https://manual.mikrotik.com.evil.example/docs/wireguard",
            "http://manual.mikrotik.com/docs/wireguard",
            "https://user@manual.mikrotik.com/docs/wireguard",
            "https://manual.mikrotik.com:444/docs/wireguard",
            "../docs/wireguard",
            "docs/%2e%2e/wireguard",
            "docs%2f..%2fwireguard",
            "docs/wireguard?next=https://evil.example",
            "docs\\wireguard",
            "docs/wireguard\nX-Injected: true",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _normalize_location(value)


class RouterOSDocNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        routeros_doc_module._CACHE.clear()
        self.skill = RouterOSDocSkill()
        self.context = SkillContext()

    async def test_search_groups_section_ref_hits_and_filters_untrusted_urls(self) -> None:
        fetch_mock = AsyncMock(
            return_value=(
                200,
                _index_json(),
                "https://manual.mikrotik.com/search-doc.json",
                "application/json",
            )
        )

        with patch.object(routeros_doc_module, "fetch_text", new=fetch_mock):
            result = await self.skill.run(
                {
                    "action": "search",
                    "query": "allowed-address",
                    "max_results": 8,
                },
                self.context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "找到 1 条 RouterOS 官方文档结果")
        self.assertEqual(result.payload["source_url"], "https://manual.mikrotik.com/search-doc.json")
        hit = result.payload["results"][0]
        self.assertEqual(hit["title"], "WireGuard")
        self.assertEqual(hit["location"], "docs/virtual-private-networks/wireguard")
        self.assertEqual(
            hit["url"],
            "https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/",
        )
        self.assertEqual(
            {section["anchor"] for section in hit["sections"]},
            {"#peers", "#peer-properties"},
        )
        self.assertNotIn("evil.example", json.dumps(result.payload))
        fetch_mock.assert_awaited_once()

    async def test_page_returns_live_markdown_with_source_and_truncation_flag(self) -> None:
        page_html = """
        <article><div class="theme-doc-markdown markdown">
          <h1>WireGuard</h1>
          <pre><code class="language-routeros">/interface/wireguard/add disabled=no</code></pre>
        </div></article>
        """
        fetch_mock = AsyncMock(
            return_value=(
                200,
                page_html,
                "https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/",
                "text/html; charset=utf-8",
            )
        )

        with patch.object(routeros_doc_module, "fetch_text", new=fetch_mock):
            result = await self.skill.run(
                {
                    "action": "page",
                    "location": "docs/virtual-private-networks/wireguard",
                },
                self.context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["title"], "WireGuard")
        self.assertIn("/interface/wireguard/add disabled=no", result.payload["content"])
        self.assertEqual(
            result.payload["source_url"],
            "https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/",
        )
        self.assertIn("fetched_at", result.payload)
        self.assertFalse(result.payload["truncated"])

    async def test_section_returns_partial_pages_errors_and_continuation(self) -> None:
        locations = [
            "docs/virtual-private-networks/wireguard",
            "docs/virtual-private-networks/wireguard/peers",
            "docs/virtual-private-networks/wireguard/troubleshooting",
            "docs/virtual-private-networks/wireguard/examples",
        ]

        async def fetch_page(location: str):
            if location.endswith("troubleshooting"):
                raise RuntimeError("temporary upstream failure")
            title = location.rsplit("/", 1)[-1]
            return f"# {title}\n\nbody", f"https://manual.mikrotik.com/{location}/", "text/html"

        with (
            patch.object(self.skill, "_sitemap", new=AsyncMock(return_value=locations)),
            patch.object(self.skill, "_fetch_page", new=AsyncMock(side_effect=fetch_page)),
        ):
            result = await self.skill.run(
                {
                    "action": "section",
                    "prefix": "docs/virtual-private-networks/wireguard",
                    "max_pages": 3,
                    "max_chars": 5000,
                },
                self.context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["total_pages"], 4)
        self.assertEqual(result.payload["returned_pages"], 2)
        self.assertEqual(result.payload["errors"][0]["location"], locations[2])
        self.assertEqual(result.payload["retry_locations"], [locations[2]])
        self.assertTrue(result.payload["has_more"])
        self.assertEqual(result.payload["next_offset"], 3)
        self.assertEqual(result.payload["next_locations"], [locations[3]])
        self.assertTrue(result.payload["truncated"])
        self.assertTrue(all(page["source_url"].startswith("https://manual.mikrotik.com/") for page in result.payload["pages"]))

    async def test_section_all_failures_return_exact_retry_locations(self) -> None:
        locations = [
            "docs/routing/bgp",
            "docs/routing/bgp/troubleshooting",
            "docs/routing/bgp/examples",
        ]

        with (
            patch.object(self.skill, "_sitemap", new=AsyncMock(return_value=locations)),
            patch.object(
                self.skill,
                "_fetch_page",
                new=AsyncMock(side_effect=RuntimeError("temporary upstream failure")),
            ),
        ):
            result = await self.skill.run(
                {
                    "action": "section",
                    "prefix": "docs/routing/bgp",
                    "max_pages": 2,
                    "max_chars": 5000,
                },
                self.context,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "section_fetch_failed")
        self.assertEqual(result.payload["retry_locations"], locations[:2])
        self.assertIn("action=page", result.summary)
        self.assertTrue(result.payload["truncated"])
        self.assertTrue(result.payload["has_more"])
        self.assertEqual(result.payload["next_offset"], 2)
        self.assertEqual(result.payload["next_locations"], [locations[2]])

    async def test_toc_uses_sitemap_and_titles_with_pagination(self) -> None:
        async def response_for(url: str, **kwargs):
            del kwargs
            if url.endswith("sitemap.xml"):
                return 200, _SITEMAP_XML, url, "application/xml"
            if url.endswith("search-doc.json"):
                return 200, _index_json(), url, "application/json"
            raise AssertionError(f"unexpected URL: {url}")

        fetch_mock = AsyncMock(side_effect=response_for)
        with patch.object(routeros_doc_module, "fetch_text", new=fetch_mock):
            result = await self.skill.run(
                {
                    "action": "toc",
                    "filter": "wireguard",
                    "max_results": 2,
                },
                self.context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["total_matches"], 3)
        self.assertEqual(len(result.payload["results"]), 2)
        self.assertEqual(result.payload["results"][0]["title"], "interface/wireguard")
        self.assertTrue(result.payload["has_more"])
        self.assertNotIn("blog/routeros-7", json.dumps(result.payload["results"]))
        self.assertNotIn("changelog-2026", json.dumps(result.payload["results"]))
        self.assertEqual(fetch_mock.await_count, 2)

    async def test_cli_lists_pages_and_reads_exact_command_reference(self) -> None:
        locations = routeros_doc_module._parse_sitemap(_SITEMAP_XML)
        with patch.object(self.skill, "_sitemap", new=AsyncMock(return_value=locations)):
            listing = await self.skill.run(
                {
                    "action": "cli",
                    "filter": "firewall",
                    "max_results": 1,
                },
                self.context,
            )

        self.assertTrue(listing.ok)
        self.assertEqual(listing.payload["mode"], "list")
        self.assertEqual(listing.payload["total_matches"], 2)
        self.assertEqual(listing.payload["results"][0]["path"], "/ip/firewall/filter")
        self.assertTrue(listing.payload["has_more"])

        with (
            patch.object(self.skill, "_sitemap", new=AsyncMock(return_value=locations)),
            patch.object(
                self.skill,
                "_fetch_page",
                new=AsyncMock(
                    return_value=(
                        "# /ip/firewall/nat\n\n`add action=masquerade`",
                        "https://manual.mikrotik.com/docs/cli-reference/ip/firewall/nat/",
                        "text/html",
                    )
                ),
            ),
        ):
            page = await self.skill.run(
                {"action": "cli", "path": "ip/firewall/nat"},
                self.context,
            )

        self.assertTrue(page.ok)
        self.assertEqual(page.payload["mode"], "page")
        self.assertEqual(page.payload["path"], "ip/firewall/nat")
        self.assertIn("action=masquerade", page.payload["content"])
        self.assertEqual(
            page.payload["source_url"],
            "https://manual.mikrotik.com/docs/cli-reference/ip/firewall/nat/",
        )

    async def test_cli_not_found_returns_close_matches_without_fetching_page(self) -> None:
        locations = routeros_doc_module._parse_sitemap(_SITEMAP_XML)
        page_mock = AsyncMock()
        with (
            patch.object(self.skill, "_sitemap", new=AsyncMock(return_value=locations)),
            patch.object(self.skill, "_fetch_page", new=page_mock),
        ):
            result = await self.skill.run(
                {"action": "cli", "path": "bridge/nat"},
                self.context,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "cli_not_found")
        self.assertEqual(result.payload["close_matches"][0]["path"], "/ip/firewall/nat")
        page_mock.assert_not_awaited()

    async def test_changelog_is_newest_first_and_paginated(self) -> None:
        locations = routeros_doc_module._parse_sitemap(_SITEMAP_XML)
        with patch.object(self.skill, "_sitemap", new=AsyncMock(return_value=locations)):
            result = await self.skill.run(
                {"action": "changelog", "limit": 1},
                self.context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "列出 1 条 RouterOS 更新记录")
        self.assertEqual(result.payload["total_matches"], 2)
        self.assertEqual(result.payload["results"][0]["date"], "2026-07-18")
        self.assertTrue(result.payload["has_more"])
        self.assertEqual(result.payload["next_offset"], 1)

    async def test_page_uses_fixed_safe_network_parameters(self) -> None:
        fetch_mock = AsyncMock(
            return_value=(
                200,
                '<article><div class="theme-doc-markdown"><h1>WireGuard</h1></div></article>',
                "https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/",
                "text/html",
            )
        )

        with patch.object(routeros_doc_module, "fetch_text", new=fetch_mock):
            result = await self.skill.run(
                {
                    "action": "page",
                    "location": "docs/virtual-private-networks/wireguard",
                    "timeout_sec": 999,
                    "allow_redirects": True,
                    "allowed_hosts": ["evil.example"],
                },
                self.context,
            )

        self.assertTrue(result.ok)
        args, kwargs = fetch_mock.await_args
        self.assertEqual(
            args,
            ("https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/",),
        )
        self.assertEqual(
            kwargs["headers"],
            {
                "User-Agent": "SmartGroupBot/1.0 routeros-doc",
                "Accept": "text/html,application/json,application/xml,text/xml,text/plain;q=0.9",
            },
        )
        self.assertEqual(kwargs["timeout_sec"], 18.0)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["allowed_hosts"], ("manual.mikrotik.com",))
        self.assertEqual(
            kwargs["allowed_content_types"],
            ("text/html", "application/xhtml+xml", "text/plain"),
        )
        self.assertEqual(kwargs["max_response_bytes"], 1024 * 1024)
        self.assertEqual(kwargs["max_decoded_bytes"], 2 * 1024 * 1024)
        self.assertEqual(kwargs["max_redirects"], 3)

    async def test_untrusted_locations_are_rejected_without_network_access(self) -> None:
        fetch_mock = AsyncMock()
        with patch.object(routeros_doc_module, "fetch_text", new=fetch_mock):
            for location in (
                "https://evil.example/docs/wireguard",
                "https://manual.mikrotik.com.evil.example/docs/wireguard",
                "http://manual.mikrotik.com/docs/wireguard",
                "https://user@manual.mikrotik.com/docs/wireguard",
                "https://manual.mikrotik.com:444/docs/wireguard",
                "../docs/wireguard",
                "docs/%2e%2e/wireguard",
                "docs/wireguard?next=https://evil.example",
                "docs\\wireguard",
            ):
                with self.subTest(location=location):
                    result = await self.skill.run(
                        {"action": "page", "location": location},
                        self.context,
                    )
                    self.assertFalse(result.ok)
                    self.assertEqual(result.summary, "RouterOS 文档路径无效")
        fetch_mock.assert_not_awaited()

    async def test_final_response_url_is_revalidated_after_fetch(self) -> None:
        fetch_mock = AsyncMock(
            return_value=(
                200,
                '<article><div class="theme-doc-markdown"><h1>WireGuard</h1></div></article>',
                "https://evil.example/docs/wireguard/",
                "text/html",
            )
        )

        with patch.object(routeros_doc_module, "fetch_text", new=fetch_mock):
            result = await self.skill.run(
                {"action": "page", "location": "docs/wireguard"},
                self.context,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.summary, "RouterOS 文档地址安全校验失败")
        self.assertEqual(result.error, "unexpected_final_host")


class RouterOSDocInterfaceTests(unittest.TestCase):
    def test_skill_exposes_registration_interface_and_all_actions(self) -> None:
        skill = RouterOSDocSkill()

        self.assertEqual(skill.name, "routeros_doc")
        self.assertIn("RouterOS", skill.description)
        self.assertEqual(
            skill.parameters_schema["properties"]["action"]["enum"],
            ["search", "page", "section", "toc", "cli", "changelog"],
        )
        self.assertTrue(callable(skill.run))
        self.assertEqual(skill.execution_timeout_seconds({"action": "section"}), 90.0)


if __name__ == "__main__":
    unittest.main()
