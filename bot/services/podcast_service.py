from __future__ import annotations

import html
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database.models import PostedPodcast
from bot.services.state_store import StateStore
from bot.services.topic_manager import TopicManager

LOGGER = logging.getLogger(__name__)

STATE_LAST_PODCAST_POSTED_AT = "last_podcast_posted_at"
DEFAULT_PODCAST_FEED_URL = "https://twicardiology.libsyn.com/rss"


@dataclass(slots=True)
class PodcastEpisode:
    unique_id: str
    title: str
    page_url: str
    audio_url: str
    published_at: datetime | None


@dataclass(slots=True)
class PodcastPostResult:
    unique_id: str
    title: str
    url: str
    audio_url: str


class PodcastPostingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        state_store: StateStore,
        topic_manager: TopicManager,
        *,
        chat_id: int,
        user_agent: str,
        timeout_seconds: float,
        legacy_db_path: Path,
        feed_url: str = DEFAULT_PODCAST_FEED_URL,
    ) -> None:
        self.session_factory = session_factory
        self.state_store = state_store
        self.topic_manager = topic_manager
        self.chat_id = chat_id
        self.legacy_db_path = legacy_db_path
        self.feed_url = feed_url
        self._legacy_ids: set[str] | None = None
        self._legacy_latest_published_at: datetime | None = None
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_seconds,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def post_next_podcast(self, bot: Bot) -> PodcastPostResult | None:
        LOGGER.info("Fetching podcast feed.")
        current_ids = await self._load_current_posted_ids()
        legacy_ids = self._load_legacy_posted_ids()
        current_latest_published_at = await self._load_current_latest_published_at()
        legacy_latest_published_at = self._load_legacy_latest_published_at()
        watermark_candidates = [value for value in [current_latest_published_at, legacy_latest_published_at] if value is not None]
        watermark = max(watermark_candidates) if watermark_candidates else None
        episodes = await self._fetch_feed()
        if watermark is None:
            candidate_pool = list(reversed(episodes))
        else:
            candidate_pool = [
                episode
                for episode in episodes
                if episode.published_at is not None and episode.published_at > watermark
            ]
        next_episode = next(
            (
                episode
                for episode in candidate_pool
                if episode.unique_id not in current_ids
                and episode.unique_id not in legacy_ids
                and episode.page_url not in current_ids
                and episode.page_url not in legacy_ids
                and episode.title not in legacy_ids
            ),
            None,
        )
        if next_episode is None:
            LOGGER.info("No unseen podcast episode is available.")
            return None

        topic_id = await self.topic_manager.get_topic_id()
        LOGGER.info('Found podcast "%s". Posting in podcast topic.', next_episode.title)
        audio_file = await self._download_audio(next_episode)
        message = await bot.send_audio(
            chat_id=self.chat_id,
            message_thread_id=topic_id,
            audio=audio_file,
            caption=self._build_caption(next_episode),
            parse_mode="HTML",
            title=next_episode.title,
        )

        async with self.session_factory() as session:
            session.add(
                PostedPodcast(
                    unique_id=next_episode.unique_id,
                    title=next_episode.title,
                    url=next_episode.page_url,
                    audio_url=next_episode.audio_url,
                    published_at=next_episode.published_at,
                    chat_id=self.chat_id,
                    topic_id=topic_id,
                    telegram_message_id=message.message_id,
                )
            )
            await session.commit()

        await self.state_store.set_datetime(STATE_LAST_PODCAST_POSTED_AT, datetime.now(UTC))
        LOGGER.info('Posted podcast episode: %s', next_episode.title)
        return PodcastPostResult(
            unique_id=next_episode.unique_id,
            title=next_episode.title,
            url=next_episode.page_url,
            audio_url=next_episode.audio_url,
        )

    async def count_posted_podcasts(self) -> int:
        async with self.session_factory() as session:
            return len((await session.scalars(select(PostedPodcast.id))).all())

    async def _load_current_posted_ids(self) -> set[str]:
        async with self.session_factory() as session:
            rows = await session.execute(select(PostedPodcast.unique_id, PostedPodcast.url, PostedPodcast.title))
            values: set[str] = set()
            for unique_id, url, title in rows.all():
                for value in (unique_id, url, title):
                    if value:
                        values.add(value)
            return values

    async def _load_current_latest_published_at(self) -> datetime | None:
        async with self.session_factory() as session:
            rows = await session.scalars(select(PostedPodcast.published_at).order_by(PostedPodcast.published_at.desc()))
            for value in rows.all():
                if value is not None:
                    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return None

    def _load_legacy_posted_ids(self) -> set[str]:
        if self._legacy_ids is not None:
            return self._legacy_ids

        legacy_ids: set[str] = set()
        if self.legacy_db_path.exists():
            try:
                connection = sqlite3.connect(self.legacy_db_path)
                cursor = connection.cursor()
                for row in cursor.execute(
                    "select unique_id, title, url from posted_items where item_type = 'podcast'"
                ):
                    for value in row:
                        if value:
                            legacy_ids.add(str(value))
                connection.close()
            except sqlite3.Error as exc:  # pragma: no cover - legacy db failure path
                LOGGER.warning("Failed to read legacy podcast history from %s: %s", self.legacy_db_path, exc)

        self._legacy_ids = legacy_ids
        LOGGER.info("Loaded %s legacy podcast ids for dedupe.", len(self._legacy_ids))
        return self._legacy_ids

    def _load_legacy_latest_published_at(self) -> datetime | None:
        if self._legacy_latest_published_at is not None:
            return self._legacy_latest_published_at

        latest: datetime | None = None
        if self.legacy_db_path.exists():
            try:
                connection = sqlite3.connect(self.legacy_db_path)
                cursor = connection.cursor()
                row = cursor.execute(
                    "select published_at from posted_items where item_type = 'podcast' and published_at is not null order by published_at desc limit 1"
                ).fetchone()
                connection.close()
                if row and row[0]:
                    latest = self._parse_legacy_datetime(str(row[0]))
            except sqlite3.Error as exc:  # pragma: no cover - legacy db failure path
                LOGGER.warning("Failed to read legacy podcast watermark from %s: %s", self.legacy_db_path, exc)

        self._legacy_latest_published_at = latest
        return self._legacy_latest_published_at

    async def _fetch_feed(self) -> list[PodcastEpisode]:
        response = await self._client.get(self.feed_url)
        response.raise_for_status()
        parsed = feedparser.parse(response.text)
        episodes: list[PodcastEpisode] = []
        for entry in list(parsed.entries or []):
            title = str(entry.get("title") or "").strip()
            page_url = str(entry.get("link") or "").strip()
            audio_url = self._extract_audio_url(entry)
            if not title or not page_url or not audio_url:
                continue
            published_at = self._parse_published_at(entry)
            unique_id = str(entry.get("id") or page_url or audio_url).strip()
            episodes.append(
                PodcastEpisode(
                    unique_id=unique_id,
                    title=title,
                    page_url=page_url,
                    audio_url=audio_url,
                    published_at=published_at,
                )
            )
        episodes.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC))
        return episodes

    async def _download_audio(self, episode: PodcastEpisode) -> BufferedInputFile:
        LOGGER.info("Downloading podcast audio from %s", episode.audio_url)
        response = await self._client.get(
            episode.audio_url,
            headers={
                **self._client.headers,
                "Accept": "audio/mpeg,audio/*;q=0.9,*/*;q=0.8",
                "Referer": episode.page_url,
            },
        )
        response.raise_for_status()
        filename = self._build_audio_filename(episode)
        return BufferedInputFile(response.content, filename=filename)

    def _extract_audio_url(self, entry: dict) -> str:
        enclosures = list(entry.get("enclosures") or [])
        for enclosure in enclosures:
            href = str((enclosure or {}).get("href") or "").strip()
            if href:
                return href
        links = list(entry.get("links") or [])
        for link in links:
            href = str((link or {}).get("href") or "").strip()
            link_type = str((link or {}).get("type") or "").strip().lower()
            if href and link_type.startswith("audio/"):
                return href
        return ""

    def _parse_published_at(self, entry: dict) -> datetime | None:
        text = str(entry.get("published") or entry.get("updated") or "").strip()
        if not text:
            return None
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _build_caption(self, episode: PodcastEpisode) -> str:
        lines = [
            f"🎙️ <b>{html.escape(episode.title)}</b>",
            "",
            "🫀 <b>Series:</b> This Week in Cardiology",
            f"🔗 <a href=\"{html.escape(episode.page_url, quote=True)}\">Open episode page</a>",
        ]
        if episode.published_at is not None:
            lines.insert(2, f"📅 <b>Published:</b> {html.escape(episode.published_at.strftime('%Y-%m-%d'))}")
        return "\n".join(lines)

    def _build_audio_filename(self, episode: PodcastEpisode) -> str:
        parsed = urlparse(episode.audio_url)
        name = Path(parsed.path).name.strip() or ""
        if not name.lower().endswith(".mp3"):
            safe_title = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in episode.title).strip("_")
            name = f"{safe_title or 'podcast_episode'}.mp3"
        return name

    def _parse_legacy_datetime(self, value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
