import asyncio
import gzip
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from bot.services.skills import platform_common
from bot.services.skills.platform_common import (
    ResponseTooLargeError,
    UnsafeUrlError,
    _HopBytesResponse,
    _HopResponse,
    _PinnedResolver,
    _ResolvedTarget,
    _content_type_allowed,
    _decompress_limited,
    _read_limited_raw_body,
    fetch_bytes,
    fetch_text,
    request_json,
    resolve_public_http_url,
    run_search_thread,
)


class _ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class PlatformCommonSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_private_link_local_and_metadata_ip_literals(self) -> None:
        for url in (
            "http://127.0.0.1/admin",
            "http://10.1.2.3/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://[fe80::1]/",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeUrlError):
                await resolve_public_http_url(url)

    async def test_rejects_private_ipv4_hidden_in_ipv6_transition_addresses(self) -> None:
        for url in (
            "http://[64:ff9b::7f00:1]/",  # NAT64 -> 127.0.0.1
            "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 -> metadata IP
            "http://[64:ff9b:1::7f00:1]/",  # local-use NAT64 prefix
            "http://[::ffff:127.0.0.1]/",  # IPv4-mapped
            "http://[2002:7f00:1::]/",  # 6to4 -> 127.0.0.1
            # Teredo server 8.8.8.8, decoded client 127.0.0.1.
            "http://[2001:0:808:808:0:0:80ff:fffe]/",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeUrlError):
                await resolve_public_http_url(url)

    async def test_rejects_dns_answer_if_any_address_is_non_public(self) -> None:
        with patch.object(
            platform_common,
            "_resolve_host_sync",
            return_value=("93.184.216.34", "127.0.0.1"),
        ):
            with self.assertRaisesRegex(UnsafeUrlError, "non_public_address"):
                await resolve_public_http_url("https://example.com/")

    async def test_pinned_resolver_uses_only_prevalidated_addresses(self) -> None:
        target = _ResolvedTarget(
            host="example.com",
            port=443,
            addresses=("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        )
        rows = await _PinnedResolver(target).resolve(
            "example.com", 443, socket.AF_UNSPEC
        )

        self.assertEqual({row["host"] for row in rows}, set(target.addresses))
        self.assertNotIn("127.0.0.1", {row["host"] for row in rows})

    async def test_redirect_destination_is_revalidated_before_second_request(self) -> None:
        public = _ResolvedTarget("example.com", 443, ("93.184.216.34",))
        resolve = AsyncMock(side_effect=[public, UnsafeUrlError("non_public_address")])
        first_hop = _HopResponse(
            status=302,
            text="",
            url="https://example.com/start",
            content_type="text/html",
            location="http://169.254.169.254/latest/meta-data/",
        )
        request = AsyncMock(return_value=first_hop)

        with (
            patch.object(platform_common, "resolve_public_http_url", new=resolve),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            with self.assertRaisesRegex(UnsafeUrlError, "non_public_address"):
                await fetch_text("https://example.com/start")

        self.assertEqual(resolve.await_count, 2)
        request.assert_awaited_once()

    async def test_redirect_does_not_repeat_first_hop_query_params(self) -> None:
        first = _ResolvedTarget("api.example.com", 443, ("93.184.216.34",))
        second = _ResolvedTarget("other.example", 443, ("93.184.216.35",))
        request = AsyncMock(
            side_effect=[
                _HopResponse(
                    status=302,
                    text="",
                    url="https://api.example.com/start?api_key=secret",
                    content_type="text/html",
                    location="https://other.example/result",
                ),
                _HopResponse(
                    status=200,
                    text="ok",
                    url="https://other.example/result",
                    content_type="text/plain",
                ),
            ]
        )
        with (
            patch.object(
                platform_common,
                "resolve_public_http_url",
                new=AsyncMock(side_effect=[first, second]),
            ),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            await fetch_text(
                "https://api.example.com/start",
                params={"api_key": "secret"},
            )

        self.assertEqual(request.await_args_list[0].kwargs["params"], {"api_key": "secret"})
        self.assertIsNone(request.await_args_list[1].kwargs["params"])

    async def test_www_to_apex_redirect_strips_sensitive_headers(self) -> None:
        first = _ResolvedTarget("www.example.com", 443, ("93.184.216.34",))
        second = _ResolvedTarget("example.com", 443, ("93.184.216.35",))
        request = AsyncMock(
            side_effect=[
                _HopResponse(
                    status=302,
                    text="",
                    url="https://www.example.com/start",
                    content_type="text/html",
                    location="https://example.com/result",
                ),
                _HopResponse(
                    status=200,
                    text="ok",
                    url="https://example.com/result",
                    content_type="text/plain",
                ),
            ]
        )
        with (
            patch.object(
                platform_common,
                "resolve_public_http_url",
                new=AsyncMock(side_effect=[first, second]),
            ),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            await fetch_text(
                "https://www.example.com/start",
                headers={
                    "Authorization": "Bearer secret",
                    "Cookie": "session=secret",
                    "X-API-Key": "api-secret",
                    "X-Test": "kept",
                },
            )

        second_headers = request.await_args_list[1].kwargs["headers"]
        self.assertNotIn("Authorization", second_headers)
        self.assertNotIn("Cookie", second_headers)
        self.assertNotIn("X-API-Key", second_headers)
        self.assertEqual(second_headers["X-Test"], "kept")

    async def test_binary_redirect_destination_is_revalidated(self) -> None:
        public = _ResolvedTarget("images.example.com", 443, ("93.184.216.34",))
        resolve = AsyncMock(side_effect=[public, UnsafeUrlError("non_public_address")])
        first_hop = _HopBytesResponse(
            status=302,
            body=b"",
            url="https://images.example.com/cover.jpg",
            content_type="text/html",
            location="http://127.0.0.1/secret",
        )
        request = AsyncMock(return_value=first_hop)

        with (
            patch.object(platform_common, "resolve_public_http_url", new=resolve),
            patch.object(platform_common, "_fetch_bytes_one_hop", new=request),
        ):
            with self.assertRaisesRegex(UnsafeUrlError, "non_public_address"):
                await fetch_bytes(
                    "https://images.example.com/cover.jpg",
                    allowed_content_types=("image/",),
                )

        self.assertEqual(resolve.await_count, 2)
        request.assert_awaited_once()

    async def test_caller_cannot_override_host_or_framing_headers(self) -> None:
        public = _ResolvedTarget("example.com", 443, ("93.184.216.34",))
        request = AsyncMock(
            return_value=_HopResponse(
                status=200,
                text="ok",
                url="https://example.com/",
                content_type="text/plain",
            )
        )
        with (
            patch.object(
                platform_common,
                "resolve_public_http_url",
                new=AsyncMock(return_value=public),
            ),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            await fetch_text(
                "https://example.com/",
                headers={
                    "Host": "127.0.0.1",
                    "Content-Length": "999",
                    "Connection": "keep-alive",
                    "X-Test": "allowed",
                },
            )

        sent_headers = request.await_args.kwargs["headers"]
        self.assertNotIn("Host", sent_headers)
        self.assertNotIn("Content-Length", sent_headers)
        self.assertNotIn("Connection", sent_headers)
        self.assertEqual(sent_headers["X-Test"], "allowed")

    async def test_json_request_rejects_method_before_network_access(self) -> None:
        resolve = AsyncMock()
        request = AsyncMock()
        with (
            patch.object(platform_common, "resolve_public_http_url", new=resolve),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
            self.assertRaisesRegex(ValueError, "unsupported_http_method"),
        ):
            await request_json("https://api.example.com/v1/models", method="DELETE")

        resolve.assert_not_awaited()
        request.assert_not_awaited()

    async def test_json_post_uses_pinned_single_hop_and_bounded_reader(self) -> None:
        public = _ResolvedTarget("api.example.com", 443, ("93.184.216.34",))
        request = AsyncMock(
            return_value=_HopResponse(
                status=200,
                text='{"choices": [{"message": {"content": "pong"}}]}',
                url="https://api.example.com/v1/chat/completions",
                content_type="application/json",
            )
        )
        resolve = AsyncMock(return_value=public)
        body = {"model": "model-a", "messages": []}
        with (
            patch.object(platform_common, "resolve_public_http_url", new=resolve),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            status, payload, final_url, error = await request_json(
                "https://api.example.com/v1/chat/completions",
                method="post",
                headers={
                    "Authorization": "Bearer group-secret",
                    "Host": "127.0.0.1",
                    "Content-Length": "999",
                },
                json_body=body,
                max_response_bytes=1024,
                max_decoded_bytes=2048,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "pong")
        self.assertEqual(final_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(error, "")
        resolve.assert_awaited_once()
        kwargs = request.await_args.kwargs
        self.assertEqual(kwargs["target"], public)
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["json_body"], body)
        self.assertEqual(kwargs["max_response_bytes"], 1024)
        self.assertEqual(kwargs["max_decoded_bytes"], 2048)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer group-secret")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertNotIn("Host", kwargs["headers"])
        self.assertNotIn("Content-Length", kwargs["headers"])

    async def test_json_post_client_disables_redirects_at_http_layer(self) -> None:
        class AsyncContext:
            def __init__(self, value: object) -> None:
                self.value = value

            async def __aenter__(self) -> object:
                return self.value

            async def __aexit__(self, *_args: object) -> None:
                return None

        class FakeClientSession:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs
                self.request_args: tuple[object, ...] | None = None
                self.request_kwargs: dict[str, object] | None = None

            async def __aenter__(self) -> "FakeClientSession":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def request(self, *args: object, **kwargs: object) -> AsyncContext:
                self.request_args = args
                self.request_kwargs = kwargs
                response = SimpleNamespace(
                    status=200,
                    url="https://api.example.com/v1/chat/completions",
                    headers={"content-type": "application/json"},
                    connection=None,
                    content_length=12,
                    content=_ChunkStream([b'{"ok": true}']),
                    charset="utf-8",
                )
                return AsyncContext(response)

        public = _ResolvedTarget("api.example.com", 443, ("93.184.216.34",))
        sessions: list[FakeClientSession] = []
        connector = object()

        def new_session(**kwargs: object) -> FakeClientSession:
            session = FakeClientSession(**kwargs)
            sessions.append(session)
            return session

        with (
            patch.object(
                platform_common,
                "resolve_public_http_url",
                new=AsyncMock(return_value=public),
            ),
            patch.object(
                platform_common.aiohttp,
                "TCPConnector",
                return_value=connector,
            ) as tcp,
            patch.object(platform_common.aiohttp, "ClientSession", side_effect=new_session),
        ):
            status, payload, _final_url, error = await request_json(
                "https://api.example.com/v1/chat/completions",
                method="POST",
                headers={"Authorization": "Bearer group-secret"},
                json_body={"model": "model-a"},
            )

        self.assertEqual((status, payload, error), (200, {"ok": True}, ""))
        tcp.assert_called_once_with(
            resolver=ANY,
            use_dns_cache=False,
            force_close=True,
            limit=1,
        )
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertIs(session.kwargs["connector"], connector)
        self.assertFalse(session.kwargs["auto_decompress"])
        self.assertFalse(session.kwargs["trust_env"])
        self.assertEqual(
            session.request_args,
            ("POST", "https://api.example.com/v1/chat/completions"),
        )
        self.assertEqual(session.request_kwargs["json"], {"model": "model-a"})
        self.assertFalse(session.request_kwargs["allow_redirects"])
        self.assertEqual(
            session.kwargs["headers"]["Authorization"], "Bearer group-secret"
        )

    async def test_json_redirect_is_not_followed_or_given_auth_again(self) -> None:
        public = _ResolvedTarget("api.example.com", 443, ("93.184.216.34",))
        resolve = AsyncMock(return_value=public)
        request = AsyncMock(
            return_value=_HopResponse(
                status=307,
                text="",
                url="https://api.example.com/v1/models",
                content_type="text/html",
                location="https://attacker.example/steal",
            )
        )
        with (
            patch.object(platform_common, "resolve_public_http_url", new=resolve),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            result = await request_json(
                "https://api.example.com/v1/models",
                headers={"Authorization": "Bearer group-secret"},
            )

        self.assertEqual(
            result,
            (
                307,
                None,
                "https://api.example.com/v1/models",
                "redirect_not_allowed",
            ),
        )
        resolve.assert_awaited_once()
        request.assert_awaited_once()

    async def test_json_request_cleans_non_json_error_body(self) -> None:
        public = _ResolvedTarget("api.example.com", 443, ("93.184.216.34",))
        request = AsyncMock(
            return_value=_HopResponse(
                status=502,
                text="  upstream\x00 failed\n\nplease retry  ",
                url="https://api.example.com/v1/models",
                content_type="text/plain",
            )
        )
        with (
            patch.object(
                platform_common,
                "resolve_public_http_url",
                new=AsyncMock(return_value=public),
            ),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            status, payload, _final_url, error = await request_json(
                "https://api.example.com/v1/models"
            )

        self.assertEqual(status, 502)
        self.assertIsNone(payload)
        self.assertEqual(error, "upstream failed\n\nplease retry")

    async def test_json_request_bounds_non_json_error_preview(self) -> None:
        public = _ResolvedTarget("api.example.com", 443, ("93.184.216.34",))
        request = AsyncMock(
            return_value=_HopResponse(
                status=500,
                text="X" * 1000,
                url="https://api.example.com/v1/models",
                content_type="text/plain",
            )
        )
        with (
            patch.object(
                platform_common,
                "resolve_public_http_url",
                new=AsyncMock(return_value=public),
            ),
            patch.object(platform_common, "_fetch_text_one_hop", new=request),
        ):
            _status, _payload, _final_url, error = await request_json(
                "https://api.example.com/v1/models"
            )

        self.assertEqual(error, ("X" * 300) + " ...")

    async def test_host_allowlist_rejects_suffix_confusion_and_credentials(self) -> None:
        with self.assertRaisesRegex(UnsafeUrlError, "host_not_allowed"):
            await resolve_public_http_url(
                "https://weibo.com.evil.example/post",
                allowed_hosts=("weibo.com",),
            )
        with self.assertRaisesRegex(UnsafeUrlError, "url_credentials_not_allowed"):
            await resolve_public_http_url("https://user:pass@example.com/")
        for malformed in (
            "https://example.com\\@127.0.0.1/",
            "https://example.com/\nheader: injected",
        ):
            with self.subTest(url=malformed), self.assertRaises(UnsafeUrlError):
                await resolve_public_http_url(malformed)

    async def test_streaming_reader_stops_once_wire_limit_is_crossed(self) -> None:
        response = SimpleNamespace(
            content_length=None,
            content=_ChunkStream([b"1234", b"5678"]),
        )
        with self.assertRaises(ResponseTooLargeError):
            await _read_limited_raw_body(response, 6)

    def test_gzip_decompression_limit_blocks_compression_bomb(self) -> None:
        compressed = gzip.compress(b"A" * 4096)
        with self.assertRaises(ResponseTooLargeError):
            _decompress_limited(compressed, "gzip", 128)

    def test_content_type_allowlist_rejects_binary_payloads(self) -> None:
        self.assertTrue(_content_type_allowed("application/problem+json", ("application/json",)))
        self.assertTrue(_content_type_allowed("image/jpeg", ("image/",)))
        self.assertFalse(_content_type_allowed("application/octet-stream", ("text/html",)))
        self.assertFalse(_content_type_allowed("", ("text/plain",)))

    async def test_blocking_search_wait_has_own_timeout(self) -> None:
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await run_search_thread(
                lambda: (time.sleep(0.08), [])[1],
                timeout_sec=0.01,
            )
        self.assertLess(time.monotonic() - started, 0.06)

    async def test_timed_out_search_threads_remain_strictly_bounded(self) -> None:
        release = threading.Event()
        started: list[int] = []

        def stuck() -> list[object]:
            started.append(1)
            release.wait(timeout=1.0)
            return []

        try:
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    await run_search_thread(stuck, timeout_sec=0.01)
            self.assertEqual(len(started), 2)
        finally:
            release.set()
            await asyncio.sleep(0.02)

    async def test_acquire_cancel_same_tick_returns_won_permit(self) -> None:
        class RacingSemaphore:
            def __init__(self) -> None:
                self.owner: asyncio.Task[object] | None = None
                self.acquired = 0
                self.released = 0

            async def acquire(self) -> bool:
                self.acquired += 1
                assert self.owner is not None
                asyncio.get_running_loop().call_soon(self.owner.cancel)
                return True

            def release(self) -> None:
                self.released += 1

        semaphore = RacingSemaphore()

        async def invoke() -> object:
            return await platform_common._run_bounded_daemon_thread(
                lambda: [],
                timeout_sec=1.0,
                limit_name="cancel-race",
                capacity=1,
            )

        with patch.object(platform_common, "_thread_limit", return_value=semaphore):
            owner = asyncio.create_task(invoke())
            semaphore.owner = owner
            result = await asyncio.gather(owner, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(semaphore.acquired, 1)
        self.assertEqual(semaphore.released, 1)

    async def test_thread_start_failure_returns_acquired_permit(self) -> None:
        class TrackingSemaphore:
            def __init__(self) -> None:
                self.acquired = 0
                self.released = 0

            async def acquire(self) -> bool:
                self.acquired += 1
                return True

            def release(self) -> None:
                self.released += 1

        semaphore = TrackingSemaphore()
        with (
            patch.object(platform_common, "_thread_limit", return_value=semaphore),
            patch.object(
                platform_common.threading.Thread,
                "start",
                side_effect=RuntimeError("cannot start worker"),
            ),
            self.assertRaisesRegex(RuntimeError, "cannot start worker"),
        ):
            await platform_common._run_bounded_daemon_thread(
                lambda: [],
                timeout_sec=1.0,
                limit_name="start-failure",
                capacity=1,
            )

        self.assertEqual(semaphore.acquired, 1)
        self.assertEqual(semaphore.released, 1)


if __name__ == "__main__":
    unittest.main()
