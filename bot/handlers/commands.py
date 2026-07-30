from __future__ import annotations

import html
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import BASE_DIR, Settings
from bot.services.article_service import ArticlePostingService
from bot.services.cache_service import ECGCacheService
from bot.services.podcast_service import PodcastPostingService
from bot.services.posting_service import ECGPostingService
from bot.services.scheduler import SchedulerService
from bot.services.stats_service import StatsService
from bot.services.topic_manager import TopicManager

LOGGER = logging.getLogger(__name__)
DIFFICULTY_PATTERN = re.compile(r"(?:\*\s*){1,5}Difficulty rating\s*$", re.IGNORECASE)
USER_DISPLAY_NAMES: dict[int, str] = {
    386839171: "Nikolai",
    5404700759: "Zhoro",
}


def build_commands_router(
    settings: Settings,
    posting_service: ECGPostingService,
    article_posting_service: ArticlePostingService,
    podcast_posting_service: PodcastPostingService,
    stats_service: StatsService,
    scheduler_service: SchedulerService,
    cache_service: ECGCacheService,
    topic_manager: TopicManager,
    article_topic_manager: TopicManager,
    podcast_topic_manager: TopicManager,
) -> Router:
    router = Router(name="commands")
    display_zone = ZoneInfo(settings.timezone)
    pending_inputs: dict[int, str] = {}

    def is_owner(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id == settings.owner_user_id)

    def is_owner_user(user_id: int | None) -> bool:
        return user_id == settings.owner_user_id

    async def ensure_owner(message: Message) -> bool:
        if is_owner(message):
            return True
        await message.answer("🚫 This command is restricted to the configured owner.")
        return False

    async def sync_topic_from_message(message: Message) -> bool:
        thread_id = message.message_thread_id
        if thread_id is None:
            return False
        if message.chat.id != settings.chat_id:
            return False

        current_topic_id = await topic_manager.get_topic_id()
        if current_topic_id == thread_id:
            return False

        await topic_manager.set_topic_id(thread_id)
        LOGGER.info(
            "Updated ECG topic binding from chat message. chat_id=%s topic_id=%s",
            message.chat.id,
            thread_id,
        )
        return True

    def format_dt(value: datetime | None) -> str:
        if value is None:
            return "Not available"
        localized = value.astimezone(display_zone)
        return localized.strftime("%Y-%m-%d %H:%M:%S %Z")

    def clean_vignette(value: str) -> str:
        cleaned = DIFFICULTY_PATTERN.sub("", value).replace("\xa0", " ").replace("\ufffd", '"')
        return " ".join(cleaned.split()).strip()

    def format_user_label(user_id: int) -> str:
        display_name = USER_DISPLAY_NAMES.get(user_id)
        if display_name:
            return f"{html.escape(display_name)} <code>({user_id})</code>"
        return f"User <code>{user_id}</code>"

    def env_path() -> Path:
        return BASE_DIR / ".env"

    def update_env_setting(key: str, value: object) -> None:
        path = env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = f"{key}={value}"
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
        else:
            lines = []
        replaced = False
        updated: list[str] = []
        for line in lines:
            if line.startswith(f"{key}="):
                updated.append(rendered)
                replaced = True
            else:
                updated.append(line)
        if not replaced:
            updated.append(rendered)
        content = "\n".join(updated).rstrip() + "\n"
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
                file_handle.write(content)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, path)
            temp_path = None
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink()
                except FileNotFoundError:
                    pass

    def tail_logs(limit: int = 15) -> str:
        try:
            lines = settings.logs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return "🧾 <b>Recent Logs</b>\n\nNo log file has been created yet."
        recent = lines[-max(1, min(limit, 25)) :]
        if not recent:
            return "🧾 <b>Recent Logs</b>\n\nThe log file is empty."
        return "🧾 <b>Recent Logs</b>\n\n<code>" + html.escape("\n".join(recent)) + "</code>"

    def main_menu_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🩺 Post ECG", callback_data="ecg:post"),
                    InlineKeyboardButton(text="📚 Post Article", callback_data="ecg:article"),
                ],
                [InlineKeyboardButton(text="🎙️ Post Podcast", callback_data="ecg:podcast")],
                [
                    InlineKeyboardButton(text="📦 Refresh Cache", callback_data="ecg:cache"),
                    InlineKeyboardButton(text="📊 Dashboard", callback_data="ecg:status"),
                ],
                [
                    InlineKeyboardButton(text="⏰ Scheduler", callback_data="ecg:scheduler"),
                    InlineKeyboardButton(text="🧵 Topics", callback_data="ecg:topics"),
                ],
                [
                    InlineKeyboardButton(text="🎯 My Score", callback_data="ecg:score"),
                    InlineKeyboardButton(text="📈 Overall Stats", callback_data="ecg:stats"),
                ],
                [
                    InlineKeyboardButton(text="❌ Wrong Answers", callback_data="ecg:wrong"),
                    InlineKeyboardButton(text="⚙️ Settings", callback_data="ecg:settings"),
                ],
                [
                    InlineKeyboardButton(text="🧾 Runtime Logs", callback_data="ecg:logs"),
                    InlineKeyboardButton(text="🏠 Home", callback_data="ecg:home"),
                ],
            ]
        )

    def scheduler_menu_keyboard() -> InlineKeyboardMarkup:
        ecg_label = "▶️ Resume ECG" if scheduler_service.is_ecg_paused() else "⏸️ Pause ECG"
        article_label = "▶️ Resume Articles" if scheduler_service.is_article_paused() else "⏸️ Pause Articles"
        podcast_label = "▶️ Resume Podcasts" if scheduler_service.is_podcast_paused() else "⏸️ Pause Podcasts"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=ecg_label, callback_data="ecg:scheduler:toggle_ecg"),
                    InlineKeyboardButton(text=article_label, callback_data="ecg:scheduler:toggle_article"),
                ],
                [InlineKeyboardButton(text=podcast_label, callback_data="ecg:scheduler:toggle_podcast")],
                [
                    InlineKeyboardButton(text="🔄 Refresh Scheduler", callback_data="ecg:scheduler"),
                    InlineKeyboardButton(text="🏠 Main Menu", callback_data="ecg:home"),
                ],
            ]
        )

    def topics_menu_keyboard(can_bind_here: bool) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if can_bind_here:
            rows.append(
                [
                    InlineKeyboardButton(text="🩺 Bind ECG Here", callback_data="ecg:topics:bind_ecg"),
                    InlineKeyboardButton(text="📚 Bind Articles Here", callback_data="ecg:topics:bind_articles"),
                ]
            )
            rows.append([InlineKeyboardButton(text="🎙️ Bind Podcasts Here", callback_data="ecg:topics:bind_podcast")])
        rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="ecg:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def settings_menu_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🩺 ECG Hours", callback_data="ecg:settings:edit:ecg_interval"),
                    InlineKeyboardButton(text="📚 Article Hours", callback_data="ecg:settings:edit:article_interval"),
                ],
                [InlineKeyboardButton(text="🎙️ Podcast Hours", callback_data="ecg:settings:edit:podcast_interval")],
                [
                    InlineKeyboardButton(text="❌ Wrong Limit", callback_data="ecg:settings:edit:wrong_limit"),
                    InlineKeyboardButton(text="🧾 Logs", callback_data="ecg:logs"),
                ],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="ecg:home")],
            ]
        )

    async def menu_intro_text(message: Message) -> str:
        topic_updated = await sync_topic_from_message(message)
        topic_id = await topic_manager.get_topic_id()
        article_topic_id = await article_topic_manager.get_topic_id()
        podcast_topic_id = await podcast_topic_manager.get_topic_id()
        topic_line = f"<b>Configured ECG topic:</b> <code>{topic_id}</code>"
        if topic_updated:
            topic_line += " (updated from this forum topic)"
        return "\n".join(
            [
                "🫀 <b>ECG Control Center</b>",
                "",
                "Manage ECG quiz posting, article posting, podcast posting, schedulers, topics, and review stats from one panel.",
                "",
                "⏱️ <b>Posting Cadence</b>",
                f"• <b>ECG interval:</b> every {settings.post_interval_hours} hours",
                f"• <b>Article interval:</b> every {settings.article_post_interval_hours} hours",
                f"• <b>Podcast interval:</b> every {settings.podcast_post_interval_hours} hours",
                "",
                "🧵 <b>Topic Binding</b>",
                topic_line,
                f"• <b>Articles topic:</b> <code>{article_topic_id}</code>",
                f"• <b>Podcasts topic:</b> <code>{podcast_topic_id}</code>",
                "",
                "Use the menu below to post ECGs, articles, or podcasts immediately, refresh cache, manage schedulers, inspect performance, edit intervals, and review recent logs.",
            ]
        )

    async def status_text(message: Message) -> str:
        topic_updated = await sync_topic_from_message(message)
        topic_id = await topic_manager.get_topic_id()
        article_topic_id = await article_topic_manager.get_topic_id()
        podcast_topic_id = await podcast_topic_manager.get_topic_id()
        status = await stats_service.get_status()
        next_ecg_run = scheduler_service.next_ecg_run_time()
        next_article_run = scheduler_service.next_article_run_time()
        next_podcast_run = scheduler_service.next_podcast_run_time()
        topic_line = f"Topic ID: <code>{topic_id}</code>"
        if topic_updated:
            topic_line += " (updated from this forum topic)"
        ecg_scheduler_state = "⏸️ paused" if scheduler_service.is_ecg_paused() else "🟢 running"
        article_scheduler_state = "⏸️ paused" if scheduler_service.is_article_paused() else "🟢 running"
        podcast_scheduler_state = "⏸️ paused" if scheduler_service.is_podcast_paused() else "🟢 running"

        return "\n".join(
            [
                "📊 <b>Bot Status</b>",
                "",
                "📍 <b>Targets</b>",
                f"• <b>Chat ID:</b> <code>{settings.chat_id}</code>",
                f"• <b>ECG topic:</b> {topic_line}",
                f"• <b>Articles topic ID:</b> <code>{article_topic_id}</code>",
                f"• <b>Podcasts topic ID:</b> <code>{podcast_topic_id}</code>",
                "",
                "⏰ <b>Schedulers</b>",
                f"• <b>ECG scheduler:</b> {ecg_scheduler_state}",
                f"• <b>Article scheduler:</b> {article_scheduler_state}",
                f"• <b>Podcast scheduler:</b> {podcast_scheduler_state}",
                f"• <b>Next ECG post:</b> {format_dt(next_ecg_run)}",
                f"• <b>Next article post:</b> {format_dt(next_article_run)}",
                f"• <b>Next podcast post:</b> {format_dt(next_podcast_run)}",
                "",
                "📦 <b>Content Stats</b>",
                f"• <b>Cached cases:</b> {status.total_cases}",
                f"• <b>Unused cache:</b> {status.unused_cases}",
                f"• <b>Used cases:</b> {status.used_cases}",
                f"• <b>Posted quizzes:</b> {status.posted_quizzes}",
                f"• <b>Posted articles:</b> {status.posted_articles}",
                f"• <b>Posted podcasts:</b> {status.posted_podcasts}",
                f"• <b>Recorded answers:</b> {status.total_answers}",
                "",
                "🕒 <b>Recent Activity</b>",
                f"• <b>Last cache fill:</b> {format_dt(status.last_cache_fill_at)}",
                f"• <b>Last ECG post:</b> {format_dt(status.last_ecg_posted_at)}",
                f"• <b>Last article post:</b> {format_dt(status.last_article_posted_at)}",
                f"• <b>Last podcast post:</b> {format_dt(status.last_podcast_posted_at)}",
            ]
        )

    async def score_text(target_user_id: int | None) -> str:
        score = await stats_service.get_score(user_id=target_user_id)
        return "\n".join(
            [
                "🎯 <b>Performance</b>",
                f"📝 Total answers: {score.total_answers}",
                f"✅ Correct: {score.correct_answers}",
                f"❌ Incorrect: {score.incorrect_answers}",
                f"📈 Accuracy: {score.accuracy_percent:.1f}%",
            ]
        )

    async def overall_stats_text() -> str:
        overall = await stats_service.get_score(user_id=None)
        per_user = await stats_service.get_all_user_scores()

        lines = [
            "📈 <b>ECG Statistics</b>",
            "",
            f"👥 <b>Users:</b> {len(per_user)}",
            f"📝 <b>Total answers:</b> {overall.total_answers}",
            f"✅ <b>Correct:</b> {overall.correct_answers}",
            f"❌ <b>Wrong:</b> {overall.incorrect_answers}",
            f"🎯 <b>Accuracy:</b> {overall.accuracy_percent:.1f}%",
        ]

        if per_user:
            lines.extend(["", "👤 <b>Per User</b>"])
            for item in per_user:
                lines.extend(
                    [
                        "",
                        f"🫀 <b>{format_user_label(item.user_id)}</b>",
                        f"📝 Total: {item.total_answers}",
                        f"✅ Correct: {item.correct_answers}",
                        f"❌ Wrong: {item.incorrect_answers}",
                        f"🎯 Accuracy: {item.accuracy_percent:.1f}%",
                    ]
                )

        return "\n".join(lines)

    async def wrong_answers_text(target_user_id: int | None) -> str:
        items = await stats_service.get_recent_wrong_answers(
            user_id=target_user_id,
            limit=settings.wrong_answer_limit,
        )
        if not items:
            return "✅ No incorrect answers recorded yet."

        lines = ["❌ <b>Recent Incorrect Answers</b>"]
        for item in items:
            lines.extend(
                [
                    "",
                    html.escape(clean_vignette(item.vignette)),
                    f"Diagnosis: {html.escape(item.diagnosis)}",
                    f"Answered: {format_dt(item.answered_at)}",
                ]
            )
        return "\n".join(lines)

    async def scheduler_text() -> str:
        ecg_state = "⏸️ paused" if scheduler_service.is_ecg_paused() else "🟢 running"
        article_state = "⏸️ paused" if scheduler_service.is_article_paused() else "🟢 running"
        podcast_state = "⏸️ paused" if scheduler_service.is_podcast_paused() else "🟢 running"
        return "\n".join(
            [
                "⏰ <b>Scheduler Control</b>",
                f"🩺 ECG scheduler: {ecg_state}",
                f"📚 Article scheduler: {article_state}",
                f"🎙️ Podcast scheduler: {podcast_state}",
                f"⏭️ Next ECG post: {format_dt(scheduler_service.next_ecg_run_time())}",
                f"⏭️ Next article post: {format_dt(scheduler_service.next_article_run_time())}",
                f"⏭️ Next podcast post: {format_dt(scheduler_service.next_podcast_run_time())}",
                "",
                "Use the buttons below to pause or resume each cycle.",
            ]
        )

    def settings_text() -> str:
        return "\n".join(
            [
                "⚙️ <b>Admin Settings</b>",
                "",
                "⏱️ <b>Cadence</b>",
                f"• <b>ECG interval:</b> <code>{settings.post_interval_hours}</code> hour(s)",
                f"• <b>Article interval:</b> <code>{settings.article_post_interval_hours}</code> hour(s)",
                f"• <b>Podcast interval:</b> <code>{settings.podcast_post_interval_hours}</code> hour(s)",
                "",
                "🧠 <b>Scoring</b>",
                f"• <b>Wrong-answer limit:</b> <code>{settings.wrong_answer_limit}</code>",
                "",
                "Choose a value to edit. Changes are saved to <code>.env</code> and applied live.",
            ]
        )

    async def topics_text(message: Message) -> str:
        configured_topic_id = await topic_manager.get_topic_id()
        configured_articles_topic_id = await article_topic_manager.get_topic_id()
        configured_podcast_topic_id = await podcast_topic_manager.get_topic_id()
        lines = [
            "🧵 <b>Topic Bindings</b>",
            f"Chat ID: <code>{message.chat.id}</code>",
            f"Current message thread: <code>{message.message_thread_id}</code>",
            f"Stored ECG topic: <code>{configured_topic_id}</code>",
            f"Stored articles topic: <code>{configured_articles_topic_id}</code>",
            f"Stored podcasts topic: <code>{configured_podcast_topic_id}</code>",
        ]
        if message.message_thread_id is None:
            lines.extend(
                [
                    "",
                    "Open this menu from inside a forum topic if you want to bind that topic with one tap.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "You can bind the current thread as the ECG topic, articles topic, or podcasts topic.",
                ]
            )
        return "\n".join(lines)

    async def cache_refresh_text() -> str:
        report = await cache_service.fill_cache_if_needed(force=True)
        total_cases = await cache_service.count_total_cases()
        unused_cases = await cache_service.count_unused_cases()
        return "\n".join(
            [
                "📦 <b>Cache Refresh Complete</b>",
                f"Requested: {report.requested}",
                f"Stored: {report.stored}",
                f"Skipped: {report.skipped}",
                f"Total cached cases: {total_cases}",
                f"Unused cached cases: {unused_cases}",
            ]
        )

    async def post_next_ecg(bot: Bot, message: Message) -> str:
        await sync_topic_from_message(message)
        result = await posting_service.post_next_case(bot)
        return (
            "🩺 <b>ECG Posted</b>\n"
            f"Case ID: <code>{result.case_id}</code>\n"
            f"Poll ID: <code>{html.escape(result.poll_id)}</code>"
        )

    async def post_next_article(bot: Bot) -> str:
        result = await article_posting_service.post_next_article(bot)
        if result is None:
            return "🫥 No unseen article is available right now."
        lines = ["📚 <b>Article Posted</b>", html.escape(result.title)]
        if result.pdf_attached:
            lines.append("📎 PDF attached.")
        return "\n".join(lines)

    async def post_next_podcast(bot: Bot) -> str:
        result = await podcast_posting_service.post_next_podcast(bot)
        if result is None:
            return "🫥 No unseen podcast episode is available right now."
        return "\n".join(["🎙️ <b>Podcast Posted</b>", html.escape(result.title)])

    async def send_menu(message: Message) -> None:
        await message.answer(await menu_intro_text(message), reply_markup=main_menu_keyboard())

    async def edit_menu(query: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup) -> None:
        if query.message is None:
            return
        await query.message.edit_text(text, reply_markup=reply_markup)

    @router.message(Command("start"))
    async def start_command(message: Message) -> None:
        if not await ensure_owner(message):
            return
        if message.from_user:
            pending_inputs.pop(message.from_user.id, None)
        await send_menu(message)

    @router.message(Command("menu"))
    async def menu_command(message: Message) -> None:
        if not await ensure_owner(message):
            return
        if message.from_user:
            pending_inputs.pop(message.from_user.id, None)
        await send_menu(message)

    @router.message(Command("id"))
    async def id_command(message: Message) -> None:
        if not await ensure_owner(message):
            return

        LOGGER.info(
            "Received /id command. chat_id=%s message_thread_id=%s user_id=%s",
            message.chat.id,
            message.message_thread_id,
            message.from_user.id if message.from_user else None,
        )
        topic_updated = await sync_topic_from_message(message)
        topic_id = await topic_manager.get_topic_id()
        lines = [
            "🧵 <b>Topic Info</b>",
            f"Chat ID: <code>{message.chat.id}</code>",
            f"Message thread ID: <code>{message.message_thread_id}</code>",
            f"Stored ECG topic ID: <code>{topic_id}</code>",
        ]
        if topic_updated:
            lines.append("✅ This topic is now bound for scheduled ECG posts.")

        await message.answer(
            "\n".join(lines),
            reply_markup=topics_menu_keyboard(message.message_thread_id is not None and message.chat.id == settings.chat_id),
        )

    @router.message(Command("post_now"))
    async def post_now_command(message: Message, bot: Bot) -> None:
        if not await ensure_owner(message):
            return

        LOGGER.info(
            "Received /post_now command. chat_id=%s message_thread_id=%s user_id=%s",
            message.chat.id,
            message.message_thread_id,
            message.from_user.id if message.from_user else None,
        )
        progress = await message.answer("🩺 Posting the next ECG case...")
        try:
            result_text = await post_next_ecg(bot, message)
        except Exception as exc:
            await progress.edit_text(f"❌ Posting failed: {html.escape(str(exc))}")
            return
        await progress.edit_text(result_text)

    @router.message(Command("next"))
    async def next_command(message: Message, bot: Bot) -> None:
        if not await ensure_owner(message):
            return

        LOGGER.info(
            "Received /next command. chat_id=%s message_thread_id=%s user_id=%s",
            message.chat.id,
            message.message_thread_id,
            message.from_user.id if message.from_user else None,
        )
        await sync_topic_from_message(message)
        try:
            await posting_service.post_next_case(bot)
        except Exception as exc:
            await message.answer(f"❌ Fetching failed: {html.escape(str(exc))}")
            return

    @router.message(Command("post_article_now"))
    async def post_article_now_command(message: Message, bot: Bot) -> None:
        if not await ensure_owner(message):
            return

        LOGGER.info(
            "Received /post_article_now command. chat_id=%s message_thread_id=%s user_id=%s",
            message.chat.id,
            message.message_thread_id,
            message.from_user.id if message.from_user else None,
        )
        progress = await message.answer("📚 Posting the next unseen article...")
        try:
            result_text = await post_next_article(bot)
        except Exception as exc:
            await progress.edit_text(f"❌ Article posting failed: {html.escape(str(exc))}")
            return

        await progress.edit_text(result_text)

    @router.message(Command("post_podcast_now"))
    async def post_podcast_now_command(message: Message, bot: Bot) -> None:
        if not await ensure_owner(message):
            return

        LOGGER.info(
            "Received /post_podcast_now command. chat_id=%s message_thread_id=%s user_id=%s",
            message.chat.id,
            message.message_thread_id,
            message.from_user.id if message.from_user else None,
        )
        progress = await message.answer("🎙️ Posting the next unseen podcast episode...")
        try:
            result_text = await post_next_podcast(bot)
        except Exception as exc:
            await progress.edit_text(f"❌ Podcast posting failed: {html.escape(str(exc))}")
            return

        await progress.edit_text(result_text)

    @router.message(Command("score"))
    async def score_command(message: Message) -> None:
        if not await ensure_owner(message):
            return

        await sync_topic_from_message(message)
        target_user_id = settings.owner_user_id or (message.from_user.id if message.from_user else None)
        await message.answer(await score_text(target_user_id), reply_markup=main_menu_keyboard())

    @router.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        if not await ensure_owner(message):
            return
        await message.answer(await overall_stats_text(), reply_markup=main_menu_keyboard())

    @router.message(Command("wrong"))
    async def wrong_command(message: Message) -> None:
        if not await ensure_owner(message):
            return

        await sync_topic_from_message(message)
        target_user_id = settings.owner_user_id or (message.from_user.id if message.from_user else None)
        await message.answer(await wrong_answers_text(target_user_id), disable_web_page_preview=True)

    @router.message(Command("status"))
    async def status_command(message: Message) -> None:
        if not await ensure_owner(message):
            return
        await message.answer(await status_text(message), reply_markup=main_menu_keyboard())

    @router.message()
    async def pending_input(message: Message) -> None:
        if not await ensure_owner(message):
            return
        if not message.from_user:
            return
        action = pending_inputs.get(message.from_user.id)
        if action is None:
            return
        raw = (message.text or "").strip()
        if raw.casefold() in {"cancel", "/cancel"}:
            pending_inputs.pop(message.from_user.id, None)
            await message.answer("🚫 Edit cancelled.", reply_markup=main_menu_keyboard())
            return
        try:
            value = int(raw)
            if value <= 0:
                raise ValueError("Value must be greater than zero.")
        except ValueError as exc:
            await message.answer(f"⚠️ {html.escape(str(exc))}\n\nSend a whole number or /cancel.", parse_mode="HTML")
            return

        if action == "ecg_interval":
            settings.post_interval_hours = value
            update_env_setting("POST_INTERVAL_HOURS", value)
            scheduler_service.update_ecg_interval(value)
            label = "ECG interval"
        elif action == "article_interval":
            settings.article_post_interval_hours = value
            update_env_setting("ARTICLE_POST_INTERVAL_HOURS", value)
            scheduler_service.update_article_interval(value)
            label = "article interval"
        elif action == "podcast_interval":
            settings.podcast_post_interval_hours = value
            update_env_setting("PODCAST_POST_INTERVAL_HOURS", value)
            scheduler_service.update_podcast_interval(value)
            label = "podcast interval"
        else:
            settings.wrong_answer_limit = value
            update_env_setting("WRONG_ANSWER_LIMIT", value)
            label = "wrong-answer limit"

        pending_inputs.pop(message.from_user.id, None)
        await message.answer(
            f"✅ Updated {label} to <code>{value}</code>.",
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )

    @router.callback_query(F.data.startswith("ecg:"))
    async def callback_router(query: CallbackQuery, bot: Bot) -> None:
        if query.message is None:
            return
        if not is_owner_user(query.from_user.id if query.from_user else None):
            await query.answer("Not authorized.", show_alert=True)
            return

        data = query.data or ""

        if data == "ecg:home":
            await query.answer()
            if query.from_user:
                pending_inputs.pop(query.from_user.id, None)
            await edit_menu(query, await menu_intro_text(query.message), main_menu_keyboard())
            return

        if data == "ecg:status":
            await query.answer()
            await edit_menu(query, await status_text(query.message), main_menu_keyboard())
            return

        if data == "ecg:post":
            await query.answer("Posting ECG...")
            progress = await query.message.answer("🩺 Posting the next ECG case...")
            try:
                result_text = await post_next_ecg(bot, query.message)
            except Exception as exc:
                await progress.edit_text(f"❌ Posting failed: {html.escape(str(exc))}")
            else:
                await progress.edit_text(result_text)
            await edit_menu(query, await status_text(query.message), main_menu_keyboard())
            return

        if data == "ecg:article":
            await query.answer("Posting article...")
            progress = await query.message.answer("📚 Posting the next unseen article...")
            try:
                result_text = await post_next_article(bot)
            except Exception as exc:
                await progress.edit_text(f"❌ Article posting failed: {html.escape(str(exc))}")
            else:
                await progress.edit_text(result_text)
            await edit_menu(query, await status_text(query.message), main_menu_keyboard())
            return

        if data == "ecg:podcast":
            await query.answer("Posting podcast...")
            progress = await query.message.answer("🎙️ Posting the next unseen podcast episode...")
            try:
                result_text = await post_next_podcast(bot)
            except Exception as exc:
                await progress.edit_text(f"❌ Podcast posting failed: {html.escape(str(exc))}")
            else:
                await progress.edit_text(result_text)
            await edit_menu(query, await status_text(query.message), main_menu_keyboard())
            return

        if data == "ecg:cache":
            await query.answer("Refreshing cache...")
            progress = await query.message.answer("📦 Refreshing ECG cache...")
            try:
                result_text = await cache_refresh_text()
            except Exception as exc:
                await progress.edit_text(f"❌ Cache refresh failed: {html.escape(str(exc))}")
            else:
                await progress.edit_text(result_text)
            await edit_menu(query, await status_text(query.message), main_menu_keyboard())
            return

        if data == "ecg:scheduler":
            await query.answer()
            await edit_menu(query, await scheduler_text(), scheduler_menu_keyboard())
            return

        if data == "ecg:settings":
            await query.answer()
            await edit_menu(query, settings_text(), settings_menu_keyboard())
            return

        if data == "ecg:logs":
            await query.answer()
            await edit_menu(
                query,
                tail_logs(),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⚙️ Settings", callback_data="ecg:settings")],
                        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="ecg:home")],
                    ]
                ),
            )
            return

        if data == "ecg:settings:edit:ecg_interval":
            pending_inputs[query.from_user.id] = "ecg_interval"
            await query.answer("Waiting for a number.")
            await query.message.answer("🩺 Send the new ECG interval in hours.\n\nExample: <code>3</code>\nSend <code>cancel</code> to abort.", parse_mode="HTML")
            return

        if data == "ecg:settings:edit:article_interval":
            pending_inputs[query.from_user.id] = "article_interval"
            await query.answer("Waiting for a number.")
            await query.message.answer("📚 Send the new article interval in hours.\n\nExample: <code>2</code>\nSend <code>cancel</code> to abort.", parse_mode="HTML")
            return

        if data == "ecg:settings:edit:podcast_interval":
            pending_inputs[query.from_user.id] = "podcast_interval"
            await query.answer("Waiting for a number.")
            await query.message.answer("🎙️ Send the new podcast interval in hours.\n\nExample: <code>168</code>\nSend <code>cancel</code> to abort.", parse_mode="HTML")
            return

        if data == "ecg:settings:edit:wrong_limit":
            pending_inputs[query.from_user.id] = "wrong_limit"
            await query.answer("Waiting for a number.")
            await query.message.answer("❌ Send the new wrong-answer history limit.\n\nExample: <code>10</code>\nSend <code>cancel</code> to abort.", parse_mode="HTML")
            return

        if data == "ecg:scheduler:toggle_ecg":
            changed = scheduler_service.resume_ecg() if scheduler_service.is_ecg_paused() else scheduler_service.pause_ecg()
            await query.answer("Updated." if changed else "No change.")
            await edit_menu(query, await scheduler_text(), scheduler_menu_keyboard())
            return

        if data == "ecg:scheduler:toggle_article":
            changed = (
                scheduler_service.resume_articles()
                if scheduler_service.is_article_paused()
                else scheduler_service.pause_articles()
            )
            await query.answer("Updated." if changed else "No change.")
            await edit_menu(query, await scheduler_text(), scheduler_menu_keyboard())
            return

        if data == "ecg:scheduler:toggle_podcast":
            changed = (
                scheduler_service.resume_podcasts()
                if scheduler_service.is_podcast_paused()
                else scheduler_service.pause_podcasts()
            )
            await query.answer("Updated." if changed else "No change.")
            await edit_menu(query, await scheduler_text(), scheduler_menu_keyboard())
            return

        if data == "ecg:topics":
            await query.answer()
            can_bind_here = query.message.message_thread_id is not None and query.message.chat.id == settings.chat_id
            await edit_menu(query, await topics_text(query.message), topics_menu_keyboard(can_bind_here))
            return

        if data == "ecg:topics:bind_ecg":
            if query.message.message_thread_id is None or query.message.chat.id != settings.chat_id:
                await query.answer("Open the menu from the target forum topic first.", show_alert=True)
                return
            await topic_manager.set_topic_id(query.message.message_thread_id)
            await query.answer("ECG topic updated.")
            await edit_menu(query, await topics_text(query.message), topics_menu_keyboard(True))
            return

        if data == "ecg:topics:bind_articles":
            if query.message.message_thread_id is None or query.message.chat.id != settings.chat_id:
                await query.answer("Open the menu from the target forum topic first.", show_alert=True)
                return
            await article_topic_manager.set_topic_id(query.message.message_thread_id)
            await query.answer("Articles topic updated.")
            await edit_menu(query, await topics_text(query.message), topics_menu_keyboard(True))
            return

        if data == "ecg:topics:bind_podcast":
            if query.message.message_thread_id is None or query.message.chat.id != settings.chat_id:
                await query.answer("Open the menu from the target forum topic first.", show_alert=True)
                return
            await podcast_topic_manager.set_topic_id(query.message.message_thread_id)
            await query.answer("Podcasts topic updated.")
            await edit_menu(query, await topics_text(query.message), topics_menu_keyboard(True))
            return

        if data == "ecg:score":
            target_user_id = settings.owner_user_id or (query.from_user.id if query.from_user else None)
            await query.answer()
            await edit_menu(query, await score_text(target_user_id), main_menu_keyboard())
            return

        if data == "ecg:stats":
            await query.answer()
            await edit_menu(query, await overall_stats_text(), main_menu_keyboard())
            return

        if data == "ecg:wrong":
            target_user_id = settings.owner_user_id or (query.from_user.id if query.from_user else None)
            await query.answer()
            await edit_menu(query, await wrong_answers_text(target_user_id), main_menu_keyboard())
            return

        await query.answer()

    return router
