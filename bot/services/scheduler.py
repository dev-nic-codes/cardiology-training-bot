from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.services.article_service import ArticlePostingService
from bot.services.podcast_service import PodcastPostingService
from bot.services.posting_service import ECGPostingService

LOGGER = logging.getLogger(__name__)


class SchedulerService:
    def __init__(
        self,
        posting_service: ECGPostingService,
        article_posting_service: ArticlePostingService,
        podcast_posting_service: PodcastPostingService,
        *,
        interval_hours: int,
        article_interval_hours: int,
        podcast_interval_hours: int,
        timezone: str,
    ) -> None:
        self.posting_service = posting_service
        self.article_posting_service = article_posting_service
        self.podcast_posting_service = podcast_posting_service
        self.interval_hours = interval_hours
        self.article_interval_hours = article_interval_hours
        self.podcast_interval_hours = podcast_interval_hours
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(timezone))
        self._bot: Bot | None = None
        self._ecg_paused = False
        self._article_paused = False
        self._podcast_paused = False

    def start(
        self,
        bot: Bot,
        *,
        last_ecg_posted_at: datetime | None = None,
        last_article_posted_at: datetime | None = None,
        last_podcast_posted_at: datetime | None = None,
    ) -> None:
        if self.scheduler.running:
            return

        self._bot = bot
        self._ecg_paused = False
        self._article_paused = False
        self._podcast_paused = False
        ecg_next_run = self._resolve_next_run_time(last_ecg_posted_at, self.interval_hours)
        article_next_run = self._resolve_next_run_time(last_article_posted_at, self.article_interval_hours)
        podcast_next_run = self._resolve_next_run_time(last_podcast_posted_at, self.podcast_interval_hours)
        self.scheduler.add_job(
            self.run_scheduled_post,
            trigger="interval",
            hours=self.interval_hours,
            id="ecg_post_cycle",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            next_run_time=ecg_next_run,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.run_scheduled_article_post,
            trigger="interval",
            hours=self.article_interval_hours,
            id="article_post_cycle",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            next_run_time=article_next_run,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.run_scheduled_podcast_post,
            trigger="interval",
            hours=self.podcast_interval_hours,
            id="podcast_post_cycle",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            next_run_time=podcast_next_run,
            replace_existing=True,
        )
        self.scheduler.start()
        LOGGER.info(
            "Scheduler started. ECG interval=%s hours next_ecg_run=%s article interval=%s hours next_article_run=%s podcast interval=%s hours next_podcast_run=%s",
            self.interval_hours,
            self.next_ecg_run_time(),
            self.article_interval_hours,
            self.next_article_run_time(),
            self.podcast_interval_hours,
            self.next_podcast_run_time(),
        )

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self._ecg_paused = False
            self._article_paused = False
            self._podcast_paused = False
            LOGGER.info("Scheduler stopped.")

    async def run_scheduled_post(self) -> None:
        if self._bot is None:
            return
        LOGGER.info("Running scheduled ECG posting cycle.")
        try:
            await self.posting_service.post_next_case(self._bot)
        except Exception:  # pragma: no cover - scheduler exception logging
            LOGGER.exception("Scheduled ECG posting failed.")

    async def run_scheduled_article_post(self) -> None:
        if self._bot is None:
            return
        LOGGER.info("Running scheduled article posting cycle.")
        try:
            await self.article_posting_service.post_next_article(self._bot)
        except Exception:  # pragma: no cover - scheduler exception logging
            LOGGER.exception("Scheduled article posting failed.")

    async def run_scheduled_podcast_post(self) -> None:
        if self._bot is None:
            return
        LOGGER.info("Running scheduled podcast posting cycle.")
        try:
            await self.podcast_posting_service.post_next_podcast(self._bot)
        except Exception:  # pragma: no cover - scheduler exception logging
            LOGGER.exception("Scheduled podcast posting failed.")

    def next_ecg_run_time(self) -> datetime | None:
        job = self.scheduler.get_job("ecg_post_cycle")
        return job.next_run_time if job else None

    def next_article_run_time(self) -> datetime | None:
        job = self.scheduler.get_job("article_post_cycle")
        return job.next_run_time if job else None

    def next_podcast_run_time(self) -> datetime | None:
        job = self.scheduler.get_job("podcast_post_cycle")
        return job.next_run_time if job else None

    def pause_ecg(self) -> bool:
        if not self.scheduler.running or self._ecg_paused:
            return False
        self.scheduler.pause_job("ecg_post_cycle")
        self._ecg_paused = True
        LOGGER.info("Paused ECG scheduler.")
        return True

    def resume_ecg(self) -> bool:
        if not self.scheduler.running or not self._ecg_paused:
            return False
        self.scheduler.resume_job("ecg_post_cycle")
        self._ecg_paused = False
        LOGGER.info("Resumed ECG scheduler.")
        return True

    def pause_articles(self) -> bool:
        if not self.scheduler.running or self._article_paused:
            return False
        self.scheduler.pause_job("article_post_cycle")
        self._article_paused = True
        LOGGER.info("Paused article scheduler.")
        return True

    def resume_articles(self) -> bool:
        if not self.scheduler.running or not self._article_paused:
            return False
        self.scheduler.resume_job("article_post_cycle")
        self._article_paused = False
        LOGGER.info("Resumed article scheduler.")
        return True

    def pause_podcasts(self) -> bool:
        if not self.scheduler.running or self._podcast_paused:
            return False
        self.scheduler.pause_job("podcast_post_cycle")
        self._podcast_paused = True
        LOGGER.info("Paused podcast scheduler.")
        return True

    def resume_podcasts(self) -> bool:
        if not self.scheduler.running or not self._podcast_paused:
            return False
        self.scheduler.resume_job("podcast_post_cycle")
        self._podcast_paused = False
        LOGGER.info("Resumed podcast scheduler.")
        return True

    def is_ecg_paused(self) -> bool:
        return self._ecg_paused

    def is_article_paused(self) -> bool:
        return self._article_paused

    def is_podcast_paused(self) -> bool:
        return self._podcast_paused

    def update_ecg_interval(self, hours: int) -> datetime | None:
        if hours <= 0:
            raise ValueError("ECG interval must be greater than zero.")
        self.interval_hours = hours
        if self.scheduler.running:
            next_run = datetime.now(self.scheduler.timezone) + timedelta(hours=hours)
            self.scheduler.reschedule_job(
                "ecg_post_cycle",
                trigger="interval",
                hours=hours,
                start_date=next_run,
            )
            LOGGER.info("Updated ECG scheduler interval to %s hour(s). next_run=%s", hours, next_run)
            return next_run
        return None

    def update_article_interval(self, hours: int) -> datetime | None:
        if hours <= 0:
            raise ValueError("Article interval must be greater than zero.")
        self.article_interval_hours = hours
        if self.scheduler.running:
            next_run = datetime.now(self.scheduler.timezone) + timedelta(hours=hours)
            self.scheduler.reschedule_job(
                "article_post_cycle",
                trigger="interval",
                hours=hours,
                start_date=next_run,
            )
            LOGGER.info("Updated article scheduler interval to %s hour(s). next_run=%s", hours, next_run)
            return next_run
        return None

    def update_podcast_interval(self, hours: int) -> datetime | None:
        if hours <= 0:
            raise ValueError("Podcast interval must be greater than zero.")
        self.podcast_interval_hours = hours
        if self.scheduler.running:
            next_run = datetime.now(self.scheduler.timezone) + timedelta(hours=hours)
            self.scheduler.reschedule_job(
                "podcast_post_cycle",
                trigger="interval",
                hours=hours,
                start_date=next_run,
            )
            LOGGER.info("Updated podcast scheduler interval to %s hour(s). next_run=%s", hours, next_run)
            return next_run
        return None

    def _resolve_next_run_time(self, last_posted_at: datetime | None, interval_hours: int) -> datetime:
        now = datetime.now(self.scheduler.timezone)
        if last_posted_at is None:
            return now

        due_at = last_posted_at.astimezone(self.scheduler.timezone) + timedelta(hours=interval_hours)
        if due_at <= now:
            return now
        return due_at
