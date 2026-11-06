from __future__ import annotations

import io
import json
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from spell_collector.sources.arctic_shift import (
    ArcticShiftClient,
    ArcticShiftConfig,
    ArcticShiftError,
)


class FakeResponse(io.BytesIO):
    def __init__(self, payload: object) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.headers: dict[str, str] = {}


class FixtureOpener:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request, timeout):  # noqa: ANN001, ANN204
        self.requests.append((request, timeout))
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/comments/search":
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": "c1",
                            "link_id": "t3_p1",
                            "parent_id": "t3_p1",
                            "subreddit": "Spells",
                            "body": "First comment",
                        },
                        {
                            "id": "c2",
                            "link_id": "t3_p2",
                            "parent_id": "t1_c1",
                            "subreddit": "Spells",
                            "body": "Second comment",
                        },
                    ][: int(query["limit"][0])]
                }
            )
        if parsed.path == "/api/posts/ids":
            ids = query["ids"][0].split(",")
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": post_id,
                            "subreddit": "Spells",
                            "title": f"Post {post_id}",
                            "selftext": "Steps",
                        }
                        for post_id in ids
                    ]
                }
            )
        if parsed.path == "/api/posts/search":
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": post_id,
                            "subreddit": "Spells",
                            "title": f"Post {post_id}",
                            "selftext": "Steps",
                        }
                        for post_id in ("p1", "p2", "p3", "p4", "p5")
                    ][: int(query["limit"][0])]
                }
            )
        raise AssertionError(f"onverwachte route: {parsed.path}")


def config(**overrides) -> ArcticShiftConfig:  # noqa: ANN003
    values = {
        "polite_delay_seconds": 0,
        "max_retries": 1,
        "max_response_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return ArcticShiftConfig(**values)


def test_pilot_is_bounded_and_links_comments_to_posts() -> None:
    opener = FixtureOpener()
    client = ArcticShiftClient(config(), opener=opener)

    result = client.collect_pilot("r/Spells", max_records=6, comment_limit=2)

    assert result.record_count == 6
    assert len(result.posts) == 4
    assert len(result.comments) == 2
    assert result.source_route == "arctic_shift_api"
    assert result.comments[0]["source_post_id"] == "p1"
    assert result.comments[0]["post_in_pilot"] is True
    assert result.comments[1]["parent_kind"] == "comment"
    assert result.posts[0]["source_comment_ids"] == ["c1"]
    assert result.posts[0]["source"]["endpoint"] == "/api/posts/ids"
    assert result.posts[-1]["source"]["endpoint"] == "/api/posts/search"
    assert all(record["source"]["provider"] == "arctic_shift" for record in result.records)


def test_pilot_never_accepts_more_than_one_hundred_records() -> None:
    opener = FixtureOpener()
    client = ArcticShiftClient(config(), opener=opener)

    with pytest.raises(ValueError, match="tussen 1 en 100"):
        client.collect_pilot("Spells", max_records=101)

    assert opener.requests == []


def test_required_link_fields_are_always_selected() -> None:
    opener = FixtureOpener()
    client = ArcticShiftClient(config(), opener=opener)

    client.fetch_comments("Spells", limit=1, fields=("body",))

    query = parse_qs(urlparse(opener.requests[0][0].full_url).query)
    fields = query["fields"][0].split(",")
    assert {"id", "subreddit", "body", "link_id", "parent_id"} <= set(fields)
    assert query["limit"] == ["1"]
    assert opener.requests[0][0].get_method() == "GET"


def test_429_honours_reset_header_then_retries() -> None:
    calls = 0
    sleeps: list[float] = []

    def opener(request, timeout):  # noqa: ANN001, ANN202, ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = Message()
            headers["X-RateLimit-Reset"] = "2"
            raise HTTPError(request.full_url, 429, "rate limited", headers, None)
        return FakeResponse({"data": []})

    client = ArcticShiftClient(config(), opener=opener, sleep=sleeps.append)

    assert client.fetch_posts("Spells", limit=1) == []
    assert calls == 2
    assert sleeps == [2.0]


def test_response_body_has_a_hard_byte_limit() -> None:
    def opener(request, timeout):  # noqa: ANN001, ANN202, ARG001
        return FakeResponse({"data": [{"body": "x" * 500}]})

    client = ArcticShiftClient(config(max_response_bytes=100), opener=opener)

    with pytest.raises(ArcticShiftError, match="opslaggrens"):
        client.fetch_comments("Spells", limit=1)


@pytest.mark.parametrize("limit", [0, 101, True])
def test_search_limits_are_rejected_before_network(limit) -> None:  # noqa: ANN001
    opener = FixtureOpener()
    client = ArcticShiftClient(config(), opener=opener)

    with pytest.raises(ValueError, match="tussen 1 en 100"):
        client.fetch_posts("Spells", limit=limit)

    assert opener.requests == []
