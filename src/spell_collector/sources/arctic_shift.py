"""Begrensde Arctic Shift API-bron voor kleine, toegestane pilots.

Deze module gebruikt uitsluitend gedocumenteerde HTTP GET-routes. Er is bewust
geen proxyrotatie, CAPTCHA-omzeiling of andere toegangs-bypass aanwezig. Voor
grote verzamelingen horen lokaal verkregen maandarchieven te worden gebruikt.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from typing import Any, BinaryIO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ARCTIC_SHIFT_BASE_URL = "https://arctic-shift.photon-reddit.com"
PILOT_MAX_RECORDS = 100

POST_FIELDS = (
    "id",
    "subreddit",
    "title",
    "selftext",
    "author",
    "created_utc",
    "retrieved_on",
    "num_comments",
    "crosspost_parent",
    "url",
    "over_18",
)
COMMENT_FIELDS = (
    "id",
    "subreddit",
    "body",
    "author",
    "created_utc",
    "retrieved_on",
    "link_id",
    "parent_id",
)


class ArcticShiftError(RuntimeError):
    """Een veilige, gebruikersgerichte fout van de Arctic Shift-adapter."""


class ResponseLike(Protocol):
    """Klein protocol zodat offline fixtures als HTTP-response kunnen dienen."""

    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


OpenUrl = Callable[[Request, float], ResponseLike]


@dataclass(frozen=True, slots=True)
class ArcticShiftConfig:
    """Netwerkgrenzen voor de gratis Arctic Shift API."""

    base_url: str = ARCTIC_SHIFT_BASE_URL
    timeout_seconds: float = 30.0
    polite_delay_seconds: float = 0.5
    max_response_bytes: int = 16 * 1024 * 1024
    max_retries: int = 3
    max_backoff_seconds: float = 120.0
    user_agent: str = "MagicalSpellCollector/0.1 (local non-commercial research)"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds moet groter zijn dan 0")
        if self.polite_delay_seconds < 0:
            raise ValueError("polite_delay_seconds mag niet negatief zijn")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes moet groter zijn dan 0")
        if self.max_retries < 0:
            raise ValueError("max_retries mag niet negatief zijn")
        if self.max_backoff_seconds <= 0:
            raise ValueError("max_backoff_seconds moet groter zijn dan 0")
        if not self.base_url.startswith("https://"):
            raise ValueError("Arctic Shift moet via HTTPS worden benaderd")


@dataclass(slots=True)
class PilotCollection:
    """Resultaat van een begrensde pilot met expliciete bronroute."""

    subreddit: str
    max_records: int
    posts: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    source_route: str = "arctic_shift_api"

    @property
    def records(self) -> list[dict[str, Any]]:
        return [*self.posts, *self.comments]

    @property
    def record_count(self) -> int:
        return len(self.posts) + len(self.comments)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subreddit": self.subreddit,
            "max_records": self.max_records,
            "record_count": self.record_count,
            "source_route": self.source_route,
            "posts": self.posts,
            "comments": self.comments,
        }


class ArcticShiftClient:
    """Een kleine, injecteerbare en begrensde Arctic Shift HTTP-client."""

    def __init__(
        self,
        config: ArcticShiftConfig | None = None,
        *,
        opener: OpenUrl | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or ArcticShiftConfig()
        self._opener = opener or self._stdlib_open
        self._sleep = sleep
        self._now = now
        self._request_count = 0

    def _stdlib_open(self, request: Request, timeout: float) -> ResponseLike:
        # urlopen gebruikt de normale systeemnetwerkconfiguratie. Deze adapter
        # voegt bewust geen proxyrotatie of omzeilingsmechanisme toe.
        return urlopen(request, timeout=timeout)  # noqa: S310

    def fetch_posts(
        self,
        subreddit: str,
        *,
        limit: int,
        fields: Iterable[str] = POST_FIELDS,
        sort: str = "asc",
        after: str | int | None = None,
        before: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """Haal maximaal 100 posts op via de gedocumenteerde zoekroute."""

        params = self._search_params(
            subreddit,
            limit=limit,
            fields=self._required_fields(fields, ("id", "subreddit", "selftext")),
            sort=sort,
            after=after,
            before=before,
        )
        return self._get_data("/api/posts/search", params)

    def fetch_comments(
        self,
        subreddit: str,
        *,
        limit: int,
        fields: Iterable[str] = COMMENT_FIELDS,
        sort: str = "asc",
        after: str | int | None = None,
        before: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """Haal maximaal 100 reacties op, inclusief post- en parentkoppeling."""

        params = self._search_params(
            subreddit,
            limit=limit,
            fields=self._required_fields(
                fields, ("id", "subreddit", "body", "link_id", "parent_id")
            ),
            sort=sort,
            after=after,
            before=before,
        )
        return self._get_data("/api/comments/search", params)

    def fetch_posts_by_ids(
        self,
        post_ids: Iterable[str],
        *,
        fields: Iterable[str] = POST_FIELDS,
    ) -> list[dict[str, Any]]:
        """Haal gekoppelde parentposts in een enkele gedocumenteerde batch op."""

        ids = list(dict.fromkeys(self._bare_id(value) for value in post_ids if value))
        if not ids:
            return []
        if len(ids) > 500:
            raise ValueError("Arctic Shift accepteert maximaal 500 ID's per batch")
        params = {
            "ids": ",".join(ids),
            "fields": ",".join(self._required_fields(fields, ("id", "subreddit", "selftext"))),
        }
        return self._get_data("/api/posts/ids", params)

    def collect_pilot(
        self,
        subreddit: str,
        *,
        max_records: int = PILOT_MAX_RECORDS,
        comment_limit: int | None = None,
        sort: str = "asc",
        after: str | int | None = None,
        before: str | int | None = None,
    ) -> PilotCollection:
        """Verzamel posts en reacties zonder ooit meer dan 100 bronrecords.

        Eerst worden reacties opgehaald. Hun unieke ``link_id``-posts worden in
        een batch gekoppeld; een eventueel resterend budget wordt met gewone
        posts gevuld. Zo blijft de context bruikbaar met slechts enkele requests.
        """

        subreddit = self._validate_subreddit(subreddit)
        self._validate_pilot_limit(max_records)
        if comment_limit is None:
            comment_limit = max_records // 2
        if not 0 <= comment_limit <= max_records:
            raise ValueError("comment_limit moet tussen 0 en max_records liggen")

        raw_comments = (
            self.fetch_comments(
                subreddit,
                limit=comment_limit,
                sort=sort,
                after=after,
                before=before,
            )
            if comment_limit
            else []
        )
        # Een onbetrouwbare serverrespons mag de lokale harde grens niet passeren.
        raw_comments = raw_comments[:comment_limit]
        post_budget = max_records - len(raw_comments)

        parent_ids = list(
            dict.fromkeys(
                self._bare_id(str(item.get("link_id", "")))
                for item in raw_comments
                if item.get("link_id")
            )
        )
        raw_posts = self.fetch_posts_by_ids(parent_ids[:post_budget]) if post_budget else []
        raw_posts = self._deduplicate(raw_posts)[:post_budget]
        post_endpoints = {
            self._bare_id(str(item.get("id", ""))): "/api/posts/ids" for item in raw_posts
        }

        missing = post_budget - len(raw_posts)
        if missing:
            extra = self.fetch_posts(
                subreddit,
                limit=min(missing + len(raw_posts), 100),
                sort=sort,
                after=after,
                before=before,
            )
            for item in extra:
                post_endpoints.setdefault(
                    self._bare_id(str(item.get("id", ""))), "/api/posts/search"
                )
            raw_posts = self._deduplicate([*raw_posts, *extra])[:post_budget]

        posts = [
            self._normalise_post(
                item,
                endpoint=post_endpoints.get(
                    self._bare_id(str(item.get("id", ""))), "/api/posts/search"
                ),
            )
            for item in raw_posts
        ]
        comments = [self._normalise_comment(item) for item in raw_comments]
        self._link_comments(posts, comments)

        result = PilotCollection(
            subreddit=subreddit,
            max_records=max_records,
            posts=posts,
            comments=comments,
        )
        if result.record_count > max_records or result.record_count > PILOT_MAX_RECORDS:
            raise AssertionError("interne fout: pilotgrens overschreden")
        return result

    @staticmethod
    def _validate_pilot_limit(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("max_records moet een geheel getal zijn")
        if not 1 <= value <= PILOT_MAX_RECORDS:
            raise ValueError("pilot max_records moet tussen 1 en 100 liggen")

    @staticmethod
    def _validate_subreddit(value: str) -> str:
        subreddit = value.strip()
        if subreddit.lower().startswith("r/"):
            subreddit = subreddit[2:]
        if not subreddit or any(char.isspace() for char in subreddit):
            raise ValueError("ongeldige subredditnaam")
        return subreddit

    def _search_params(
        self,
        subreddit: str,
        *,
        limit: int,
        fields: Iterable[str],
        sort: str,
        after: str | int | None,
        before: str | int | None,
    ) -> dict[str, str | int]:
        subreddit = self._validate_subreddit(subreddit)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("API search limit moet tussen 1 en 100 liggen")
        if sort not in {"asc", "desc"}:
            raise ValueError("sort moet 'asc' of 'desc' zijn")
        params: dict[str, str | int] = {
            "subreddit": subreddit,
            "limit": limit,
            "sort": sort,
            "fields": ",".join(fields),
        }
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        return params

    @staticmethod
    def _required_fields(fields: Iterable[str], required: Iterable[str]) -> tuple[str, ...]:
        clean = []
        for field_name in [*fields, *required]:
            name = str(field_name).strip()
            if not name or not name.replace("_", "").isalnum():
                raise ValueError(f"ongeldige veldnaam: {field_name!r}")
            if name not in clean:
                clean.append(name)
        return tuple(clean)

    def _get_data(self, endpoint: str, params: Mapping[str, str | int]) -> list[dict[str, Any]]:
        payload = self._request_json(endpoint, params)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ArcticShiftError("Arctic Shift-respons bevat geen geldige data-lijst")
        if not all(isinstance(item, dict) for item in data):
            raise ArcticShiftError("Arctic Shift-respons bevat een ongeldig record")
        return data

    def _request_json(self, endpoint: str, params: Mapping[str, str | int]) -> dict[str, Any]:
        url = f"{self.config.base_url.rstrip('/')}{endpoint}?{urlencode(params)}"
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": self.config.user_agent},
            method="GET",
        )

        for attempt in range(self.config.max_retries + 1):
            self._pace()
            response: ResponseLike | None = None
            try:
                response = self._opener(request, self.config.timeout_seconds)
                raw = self._read_bounded(response)
                decoded = json.loads(raw.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ArcticShiftError("Arctic Shift-respons is geen JSON-object")
                return decoded
            except HTTPError as exc:
                if exc.code != 429 or attempt >= self.config.max_retries:
                    raise ArcticShiftError(f"Arctic Shift HTTP-fout {exc.code}") from exc
                try:
                    self._sleep(self._retry_delay(exc.headers, attempt))
                finally:
                    exc.close()
            except (URLError, TimeoutError) as exc:
                raise ArcticShiftError(f"Arctic Shift is niet bereikbaar: {exc}") from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArcticShiftError("Arctic Shift gaf ongeldige JSON terug") from exc
            finally:
                if response is not None:
                    response.close()
        raise ArcticShiftError("Arctic Shift-request mislukte na herhaalde pogingen")

    def _pace(self) -> None:
        if self._request_count and self.config.polite_delay_seconds:
            self._sleep(self.config.polite_delay_seconds)
        self._request_count += 1

    def _read_bounded(self, response: BinaryIO) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > self.config.max_response_bytes:
                raise ArcticShiftError(
                    "Arctic Shift-respons overschrijdt de ingestelde opslaggrens"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _retry_delay(self, headers: Mapping[str, str] | Message, attempt: int) -> float:
        def header(name: str) -> str | None:
            value = headers.get(name)
            return str(value) if value is not None else None

        candidates: list[float] = []
        for name in ("X-RateLimit-Reset", "Retry-After"):
            value = header(name)
            try:
                if value is not None:
                    candidates.append(float(value))
            except ValueError:
                pass

        reset_at = header("X-RateLimit-Reset-At")
        try:
            if reset_at is not None:
                timestamp = float(reset_at)
                if timestamp > 10_000_000_000:  # gedocumenteerde milliseconden
                    timestamp /= 1000
                candidates.append(max(0.0, timestamp - self._now()))
        except ValueError:
            pass

        delay = max(candidates, default=float(2**attempt))
        return min(max(delay, self.config.polite_delay_seconds), self.config.max_backoff_seconds)

    @staticmethod
    def _deduplicate(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            record_id = str(record.get("id", ""))
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            unique.append(record)
        return unique

    @staticmethod
    def _bare_id(value: str) -> str:
        value = value.strip()
        return value[3:] if value.startswith(("t1_", "t3_")) else value

    @staticmethod
    def _route_metadata(endpoint: str) -> dict[str, str]:
        return {
            "provider": "arctic_shift",
            "route": "arctic_shift_api",
            "endpoint": endpoint,
            "collected_at": datetime.now(UTC).isoformat(),
        }

    def _normalise_post(
        self, item: Mapping[str, Any], *, endpoint: str = "/api/posts/search"
    ) -> dict[str, Any]:
        post_id = self._bare_id(str(item.get("id", "")))
        subreddit = str(item.get("subreddit", ""))
        return {
            "kind": "post",
            "id": post_id,
            "source_post_id": post_id,
            "source_comment_ids": [],
            "subreddit": subreddit,
            "title": item.get("title") or "",
            "text": item.get("selftext") or "",
            "author": item.get("author") or "",
            "created_utc": item.get("created_utc"),
            "url": item.get("url") or f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/",
            "crosspost_parent": item.get("crosspost_parent") or "",
            "source": self._route_metadata(endpoint),
            "raw": dict(item),
        }

    def _normalise_comment(self, item: Mapping[str, Any]) -> dict[str, Any]:
        comment_id = self._bare_id(str(item.get("id", "")))
        post_id = self._bare_id(str(item.get("link_id", "")))
        parent_raw = str(item.get("parent_id", ""))
        parent_kind = "comment" if parent_raw.startswith("t1_") else "post"
        subreddit = str(item.get("subreddit", ""))
        return {
            "kind": "comment",
            "id": comment_id,
            "source_comment_id": comment_id,
            "source_post_id": post_id,
            "source_parent_id": self._bare_id(parent_raw),
            "parent_kind": parent_kind,
            "post_in_pilot": False,
            "subreddit": subreddit,
            "text": item.get("body") or "",
            "author": item.get("author") or "",
            "created_utc": item.get("created_utc"),
            "url": (
                f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/_/{comment_id}/"
                if post_id and comment_id
                else ""
            ),
            "source": self._route_metadata("/api/comments/search"),
            "raw": dict(item),
        }

    @staticmethod
    def _link_comments(posts: list[dict[str, Any]], comments: list[dict[str, Any]]) -> None:
        post_index = {str(post["source_post_id"]): post for post in posts}
        for comment in comments:
            post = post_index.get(str(comment["source_post_id"]))
            if post is None:
                continue
            post["source_comment_ids"].append(comment["source_comment_id"])
            comment["post_in_pilot"] = True
