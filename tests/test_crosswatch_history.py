import asyncio

import httpx
import pytest

from app.services.crosswatch import CrossWatchService

GOOD_TOKEN = "cwt_0123456789abcdef.secret-secret-secret-secret-secret"

HISTORY = [
    {
        "key": "movie:tmdb:949",
        "type": "movie",
        "title": "Heat",
        "year": "1995",
        "ids": {"imdb": "tt0113277", "tmdb": "949"},
        "watched_at": "2026-01-05T22:27:00Z",
    },
    {"key": "movie:tmdb:1", "type": "movie", "title": "No imdb id", "ids": {"tmdb": "1"}, "watched_at": ""},
    {
        "key": "episode:1",
        "type": "episode",
        "title": "The Target",
        "series_title": "The Wire",
        "ids": {"tmdb": "10"},
        "show_ids": {"imdb": "tt0306414"},
        "season": 1,
        "episode": 1,
        "watched_at": "2025-10-15T20:00:00Z",
    },
    {
        "key": "episode:2",
        "type": "episode",
        "title": "The Detail",
        "series_title": "The Wire",
        "ids": {"tmdb": "11"},
        "show_ids": {"imdb": "tt0306414"},
        "season": 1,
        "episode": 2,
        "watched_at": "2025-10-16T20:00:00Z",
    },
]
RATINGS = [
    {
        "key": "movie:tmdb:949",
        "type": "movie",
        "title": "Heat",
        "ids": {"imdb": "tt0113277"},
        "rating": 9,
        "rated_at": "2026-01-06T00:00:00Z",
    },
    {
        "key": "show:7",
        "type": "show",
        "title": "Rated",
        "ids": {"imdb": "tt0000001"},
        "rating": 7,
        "rated_at": "2025-12-01T00:00:00Z",
    },
]


class FakeClient:
    """Serves the export in pages of `page_size`; any other token is a 401 like CrossWatch."""

    def __init__(self, status_for_good_token: int | None = None, page_size: int = 3):
        self.status_for_good_token = status_for_good_token
        self.page_size = page_size
        self.calls: list[dict] = []

    async def get(self, url: str, params=None, headers=None, **kwargs):
        self.calls.append(dict(params))
        if headers.get("Authorization") != f"Bearer {GOOD_TOKEN}":
            self._raise(url, 401)
        if self.status_for_good_token:
            self._raise(url, self.status_for_good_token)
        rows = HISTORY if params["feature"] == "history" else RATINGS
        offset = params["offset"]
        limit = min(params["limit"], self.page_size)
        return {"items": rows[offset : offset + limit], "total": len(rows), "offset": offset, "limit": limit}

    @staticmethod
    def _raise(url: str, status: int):
        request = httpx.Request("GET", f"http://crosswatch:8787{url}")
        raise httpx.HTTPStatusError("rejected", request=request, response=httpx.Response(status, request=request))


def service_with(client: FakeClient) -> CrossWatchService:
    service = CrossWatchService()
    service.client = client
    return service


def test_history_pages_folds_episodes_and_merges_ratings():
    client = FakeClient()
    history = asyncio.run(service_with(client).get_history(GOOD_TOKEN, "TRAKT"))

    by_id = {item.imdb_id: item for item in history.items}
    assert history.source == "crosswatch"
    assert set(by_id) == {"tt0113277", "tt0306414", "tt0000001"}

    heat = by_id["tt0113277"]
    assert heat.type == "movie" and heat.rating == 9.0 and heat.watch_count == 1
    assert heat.last_watched is not None and heat.last_watched.year == 2026

    wire = by_id["tt0306414"]
    assert wire.type == "series" and wire.name == "The Wire" and wire.rating is None
    assert wire.last_watched is not None and wire.last_watched.day == 16

    rated_only = by_id["tt0000001"]
    assert rated_only.type == "series" and rated_only.rating == 7.0
    assert rated_only.watch_count == 0 and rated_only.completion == 0.0

    history_offsets = [params["offset"] for params in client.calls if params["feature"] == "history"]
    assert history_offsets == [0, 3]
    assert {params["provider"] for params in client.calls} == {"TRAKT"}


def test_a_rejected_token_raises_instead_of_an_empty_history():
    with pytest.raises(httpx.HTTPStatusError) as exc:
        asyncio.run(service_with(FakeClient()).get_history("cwt_stale", "TRAKT"))
    assert exc.value.response.status_code == 401


def test_an_outage_raises_instead_of_an_empty_history():
    with pytest.raises(httpx.HTTPStatusError) as exc:
        asyncio.run(service_with(FakeClient(status_for_good_token=503)).get_history(GOOD_TOKEN, "TRAKT"))
    assert exc.value.response.status_code == 503


def settings_with_crosswatch_token(api_token: str):
    from app.core.settings import get_default_settings

    user_settings = get_default_settings()
    user_settings.watch_history_source = "crosswatch"
    user_settings.crosswatch_api_token = api_token
    user_settings.crosswatch_provider = "TRAKT"
    return user_settings


def rejecting_history(status: int):
    async def get_history(api_token: str, provider: str):
        FakeClient._raise("/api/export/items", status)

    return get_history


@pytest.mark.parametrize(("status", "revoked_expected"), [(401, True), (503, False)])
def test_only_a_rejected_token_is_cleared(monkeypatch, status, revoked_expected):
    from app.services.profile.service import ProfileService

    cleared: list[tuple[str, str]] = []

    async def record(self, token, source):
        cleared.append((token, source))

    monkeypatch.setattr(ProfileService, "_clear_revoked_token", record)
    monkeypatch.setattr("app.services.crosswatch.settings.CROSSWATCH_URL", "http://crosswatch:8787")
    monkeypatch.setattr("app.services.profile.service.crosswatch_service.get_history", rejecting_history(status))

    history, missing, revoked = asyncio.run(
        ProfileService().fetch_external_watch_history(
            "crosswatch", settings_with_crosswatch_token(GOOD_TOKEN), "tok_crosswatch"
        )
    )

    assert history is None and not missing
    assert revoked is revoked_expected
    assert cleared == ([("tok_crosswatch", "crosswatch")] if revoked_expected else [])


def test_an_unconfigured_server_treats_the_token_as_missing(monkeypatch):
    from app.services.profile.service import ProfileService

    monkeypatch.setattr("app.services.crosswatch.settings.CROSSWATCH_URL", None)
    history, missing, revoked = asyncio.run(
        ProfileService().fetch_external_watch_history(
            "crosswatch", settings_with_crosswatch_token(GOOD_TOKEN), "tok_crosswatch"
        )
    )
    assert history is None and missing and not revoked
