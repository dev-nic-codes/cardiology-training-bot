from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.services.article_service import ArticlePostingService, STATE_LAST_ARTICLE_POSTED_AT
from bot.services.podcast_service import PodcastPostingService, STATE_LAST_PODCAST_POSTED_AT
from api.ecg_wave_maven import WaveMavenClient
from bot.config import BASE_DIR, Settings
from bot.database.session import create_engine, create_session_factory, init_database
from bot.handlers.commands import build_commands_router
from bot.services.ai_summary_service import AISummaryService
from bot.handlers.poll_answers import build_poll_router
from bot.services.cache_service import ECGCacheService
from bot.services.console_dashboard import ConsoleDashboard, DashboardEventBuffer
from bot.services.posting_service import ECGPostingService
from bot.services.scheduler import SchedulerService
from bot.services.state_store import StateStore
from bot.services.stats_service import STATE_LAST_ECG_POSTED_AT, StatsService
from bot.services.topic_manager import STATE_ARTICLES_TOPIC_ID, STATE_PODCAST_TOPIC_ID, TopicManager

LOGGER = logging.getLogger(__name__)


def startup_post_is_due(
    last_posted_at: datetime | None,
    interval_hours: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a startup post is due without bypassing its interval."""
    if last_posted_at is None:
        return True
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    previous = last_posted_at
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    return current.astimezone(UTC) >= previous.astimezone(UTC) + timedelta(hours=interval_hours)


def configure_logging(settings: Settings, event_buffer: DashboardEventBuffer) -> None:
    settings.ensure_runtime_directories()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.log_level)

    file_handler = RotatingFileHandler(
        settings.logs_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(event_buffer)


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Show the ECG bot overview"),
            BotCommand(command="menu", description="Open the admin control center"),
            BotCommand(command="id", description="Show and store this forum topic id"),
            BotCommand(command="next", description="Post the next unseen ECG immediately"),
            BotCommand(command="post_now", description="Post one ECG case immediately"),
            BotCommand(command="post_article_now", description="Post one article immediately"),
            BotCommand(command="post_podcast_now", description="Post one podcast episode immediately"),
            BotCommand(command="score", description="Show your ECG quiz score"),
            BotCommand(command="stats", description="Show overall ECG answer statistics"),
            BotCommand(command="wrong", description="Show recent incorrect answers"),
            BotCommand(command="status", description="Show cache and scheduler status"),
        ]
    )


async def main() -> None:
    settings = Settings()
    event_buffer = DashboardEventBuffer()
    configure_logging(settings, event_buffer)
    LOGGER.info("Starting Cardiology Training Bot.")
    LOGGER.info(
        "Configuration loaded. chat_id=%s ecg_topic_id=%s articles_topic_id=%s podcast_topic_id=%s owner_user_id=%s ecg_interval=%s article_interval=%s podcast_interval=%s database=%s",
        settings.chat_id,
        settings.ecg_topic_id,
        settings.articles_topic_id,
        settings.podcast_topic_id,
        settings.owner_user_id,
        settings.post_interval_hours,
        settings.article_post_interval_hours,
        settings.podcast_post_interval_hours,
        settings.database_url,
    )

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    LOGGER.info("Initializing database schema.")
    await init_database(engine)

    state_store = StateStore(session_factory)
    topic_manager = TopicManager(state_store, settings.ecg_topic_id)
    article_topic_manager = TopicManager(
        state_store,
        settings.articles_topic_id,
        state_key=STATE_ARTICLES_TOPIC_ID,
    )
    podcast_topic_manager = TopicManager(
        state_store,
        settings.podcast_topic_id,
        state_key=STATE_PODCAST_TOPIC_ID,
    )
    wave_maven_client = WaveMavenClient(
        base_url=settings.wave_maven_base_url,
        user_agent=settings.user_agent,
        request_delay_seconds=settings.scrape_delay_seconds,
        timeout_seconds=settings.scrape_timeout_seconds,
        bootstrap_training_code=settings.scrape_bootstrap_training_code,
        bootstrap_goal_code=settings.scrape_bootstrap_goal_code,
    )
    cache_service = ECGCacheService(
        session_factory,
        wave_maven_client,
        state_store,
        refill_threshold=settings.cache_refill_threshold,
        batch_size=settings.cache_batch_size,
    )
    posting_service = ECGPostingService(
        session_factory,
        cache_service,
        topic_manager,
        state_store,
        chat_id=settings.chat_id,
        notify_user_id=settings.owner_user_id,
    )
    article_posting_service = ArticlePostingService(
        session_factory,
        state_store,
        article_topic_manager,
        chat_id=settings.chat_id,
        user_agent=settings.user_agent,
        timeout_seconds=settings.scrape_timeout_seconds,
        legacy_db_path=BASE_DIR / "data" / "bot_state.sqlite3",
        ai_summary_service=AISummaryService(
            enabled=settings.ai_summary_enabled,
            api_key=settings.openai_api_key,
            model=settings.ai_summary_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.scrape_timeout_seconds,
            max_input_chars=settings.ai_summary_max_input_chars,
        ),
    )
    podcast_posting_service = PodcastPostingService(
        session_factory,
        state_store,
        podcast_topic_manager,
        chat_id=settings.chat_id,
        user_agent=settings.user_agent,
        timeout_seconds=settings.scrape_timeout_seconds,
        legacy_db_path=BASE_DIR / "data" / "bot_state.sqlite3",
    )
    stats_service = StatsService(session_factory, state_store)
    scheduler_service = SchedulerService(
        posting_service,
        article_posting_service,
        podcast_posting_service,
        interval_hours=settings.post_interval_hours,
        article_interval_hours=settings.article_post_interval_hours,
        podcast_interval_hours=settings.podcast_post_interval_hours,
        timezone=settings.timezone,
    )
    dashboard = ConsoleDashboard(
        settings=settings,
        stats_service=stats_service,
        scheduler_service=scheduler_service,
        topic_manager=topic_manager,
        article_topic_manager=article_topic_manager,
        podcast_topic_manager=podcast_topic_manager,
        event_buffer=event_buffer,
    )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_commands_router(
            settings,
            posting_service,
            article_posting_service,
            podcast_posting_service,
            stats_service,
            scheduler_service,
            cache_service,
            topic_manager,
            article_topic_manager,
            podcast_topic_manager,
        )
    )
    dispatcher.include_router(build_poll_router(settings, posting_service))
    await dashboard.start()

    async def on_startup() -> None:
        configured_topic_id = await topic_manager.get_topic_id()
        configured_articles_topic_id = await article_topic_manager.get_topic_id()
        configured_podcast_topic_id = await podcast_topic_manager.get_topic_id()
        LOGGER.info(
            "Startup: active ECG topic_id=%s active articles topic_id=%s active podcast topic_id=%s",
            configured_topic_id,
            configured_articles_topic_id,
            configured_podcast_topic_id,
        )
        LOGGER.info("Startup: registering bot commands.")
        await set_bot_commands(bot)
        LOGGER.info("Startup: warming ECG cache from Wave-Maven.")
        try:
            cache_report = await cache_service.fill_cache_if_needed(force=True)
            LOGGER.info(
                "Startup cache result: requested=%s stored=%s skipped=%s",
                cache_report.requested,
                cache_report.stored,
                cache_report.skipped,
            )
        except Exception as exc:
            LOGGER.warning("Startup cache refresh failed. Continuing with existing cached ECG cases. error=%s", exc)
        last_ecg_posted_at = await state_store.get_datetime(STATE_LAST_ECG_POSTED_AT)
        last_article_posted_at = await state_store.get_datetime(STATE_LAST_ARTICLE_POSTED_AT)
        last_podcast_posted_at = await state_store.get_datetime(STATE_LAST_PODCAST_POSTED_AT)

        if startup_post_is_due(last_ecg_posted_at, settings.post_interval_hours):
            LOGGER.info("Startup: ECG interval is due; posting one case.")
            try:
                ecg_result = await posting_service.post_next_case(bot)
                LOGGER.info(
                    "Startup ECG posted successfully. case_id=%s poll_id=%s",
                    ecg_result.case_id,
                    ecg_result.poll_id,
                )
            except Exception:
                LOGGER.exception("Startup ECG posting failed.")
        else:
            LOGGER.info("Startup ECG post skipped because its interval is not due.")

        if startup_post_is_due(last_article_posted_at, settings.article_post_interval_hours):
            LOGGER.info("Startup: article interval is due; posting one article.")
            try:
                article_result = await article_posting_service.post_next_article(bot)
                if article_result is None:
                    LOGGER.info("Startup article post skipped because no unseen article is available.")
                else:
                    LOGGER.info(
                        "Startup article posted successfully. unique_id=%s pdf_attached=%s",
                        article_result.unique_id,
                        article_result.pdf_attached,
                    )
            except Exception:
                LOGGER.exception("Startup article posting failed.")
        else:
            LOGGER.info("Startup article post skipped because its interval is not due.")

        if startup_post_is_due(last_podcast_posted_at, settings.podcast_post_interval_hours):
            LOGGER.info("Startup: podcast interval is due; posting one episode.")
            try:
                podcast_result = await podcast_posting_service.post_next_podcast(bot)
                if podcast_result is None:
                    LOGGER.info("Startup podcast post skipped because no unseen episode is available.")
                else:
                    LOGGER.info(
                        "Startup podcast posted successfully. unique_id=%s",
                        podcast_result.unique_id,
                    )
            except Exception:
                LOGGER.exception("Startup podcast posting failed.")
        else:
            LOGGER.info("Startup podcast post skipped because its interval is not due.")

        last_ecg_posted_at = await state_store.get_datetime(STATE_LAST_ECG_POSTED_AT)
        last_article_posted_at = await state_store.get_datetime(STATE_LAST_ARTICLE_POSTED_AT)
        last_podcast_posted_at = await state_store.get_datetime(STATE_LAST_PODCAST_POSTED_AT)
        LOGGER.info(
            "Startup: last_ecg_posted_at=%s last_article_posted_at=%s last_podcast_posted_at=%s",
            last_ecg_posted_at,
            last_article_posted_at,
            last_podcast_posted_at,
        )
        scheduler_service.start(
            bot,
            last_ecg_posted_at=last_ecg_posted_at,
            last_article_posted_at=last_article_posted_at,
            last_podcast_posted_at=last_podcast_posted_at,
        )
        LOGGER.info("Startup complete. Bot is now polling Telegram updates.")

    async def on_shutdown() -> None:
        LOGGER.info("Shutting down ECG bot.")
        await scheduler_service.shutdown()
        await article_posting_service.close()
        await podcast_posting_service.close()
        await wave_maven_client.close()
        await bot.session.close()
        LOGGER.info("ECG bot shutdown complete.")
        await dashboard.stop()
        await engine.dispose()

    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
