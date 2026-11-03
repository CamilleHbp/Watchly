import asyncio
from datetime import datetime
from typing import Any

from loguru import logger

from app.core.base_client import BaseClient
from app.core.config import settings
from app.models.history import WatchHistory, WatchHistoryItem

# The export endpoint's maximum page size.
EXPORT_PAGE_LIMIT = 1000
# Backstop against a pagination contract change turning the loop endless.
MAX_EXPORT_PAGES = 100


class CrossWatchService:
    """A self-hosted CrossWatch's saved history, read through its JSON export.

    Only available when the server sets CROSSWATCH_URL: users supply a token, never
    an address, so a shared instance can't be pointed at arbitrary hosts.
    """

    def __init__(self):
        self.client = BaseClient(base_url=settings.CROSSWATCH_URL or "", timeout=30.0, max_retries=3)

    @staticmethod
    def enabled() -> bool:
        return bool(settings.CROSSWATCH_URL)

    async def close(self) -> None:
        await self.client.close()

    async def _get_page(self, api_token: str, provider: str, feature: str, offset: int, limit: int) -> dict[str, Any]:
        return await self.client.get(
            "/api/export/items",
            params={"provider": provider, "feature": feature, "offset": offset, "limit": limit},
            headers={"Authorization": f"Bearer {api_token}"},
        )

    async def count_items(self, api_token: str, provider: str) -> int:
        return int((await self._get_page(api_token, provider, "history", 0, 1)).get("total") or 0)

    async def _get_all_items(self, api_token: str, provider: str, feature: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for _ in range(MAX_EXPORT_PAGES):
            data = await self._get_page(api_token, provider, feature, len(items), EXPORT_PAGE_LIMIT)
            page = data.get("items") or []
            items.extend(page)
            if not page or len(items) >= int(data.get("total") or 0):
                return items

        logger.warning(f"CrossWatch {feature}: stopped at the {MAX_EXPORT_PAGES}-page cap")
        return items

    async def get_history(self, api_token: str, provider: str) -> WatchHistory:
        # A failed request raises rather than degrading to an empty list: an empty
        # history looks like a user who has watched nothing, and the callers cache it.
        watched, rated = await asyncio.gather(
            self._get_all_items(api_token, provider, "history"),
            self._get_all_items(api_token, provider, "ratings"),
        )

        ratings: dict[str, float] = {}
        for entry in rated:
            imdb_id, _mtype = self._title(entry)
            if imdb_id and entry.get("rating"):
                ratings[imdb_id] = float(entry["rating"])

        # History is per episode for shows; fold episodes into one series entry.
        titles: dict[str, WatchHistoryItem] = {}
        for entry in watched:
            imdb_id, mtype = self._title(entry)
            if not imdb_id:
                continue
            last_watched = self._parse_date(entry.get("watched_at"))
            item = titles.get(imdb_id)
            if item is None:
                titles[imdb_id] = WatchHistoryItem(
                    imdb_id=imdb_id,
                    type=mtype,
                    name=entry.get("series_title") or entry.get("title") or "",
                    rating=ratings.get(imdb_id),
                    watch_count=1,
                    completion=1.0,
                    last_watched=last_watched,
                    source="crosswatch",
                )
            elif last_watched and (item.last_watched is None or last_watched > item.last_watched):
                item.last_watched = last_watched

        # Rated but never marked watched still says something about taste.
        for entry in rated:
            imdb_id, mtype = self._title(entry)
            if not imdb_id or imdb_id in titles or not entry.get("rating"):
                continue
            titles[imdb_id] = WatchHistoryItem(
                imdb_id=imdb_id,
                type=mtype,
                name=entry.get("series_title") or entry.get("title") or "",
                rating=float(entry["rating"]),
                watch_count=0,
                completion=0.0,
                last_watched=self._parse_date(entry.get("rated_at")),
                source="crosswatch",
            )

        logger.info(f"CrossWatch history: {len(titles)} items ({len(ratings)} rated)")
        return WatchHistory(items=list(titles.values()), source="crosswatch")

    @staticmethod
    def _title(entry: dict[str, Any]) -> tuple[str | None, str]:
        """IMDb id and Stremio type of the movie or series an export row belongs to."""
        if entry.get("type") == "movie":
            return (entry.get("ids") or {}).get("imdb"), "movie"
        if entry.get("type") in ("season", "episode"):
            return (entry.get("show_ids") or {}).get("imdb"), "series"
        return (entry.get("ids") or {}).get("imdb"), "series"

    @staticmethod
    def _parse_date(date_str: str | None) -> datetime | None:
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        except ValueError:
            return None


crosswatch_service = CrossWatchService()
