import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.skills.movie_info import MovieInfoSkill


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "movie_info_enabled": True,
        "movie_info_http_timeout_sec": 6.0,
        "movie_info_max_results": 5,
        "movie_info_default_language": "zh-CN",
        "movie_info_default_region": "CN",
        "movie_info_tmdb_read_access_token": "tmdb-read-token",
        "movie_info_imdb_data_set_id": "dataset-123",
        "movie_info_imdb_revision_id": "revision-456",
        "movie_info_imdb_asset_id": "asset-789",
        "movie_info_imdb_api_key": "imdb-api-key",
        "movie_info_imdb_aws_access_key_id": "AKIDEXAMPLE",
        "movie_info_imdb_aws_secret_access_key": (
            "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
        ),
        "movie_info_imdb_aws_session_token": "session-token",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _tmdb_search_payload() -> dict:
    return {
        "page": 1,
        "results": [
            {
                "id": 27205,
                "title": "盗梦空间",
                "original_title": "Inception",
                "overview": "一名窃取梦境秘密的盗贼接受了反向任务。",
                "release_date": "2010-07-16",
                "genre_ids": [28, 878],
                "vote_average": 8.37,
                "vote_count": 37000,
                "poster_path": "/inception.jpg",
            }
        ],
    }


def _tmdb_detail_payload() -> dict:
    return {
        "id": 27205,
        "imdb_id": "tt1375666",
        "title": "盗梦空间",
        "original_title": "Inception",
        "overview": "一名窃取梦境秘密的盗贼接受了反向任务。",
        "release_date": "2010-07-16",
        "runtime": 148,
        "status": "Released",
        "genres": [{"id": 28, "name": "动作"}, {"id": 878, "name": "科幻"}],
        "vote_average": 8.37,
        "vote_count": 37000,
        "poster_path": "/inception.jpg",
        "external_ids": {"imdb_id": "tt1375666"},
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": "CN",
                    "release_dates": [
                        {
                            "certification": "PG-13",
                            "release_date": "2010-09-01T00:00:00.000Z",
                            "type": 3,
                            "note": "",
                        }
                    ],
                }
            ]
        },
    }


def _imdb_movie(
    *,
    imdb_id: str = "tt1375666",
    title: str = "Inception",
    type_id: str = "movie",
) -> dict:
    return {
        "id": imdb_id,
        "titleText": {"text": title},
        "originalTitleText": {"text": "Inception"},
        "releaseDate": {"year": 2010, "month": 7, "day": 16},
        "releaseYear": {"year": 2010},
        "titleType": {"id": type_id, "text": "Movie"},
        "primaryImage": {"url": "https://m.media-amazon.com/inception.jpg"},
        "plots": {
            "edges": [
                {
                    "node": {
                        "plotText": {
                            "plainText": "A thief who steals corporate secrets."
                        }
                    }
                }
            ]
        },
        "titleGenres": {
            "genres": [
                {"genre": {"text": "Action"}},
                {"genre": {"text": "Sci-Fi"}},
            ]
        },
        "runtime": {"seconds": 8880},
        "ratingsSummary": {"aggregateRating": 8.8, "voteCount": 2700000},
    }


def _imdb_response(data: dict, *, disclaimer: str = "IMDb data disclaimer") -> dict:
    return {
        "data": data,
        "extensions": {"disclaimer": disclaimer},
    }


class MovieInfoSchemaAndAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_exposes_movie_only_actions_and_sources(self) -> None:
        skill = MovieInfoSkill(_settings())

        self.assertEqual(
            skill.parameters_schema["properties"]["action"]["enum"],
            ["search", "details", "trending", "now_playing", "upcoming"],
        )
        self.assertEqual(
            skill.parameters_schema["properties"]["source"]["enum"],
            ["auto", "tmdb", "imdb", "both"],
        )
        self.assertTrue(skill.available)

    async def test_disabled_skill_returns_structured_error(self) -> None:
        skill = MovieInfoSkill(_settings(movie_info_enabled=False))

        result = await skill.run({"action": "search", "query": "Inception"}, SimpleNamespace())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "disabled")
        self.assertEqual(result.payload["providers_used"], [])
        self.assertIn("fetched_at", result.payload)
        self.assertEqual(result.payload["results"], [])

    async def test_unconfigured_requested_source_is_not_called(self) -> None:
        skill = MovieInfoSkill(
            _settings(
                movie_info_tmdb_read_access_token="",
                movie_info_imdb_api_key="",
            )
        )
        request = AsyncMock()

        with patch.object(skill, "_request_json", new=request):
            result = await skill.run(
                {"action": "search", "query": "Inception", "source": "both"},
                SimpleNamespace(),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_configured")
        self.assertEqual(
            result.payload["provider_errors"],
            {"tmdb": "not_configured", "imdb": "not_configured"},
        )
        request.assert_not_awaited()

    async def test_search_and_details_validate_required_arguments(self) -> None:
        skill = MovieInfoSkill(_settings())

        search = await skill.run({"action": "search"}, SimpleNamespace())
        details = await skill.run({"action": "details"}, SimpleNamespace())
        invalid_imdb = await skill.run(
            {"action": "details", "imdb_id": "inception"}, SimpleNamespace()
        )

        self.assertEqual(search.error, "empty_query")
        self.assertEqual(details.error, "missing_movie_id")
        self.assertEqual(invalid_imdb.error, "invalid_imdb_id")

    def test_unrated_imdb_movies_do_not_expose_zero_as_a_real_score(self) -> None:
        raw = _imdb_movie()
        raw["ratingsSummary"] = {"aggregateRating": 0.0, "voteCount": 0}

        row = MovieInfoSkill._normalize_imdb_movie(raw)

        self.assertIsNotNone(row)
        self.assertEqual(row["ratings"]["imdb"], {"score": None, "vote_count": 0})


class MovieInfoTmdbTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmdb_search_uses_bearer_header_and_normalizes_result(self) -> None:
        skill = MovieInfoSkill(_settings(movie_info_max_results=3))
        request = AsyncMock(return_value=(200, _tmdb_search_payload()))

        with patch.object(skill, "_request_json", new=request):
            result = await skill.run(
                {
                    "action": "search",
                    "source": "tmdb",
                    "query": "盗梦空间",
                    "year": 2010,
                    "language": "zh-cn",
                    "region": "cn",
                    "max_results": 20,
                },
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["providers_used"], ["tmdb"])
        self.assertEqual(result.payload["provider_errors"], {})
        self.assertRegex(result.payload["fetched_at"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertIn("uses the TMDB API", result.payload["attribution"])
        row = result.payload["results"][0]
        self.assertEqual(row["source"], "tmdb")
        self.assertEqual(row["ids"], {"tmdb": 27205, "imdb": None})
        self.assertEqual(row["year"], 2010)
        self.assertEqual(row["genres"], ["Action", "Science Fiction"])
        self.assertEqual(row["ratings"]["tmdb"]["score"], 8.37)
        self.assertIsNone(row["ratings"]["imdb"]["score"])
        self.assertEqual(row["url"], "https://www.themoviedb.org/movie/27205")

        kwargs = request.await_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "https://api.themoviedb.org/3/search/movie")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tmdb-read-token")
        self.assertEqual(kwargs["params"]["year"], 2010)
        self.assertEqual(kwargs["params"]["language"], "zh-CN")
        self.assertEqual(kwargs["params"]["region"], "CN")
        self.assertNotIn("api_key", kwargs["params"])
        self.assertNotIn("tmdb-read-token", json.dumps(kwargs["params"]))

    def test_unrated_movies_do_not_expose_zero_as_a_real_score(self) -> None:
        raw = dict(_tmdb_search_payload()["results"][0])
        raw.update(vote_average=0.0, vote_count=0)

        row = MovieInfoSkill._normalize_tmdb_movie(raw)

        self.assertEqual(row["ratings"]["tmdb"], {"score": None, "vote_count": 0})

    def test_regional_release_prefers_theatrical_availability(self) -> None:
        raw = _tmdb_detail_payload()
        raw["release_dates"]["results"][0]["release_dates"].insert(
            0,
            {
                "certification": "",
                "release_date": "2010-08-20T00:00:00.000Z",
                "type": 4,
                "note": "Digital preview",
            },
        )

        row = MovieInfoSkill._normalize_tmdb_movie(raw, region="CN")

        self.assertEqual(row["regional_release"]["date"], "2010-09-01")
        self.assertEqual(row["regional_release"]["type"], 3)

    async def test_now_playing_uses_movie_endpoint_and_configured_result_limit(self) -> None:
        payload = _tmdb_search_payload()
        payload["results"] = payload["results"] * 4
        skill = MovieInfoSkill(_settings(movie_info_max_results=2))
        request = AsyncMock(return_value=(200, payload))

        with patch.object(skill, "_request_json", new=request):
            result = await skill.run(
                {
                    "action": "now_playing",
                    "source": "tmdb",
                    "max_results": 20,
                    "page": 2,
                },
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.payload["results"]), 1)  # Duplicate movies are merged.
        kwargs = request.await_args.kwargs
        self.assertEqual(kwargs["url"], "https://api.themoviedb.org/3/movie/now_playing")
        self.assertEqual(kwargs["params"]["page"], 2)
        self.assertEqual(kwargs["params"]["region"], "CN")

    async def test_tmdb_details_can_resolve_an_imdb_id_via_find(self) -> None:
        skill = MovieInfoSkill(_settings())

        async def fake_request(**kwargs):
            if "/find/tt1375666" in kwargs["url"]:
                return 200, {"movie_results": [{"id": 27205}]}
            if kwargs["url"].endswith("/movie/27205"):
                return 200, _tmdb_detail_payload()
            raise AssertionError(kwargs["url"])

        request = AsyncMock(side_effect=fake_request)
        with patch.object(skill, "_request_json", new=request):
            result = await skill.run(
                {"action": "details", "source": "tmdb", "imdb_id": "tt1375666"},
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["entry"]["ids"]["tmdb"], 27205)
        self.assertEqual(
            result.payload["entry"]["regional_release"],
            {
                "date": "2010-09-01",
                "certification": "PG-13",
                "type": 3,
                "note": "",
                "status": "released",
                "region": "CN",
            },
        )
        self.assertEqual(request.await_count, 2)
        self.assertEqual(
            request.await_args_list[0].kwargs["params"]["external_source"],
            "imdb_id",
        )


class MovieInfoAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_aggregates_tmdb_and_imdb_without_mixing_ratings(self) -> None:
        skill = MovieInfoSkill(_settings())
        seen: list[dict] = []

        async def fake_request(**kwargs):
            seen.append(kwargs)
            if kwargs["method"] == "GET":
                return 200, _tmdb_detail_payload()
            body = json.loads(kwargs["body"].decode("utf-8"))
            self.assertEqual(body["variables"], {"id": "tt1375666"})
            return 200, _imdb_response({"title": _imdb_movie()})

        with patch.object(skill, "_request_json", new=AsyncMock(side_effect=fake_request)):
            result = await skill.run(
                {"action": "details", "source": "both", "tmdb_id": 27205},
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["providers_used"], ["tmdb", "imdb"])
        self.assertEqual(result.payload["provider_errors"], {})
        self.assertEqual(result.payload["imdb_disclaimer"], "IMDb data disclaimer")
        self.assertIn("uses the TMDB API", result.payload["attribution"])
        entry = result.payload["entry"]
        self.assertEqual(entry["source"], "both")
        self.assertEqual(entry["ids"], {"tmdb": 27205, "imdb": "tt1375666"})
        self.assertEqual(entry["ratings"]["tmdb"], {"score": 8.37, "vote_count": 37000})
        self.assertEqual(entry["ratings"]["imdb"], {"score": 8.8, "vote_count": 2700000})
        self.assertEqual(entry["runtime_minutes"], 148)
        self.assertEqual(entry["regional_release"]["region"], "CN")
        self.assertEqual(entry["regional_release"]["date"], "2010-09-01")
        self.assertEqual(entry["genres"], ["动作", "科幻", "Action", "Sci-Fi"])
        self.assertEqual(entry["url"], "https://www.themoviedb.org/movie/27205")
        self.assertEqual(entry["content"], "一名窃取梦境秘密的盗贼接受了反向任务。")

        post = next(call for call in seen if call["method"] == "POST")
        body_text = post["body"].decode("utf-8")
        self.assertNotIn("imdb-api-key", body_text)
        self.assertNotIn("EXAMPLEKEY", body_text)
        self.assertEqual(
            post["headers"]["x-amzn-dataexchange-header-x-api-key"],
            "imdb-api-key",
        )
        self.assertTrue(post["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 "))

        tmdb_call = next(call for call in seen if call["method"] == "GET")
        self.assertEqual(
            tmdb_call["params"]["append_to_response"],
            "external_ids,release_dates",
        )

    async def test_one_provider_failure_returns_the_other_with_provider_error(self) -> None:
        skill = MovieInfoSkill(_settings())
        imdb_rows = {
            "mainSearch": {
                "edges": [
                    {"node": {"entity": _imdb_movie()}},
                    {
                        "node": {
                            "entity": _imdb_movie(
                                imdb_id="tt0944947",
                                title="Game of Thrones",
                                type_id="tvSeries",
                            )
                        }
                    },
                ]
            }
        }

        async def fake_request(**kwargs):
            if kwargs["method"] == "GET":
                raise asyncio.TimeoutError
            return 200, _imdb_response(imdb_rows)

        with patch.object(skill, "_request_json", new=AsyncMock(side_effect=fake_request)):
            result = await skill.run(
                {"action": "search", "source": "both", "query": "Inception"},
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["providers_used"], ["imdb"])
        self.assertEqual(result.payload["provider_errors"], {"tmdb": "network_error"})
        self.assertEqual(len(result.payload["results"]), 1)
        self.assertEqual(result.payload["results"][0]["ids"]["imdb"], "tt1375666")
        self.assertNotIn("attribution", result.payload)

    async def test_same_title_and_year_search_rows_are_merged(self) -> None:
        skill = MovieInfoSkill(_settings())

        async def fake_request(**kwargs):
            if kwargs["method"] == "GET":
                return 200, _tmdb_search_payload()
            return 200, _imdb_response(
                {"mainSearch": {"edges": [{"node": {"entity": _imdb_movie()}}]}}
            )

        with patch.object(skill, "_request_json", new=AsyncMock(side_effect=fake_request)):
            result = await skill.run(
                {"action": "search", "source": "both", "query": "Inception"},
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.payload["results"]), 1)
        row = result.payload["results"][0]
        self.assertEqual(row["source"], "both")
        self.assertEqual(row["ids"]["tmdb"], 27205)
        self.assertEqual(row["ids"]["imdb"], "tt1375666")

    async def test_second_page_does_not_mix_in_imdb_first_page(self) -> None:
        skill = MovieInfoSkill(_settings())
        request = AsyncMock(return_value=(200, _tmdb_search_payload()))

        with patch.object(skill, "_request_json", new=request):
            result = await skill.run(
                {
                    "action": "search",
                    "source": "both",
                    "query": "Inception",
                    "page": 2,
                },
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["providers_used"], ["tmdb"])
        self.assertEqual(
            result.payload["provider_errors"],
            {"imdb": "pagination_unsupported"},
        )
        request.assert_awaited_once()

    async def test_conflicting_detail_ids_do_not_mix_provider_records(self) -> None:
        skill = MovieInfoSkill(_settings())

        async def fake_request(**kwargs):
            if kwargs["method"] == "GET":
                return 200, _tmdb_detail_payload()
            return 200, _imdb_response(
                {"title": _imdb_movie(imdb_id="tt9999999", title="Wrong Movie")}
            )

        with patch.object(skill, "_request_json", new=AsyncMock(side_effect=fake_request)):
            result = await skill.run(
                {
                    "action": "details",
                    "source": "both",
                    "tmdb_id": 27205,
                    "imdb_id": "tt9999999",
                },
                SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["providers_used"], ["tmdb"])
        self.assertEqual(result.payload["provider_errors"], {"imdb": "id_mismatch"})
        self.assertIsNone(result.payload["entry"]["ratings"]["imdb"]["score"])

    async def test_all_provider_failures_return_failed_result(self) -> None:
        skill = MovieInfoSkill(_settings())
        request = AsyncMock(side_effect=OSError("offline"))

        with patch.object(skill, "_request_json", new=request):
            result = await skill.run(
                {"action": "search", "source": "both", "query": "Inception"},
                SimpleNamespace(),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "provider_failed")
        self.assertEqual(result.payload["providers_used"], [])
        self.assertEqual(
            result.payload["provider_errors"],
            {"tmdb": "provider_error", "imdb": "provider_error"},
        )

    async def test_imdb_only_ranked_lists_are_explicitly_unsupported(self) -> None:
        skill = MovieInfoSkill(_settings())

        for action in ("trending", "now_playing", "upcoming"):
            with self.subTest(action=action):
                result = await skill.run(
                    {"action": action, "source": "imdb"},
                    SimpleNamespace(),
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.error, "unsupported_action")
                self.assertEqual(
                    result.payload["provider_errors"],
                    {"imdb": "unsupported_action"},
                )

    async def test_successful_empty_search_is_no_results_not_provider_failure(self) -> None:
        skill = MovieInfoSkill(_settings())

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value=(200, {"page": 1, "results": []})),
        ):
            result = await skill.run(
                {"action": "search", "source": "tmdb", "query": "不存在的电影"},
                SimpleNamespace(),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_results")
        self.assertEqual(result.payload["providers_used"], ["tmdb"])
        self.assertEqual(result.payload["results"], [])


class MovieInfoSigV4Tests(unittest.TestCase):
    def test_sigv4_headers_are_deterministic_and_include_session_token(self) -> None:
        skill = MovieInfoSkill(_settings())
        body = b'{"query":"query Test { title(id: \\"tt1375666\\") { id } }"}'
        headers = skill._imdb_signed_headers(
            body,
            now=datetime(2026, 7, 21, 9, 30, 45, tzinfo=timezone.utc),
        )

        self.assertEqual(headers["host"], "api-fulfill.dataexchange.us-east-1.amazonaws.com")
        self.assertEqual(headers["x-amz-date"], "20260721T093045Z")
        self.assertEqual(headers["x-amz-security-token"], "session-token")
        self.assertEqual(headers["x-amzn-dataexchange-http-method"], "POST")
        self.assertEqual(headers["x-amzn-dataexchange-path"], "/v1")
        self.assertEqual(headers["x-amzn-dataexchange-data-set-id"], "dataset-123")
        self.assertEqual(headers["x-amzn-dataexchange-revision-id"], "revision-456")
        self.assertEqual(headers["x-amzn-dataexchange-asset-id"], "asset-789")
        self.assertEqual(
            headers["x-amz-content-sha256"],
            "53fb3c2eb362bd94f762bc837b653bc1bcb7a3245dc8dfda509a755a86ede27e",
        )
        self.assertEqual(
            headers["authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260721/us-east-1/dataexchange/aws4_request, "
            "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token;"
            "x-amzn-dataexchange-asset-id;x-amzn-dataexchange-data-set-id;"
            "x-amzn-dataexchange-header-content-type;x-amzn-dataexchange-header-x-api-key;"
            "x-amzn-dataexchange-http-method;x-amzn-dataexchange-path;"
            "x-amzn-dataexchange-revision-id, "
            "Signature=3a3a56fe9670b526aa4364031f64bce5a7b8f8f1106b329813cbec8c9f7d295c",
        )

    def test_sigv4_omits_security_token_when_not_configured(self) -> None:
        skill = MovieInfoSkill(_settings(movie_info_imdb_aws_session_token=""))

        headers = skill._imdb_signed_headers(
            b"{}",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        self.assertNotIn("x-amz-security-token", headers)
        self.assertNotIn("x-amz-security-token", headers["authorization"])


if __name__ == "__main__":
    unittest.main()
