from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.types import BufferedInputFile, Message
from PIL import Image
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import BASE_DIR
from bot.database.models import ECGCase, PostedQuiz, UserAnswer
from bot.services.cache_service import ECGCacheService
from bot.services.state_store import StateStore
from bot.services.stats_service import STATE_LAST_ECG_POSTED_AT
from bot.services.topic_manager import TopicManager

LOGGER = logging.getLogger(__name__)
DIFFICULTY_PATTERN = re.compile(r"(?P<stars>(?:\*\s*){1,5})Difficulty rating\s*$", re.IGNORECASE)
MAX_ECG_POST_ATTEMPTS = 3


@dataclass(slots=True)
class PostResult:
    case_id: int
    poll_id: str
    diagnosis: str
    source_url: str


class ECGPostingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cache_service: ECGCacheService,
        topic_manager: TopicManager,
        state_store: StateStore,
        *,
        chat_id: int,
        notify_user_id: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cache_service = cache_service
        self.topic_manager = topic_manager
        self.state_store = state_store
        self.chat_id = chat_id
        self.notify_user_id = notify_user_id
        self._post_lock = asyncio.Lock()
        self._media_warm_lock = asyncio.Lock()
        self._media_warm_task: asyncio.Task[None] | None = None
        self._media_cache_dir = BASE_DIR / "downloads" / "ecg_media"
        self._media_cache_dir.mkdir(parents=True, exist_ok=True)

    async def post_next_case(self, bot: Bot) -> PostResult:
        async with self._post_lock:
            LOGGER.info("Selecting the next ECG case from the local cache.")
            await self.cache_service.fill_cache_if_needed()
            topic_id = await self.topic_manager.get_topic_id()
            await self._release_pending_unanswered_explanations(bot, topic_id=topic_id)
            candidates = await self._load_candidate_cases(limit=MAX_ECG_POST_ATTEMPTS * 2)
            if not candidates:
                refill_report = await self.cache_service.fill_cache_if_needed(force=True)
                if refill_report.stored:
                    candidates = await self._load_candidate_cases(limit=MAX_ECG_POST_ATTEMPTS * 2)

            if not candidates and await self.cache_service.count_total_cases() > 0:
                LOGGER.info("All cached ECG cases have been used. Starting a new training cycle.")
                await self.cache_service.reset_usage_cycle()
                candidates = await self._load_candidate_cases(limit=MAX_ECG_POST_ATTEMPTS * 2)

            if not candidates:
                raise RuntimeError("No unseen ECG cases are available to post.")

            candidates.sort(key=lambda item: self._find_cached_case_media_path(item) is None)
            candidates = candidates[:MAX_ECG_POST_ATTEMPTS]
            failures: list[str] = []
            for case in candidates:
                try:
                    return await self._post_case_candidate(bot, case=case, topic_id=topic_id)
                except Exception as exc:
                    LOGGER.warning(
                        "Failed to post ECG case rownum=%s. Trying the next unused case. error=%s",
                        case.rownum,
                        exc,
                    )
                    failures.append(f"case #{case.rownum + 1}: {exc}")

            raise RuntimeError(
                "Unable to post an ECG case after "
                f"{len(candidates)} attempts. Last error: {failures[-1] if failures else 'unknown'}"
            )

    async def warm_upcoming_case_media(self, *, limit: int = 3) -> int:
        async with self._media_warm_lock:
            warmed = 0
            for case in await self._load_candidate_cases(limit=limit):
                if self._find_cached_case_media_path(case) is not None:
                    continue
                try:
                    await self._load_case_media(case)
                    warmed += 1
                except Exception as exc:
                    LOGGER.warning("Failed to warm ECG media cache for rownum=%s: %s", case.rownum, exc)
            if warmed:
                LOGGER.info("Warmed local ECG media cache for %s upcoming cases.", warmed)
            return warmed

    async def _load_candidate_cases(self, *, limit: int) -> list[ECGCase]:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(ECGCase)
                .where(ECGCase.used.is_(False))
                .order_by(ECGCase.created_at.asc(), ECGCase.id.asc())
                .limit(limit)
            )
            return list(result.all())

    async def _post_case_candidate(self, bot: Bot, *, case: ECGCase, topic_id: int) -> PostResult:
        LOGGER.info(
            "Preparing ECG case rownum=%s for chat_id=%s topic_id=%s.",
            case.rownum,
            self.chat_id,
            topic_id,
        )
        poll_options, correct_option_index = self._build_telegram_poll_options(
            json.loads(case.options_json),
            case.correct_option_index,
        )

        sent_message_ids: list[int] = []
        try:
            media_message = await self._send_case_media(bot, case, topic_id)
            sent_message_ids.append(media_message.message_id)

            prompt_message = await bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=topic_id,
                text=self._build_case_prompt(case),
                disable_web_page_preview=True,
            )
            sent_message_ids.append(prompt_message.message_id)

            poll_message = await bot.send_poll(
                chat_id=self.chat_id,
                message_thread_id=topic_id,
                question="What is the correct answer?",
                options=poll_options,
                type="quiz",
                is_anonymous=False,
                correct_option_id=correct_option_index,
            )
            sent_message_ids.append(poll_message.message_id)
            LOGGER.info(
                "Posted ECG case rownum=%s with poll_id=%s.",
                case.rownum,
                poll_message.poll.id,
            )

            async with self.session_factory() as session:
                session.add(
                    PostedQuiz(
                        ecg_case_id=case.id,
                        poll_id=poll_message.poll.id,
                        chat_id=self.chat_id,
                        topic_id=topic_id,
                    )
                )
                stored_case = await session.get(ECGCase, case.id)
                if stored_case is not None:
                    stored_case.used = True
                await session.commit()
        except Exception:
            await self._cleanup_partial_case_post(bot, sent_message_ids)
            raise

        try:
            await self.state_store.set_datetime(STATE_LAST_ECG_POSTED_AT, datetime.now(UTC))
        except Exception as exc:
            LOGGER.warning("Failed to update last ECG post state after case rownum=%s: %s", case.rownum, exc)

        await self._notify_owner(bot, case=case, topic_id=topic_id, poll_id=poll_message.poll.id)
        return PostResult(
            case_id=case.id,
            poll_id=poll_message.poll.id,
            diagnosis=case.diagnosis,
            source_url=case.source_url,
        )

    async def _release_pending_unanswered_explanations(self, bot: Bot, *, topic_id: int) -> int:
        pending = await self._load_unanswered_quizzes_without_explanation(topic_id=topic_id)
        released = 0

        for posted_quiz, case in pending:
            claimed_at = datetime.now(UTC)
            async with self.session_factory() as session:
                claim_result = await session.execute(
                    update(PostedQuiz)
                    .where(
                        PostedQuiz.id == posted_quiz.id,
                        PostedQuiz.explanation_sent_at.is_(None),
                    )
                    .values(explanation_sent_at=claimed_at)
                )
                await session.commit()

            if (claim_result.rowcount or 0) <= 0:
                continue

            try:
                await self._send_explanation(
                    bot,
                    posted_quiz.chat_id,
                    posted_quiz.topic_id,
                    case,
                    intro_text="No answers were submitted before the next ECG. Tap to reveal the diagnosis and explanation.",
                )
            except Exception as exc:
                await self._release_explanation_claim(posted_quiz.id, claimed_at)
                LOGGER.warning(
                    "Failed to send unanswered ECG explanation for poll_id=%s before the next post: %s",
                    posted_quiz.poll_id,
                    exc,
                )
                continue

            released += 1

        if released:
            LOGGER.info("Released %s unanswered ECG explanation(s) before posting the next case.", released)
        return released

    async def _load_unanswered_quizzes_without_explanation(self, *, topic_id: int) -> list[tuple[PostedQuiz, ECGCase]]:
        answer_exists = exists(select(UserAnswer.id).where(UserAnswer.poll_id == PostedQuiz.poll_id))
        async with self.session_factory() as session:
            rows = await session.execute(
                select(PostedQuiz, ECGCase)
                .join(ECGCase, ECGCase.id == PostedQuiz.ecg_case_id)
                .where(
                    PostedQuiz.chat_id == self.chat_id,
                    PostedQuiz.topic_id == topic_id,
                    PostedQuiz.explanation_sent_at.is_(None),
                    ~answer_exists,
                )
                .order_by(PostedQuiz.created_at.asc(), PostedQuiz.id.asc())
            )
            return list(rows.all())

    async def handle_poll_answer(self, bot: Bot, *, poll_id: str, user_id: int, selected_option: int) -> bool:
        should_send_explanation = False
        explanation_claimed_at: datetime | None = None
        posted_quiz_id: int | None = None

        async with self.session_factory() as session:
            query = (
                select(PostedQuiz, ECGCase)
                .join(ECGCase, ECGCase.id == PostedQuiz.ecg_case_id)
                .where(PostedQuiz.poll_id == poll_id)
            )
            row = (await session.execute(query)).one_or_none()
            if row is None:
                LOGGER.warning("Received poll answer for unknown poll_id=%s.", poll_id)
                return False

            posted_quiz, case = row
            posted_quiz_id = posted_quiz.id
            is_correct = selected_option == case.correct_option_index
            LOGGER.info(
                "Received poll answer for poll_id=%s from user_id=%s. selected_option=%s correct=%s",
                poll_id,
                user_id,
                selected_option,
                is_correct,
            )

            answer = await session.scalar(
                select(UserAnswer).where(UserAnswer.poll_id == poll_id, UserAnswer.user_id == user_id)
            )
            if answer is None:
                session.add(
                    UserAnswer(
                        poll_id=poll_id,
                        user_id=user_id,
                        selected_option=selected_option,
                        is_correct=is_correct,
                    )
                )
            else:
                answer.selected_option = selected_option
                answer.is_correct = is_correct

            explanation_claimed_at = datetime.now(UTC)
            claim_result = await session.execute(
                update(PostedQuiz)
                .where(
                    PostedQuiz.id == posted_quiz.id,
                    PostedQuiz.explanation_sent_at.is_(None),
                )
                .values(explanation_sent_at=explanation_claimed_at)
            )
            should_send_explanation = (claim_result.rowcount or 0) > 0
            await session.commit()

        if not should_send_explanation:
            LOGGER.info("Explanation already released for poll_id=%s. Skipping duplicate send.", poll_id)
            return True

        try:
            await self._send_explanation(
                bot,
                posted_quiz.chat_id,
                posted_quiz.topic_id,
                case,
                intro_text="First answer received. Tap to reveal the diagnosis and explanation.",
            )
        except Exception:
            if posted_quiz_id is not None:
                await self._release_explanation_claim(posted_quiz_id, explanation_claimed_at)
            raise
        return True

    async def _send_case_media(
        self,
        bot: Bot,
        case: ECGCase,
        topic_id: int,
    ) -> Message:
        caption = f"&#129728; <b>ECG Challenge</b>\n<i>Case #{case.rownum + 1}</i>"

        if self._can_delegate_media_fetch_to_telegram(case.image_url):
            try:
                LOGGER.info("Uploading ECG image by remote URL for case rownum=%s.", case.rownum)
                return await bot.send_photo(
                    chat_id=self.chat_id,
                    message_thread_id=topic_id,
                    photo=case.image_url,
                    caption=caption,
                )
            except Exception as exc:
                LOGGER.warning(
                    "Remote-URL ECG upload failed for case rownum=%s. Falling back to local fetch. error=%s",
                    case.rownum,
                    exc,
                )

        image_bytes, content_type = await self._load_case_media(case)
        upload, send_as_photo = self._prepare_case_upload(case, image_bytes, content_type)

        if send_as_photo:
            LOGGER.info("Uploading ECG image as local photo for case rownum=%s.", case.rownum)
            return await bot.send_photo(
                chat_id=self.chat_id,
                message_thread_id=topic_id,
                photo=upload,
                caption=caption,
            )

        LOGGER.info("Uploading ECG image as local document for case rownum=%s.", case.rownum)
        return await bot.send_document(
            chat_id=self.chat_id,
            message_thread_id=topic_id,
            document=upload,
            caption=caption,
        )

    def _can_delegate_media_fetch_to_telegram(self, value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def _load_case_media(self, case: ECGCase) -> tuple[bytes, str]:
        cached = self._read_cached_case_media(case)
        if cached is not None:
            LOGGER.info("Using locally cached ECG media for case rownum=%s.", case.rownum)
            return cached

        LOGGER.info("Downloading ECG image from %s", case.image_url)
        image_bytes, content_type = await self.cache_service.wave_maven_client.download_binary(case.image_url)
        return self._store_case_media(case, image_bytes, content_type)

    def _read_cached_case_media(self, case: ECGCase) -> tuple[bytes, str] | None:
        cached_path = self._find_cached_case_media_path(case)
        if cached_path is None:
            return None
        return cached_path.read_bytes(), self._content_type_for_suffix(cached_path.suffix)

    def _store_case_media(self, case: ECGCase, image_bytes: bytes, content_type: str) -> tuple[bytes, str]:
        upload, send_as_photo = self._prepare_case_upload(case, image_bytes, content_type)
        extension = Path(upload.filename).suffix or self._guess_extension(case.image_url, content_type)
        if send_as_photo and extension.lower() not in {".jpg", ".jpeg", ".png"}:
            extension = ".png"
        content = bytes(upload.data)
        final_content_type = self._content_type_for_suffix(extension)
        cache_path = self._media_cache_dir / f"ecg_case_{case.id}{extension.lower()}"

        for existing_path in self._media_cache_dir.glob(f"ecg_case_{case.id}.*"):
            if existing_path != cache_path:
                existing_path.unlink(missing_ok=True)

        cache_path.write_bytes(content)
        LOGGER.info("Cached ECG media locally for case rownum=%s at %s.", case.rownum, cache_path.name)
        return content, final_content_type

    def _find_cached_case_media_path(self, case: ECGCase) -> Path | None:
        for suffix in (".png", ".jpg", ".jpeg", ".gif", ".bin"):
            candidate = self._media_cache_dir / f"ecg_case_{case.id}{suffix}"
            if candidate.exists():
                return candidate
        return None

    async def _cleanup_partial_case_post(self, bot: Bot, message_ids: list[int]) -> None:
        if not message_ids:
            return
        for message_id in reversed(message_ids):
            try:
                await bot.delete_message(chat_id=self.chat_id, message_id=message_id)
            except Exception as exc:
                LOGGER.warning("Failed to clean up partial ECG post message_id=%s: %s", message_id, exc)

    def _schedule_media_warmup(self, *, limit: int) -> None:
        if self._media_warm_task is not None and not self._media_warm_task.done():
            return

        async def runner() -> None:
            try:
                await self.warm_upcoming_case_media(limit=limit)
            except Exception as exc:
                LOGGER.warning("Background ECG media warmup failed: %s", exc)

        self._media_warm_task = asyncio.create_task(runner(), name="ecg-media-warmup")

    def _prepare_case_upload(
        self,
        case: ECGCase,
        image_bytes: bytes,
        content_type: str,
    ) -> tuple[BufferedInputFile, bool]:
        extension = self._guess_extension(case.image_url, content_type)
        if extension.lower() in {".jpg", ".jpeg", ".png"}:
            filename = f"ecg_case_{case.id}{extension.lower()}"
            return BufferedInputFile(image_bytes, filename=filename), True

        converted_png = self._convert_image_to_png(image_bytes)
        if converted_png is not None:
            LOGGER.info(
                "Converted ECG image rownum=%s from %s to PNG for photo upload.",
                case.rownum,
                extension or content_type or "unknown format",
            )
            return BufferedInputFile(converted_png, filename=f"ecg_case_{case.id}.png"), True

        filename = f"ecg_case_{case.id}{extension}"
        return BufferedInputFile(image_bytes, filename=filename), False

    def _build_case_prompt(self, case: ECGCase) -> str:
        vignette, difficulty = self._extract_vignette_parts(case.vignette)
        vignette_text = html.escape(vignette) if vignette else "Review the ECG and choose the most likely diagnosis."
        lines = [
            "<b>&#10067; ECG Question</b>",
            "",
            "<b>&#128221; Clinical vignette</b>",
            vignette_text,
        ]
        if difficulty:
            lines.extend(["", f"<b>&#11088; Difficulty</b> {difficulty}"])
        return "\n".join(lines)

    async def _send_explanation(
        self,
        bot: Bot,
        chat_id: int,
        topic_id: int,
        case: ECGCase,
        *,
        intro_text: str,
    ) -> None:
        explanation_text = await self._resolve_explanation_text(case)
        explanation_paragraphs = self._format_explanation_paragraphs(explanation_text)
        spoilered_explanation = "\n\n".join(self._wrap_spoiler(paragraph) for paragraph in explanation_paragraphs)
        message = "\n".join(
            [
                "&#129728; <b>ECG Explanation</b>",
                "",
                f"<i>{html.escape(intro_text)}</i>",
                "",
                self._wrap_spoiler("<b>&#129504; Most likely diagnosis</b>"),
                self._wrap_spoiler(html.escape(self._normalize_text(case.diagnosis))),
                "",
                self._wrap_spoiler("<b>&#128218; Explanation</b>"),
                spoilered_explanation,
            ]
        )

        LOGGER.info(
            "Sending ECG explanation for case rownum=%s to chat_id=%s topic_id=%s.",
            case.rownum,
            chat_id,
            topic_id,
        )
        for chunk in self._chunk_text(message):
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=topic_id,
                text=chunk,
                disable_web_page_preview=True,
            )
        LOGGER.info("ECG explanation sent for case rownum=%s.", case.rownum)

    async def _resolve_explanation_text(self, case: ECGCase) -> str:
        try:
            refreshed_case = await self.cache_service.wave_maven_client.fetch_case_from_source(
                rownum=case.rownum,
                case_id=case.case_identifier,
                answer_url=case.source_url,
            )
        except Exception as exc:  # pragma: no cover - remote availability variability
            LOGGER.warning(
                "Failed to refresh Wave-Maven explanation for rownum=%s. Using cached explanation. error=%s",
                case.rownum,
                exc,
            )
            return case.explanation

        if refreshed_case.explanation:
            LOGGER.info("Using refreshed Wave-Maven explanation for rownum=%s.", case.rownum)
            return refreshed_case.explanation

        return case.explanation

    async def _release_explanation_claim(self, posted_quiz_id: int, claimed_at: datetime | None) -> None:
        async with self.session_factory() as session:
            query = update(PostedQuiz).where(PostedQuiz.id == posted_quiz_id)
            if claimed_at is not None:
                query = query.where(PostedQuiz.explanation_sent_at == claimed_at)
            await session.execute(query.values(explanation_sent_at=None))
            await session.commit()
        LOGGER.info("Released explanation send claim for posted_quiz_id=%s after send failure.", posted_quiz_id)

    async def _notify_owner(self, bot: Bot, *, case: ECGCase, topic_id: int, poll_id: str) -> None:
        if self.notify_user_id is None:
            return

        message = "\n".join(
            [
                "&#129728; <b>New ECG quiz posted</b>",
                f"&#128196; Case #{case.rownum + 1} was posted to the ECG forum topic.",
                f"&#128204; Topic ID: <code>{topic_id}</code>",
                f"&#128202; Poll ID: <code>{html.escape(poll_id)}</code>",
                "",
                self._build_case_prompt(case),
            ]
        )

        try:
            await bot.send_message(
                chat_id=self.notify_user_id,
                text=message,
                disable_web_page_preview=True,
            )
            LOGGER.info("Sent private notification to user_id=%s for case rownum=%s.", self.notify_user_id, case.rownum)
        except Exception as exc:  # pragma: no cover - Telegram availability path
            LOGGER.warning(
                "Failed to send private notification to user_id=%s: %s",
                self.notify_user_id,
                exc,
            )

    def _chunk_text(self, text: str, limit: int = 3900) -> list[str]:
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            split_at = remaining.rfind("\n", 0, limit)
            if split_at < 0:
                split_at = remaining.rfind(" ", 0, limit)
            if split_at < 0:
                split_at = limit

            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        return chunks

    def _guess_extension(self, url: str, content_type: str) -> str:
        path = Path(urlparse(url).path)
        if path.suffix:
            return path.suffix
        if "png" in content_type:
            return ".png"
        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg"
        if "gif" in content_type:
            return ".gif"
        return ".bin"

    def _content_type_for_suffix(self, suffix: str) -> str:
        normalized = suffix.lower()
        if normalized == ".png":
            return "image/png"
        if normalized in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if normalized == ".gif":
            return "image/gif"
        return "application/octet-stream"

    def _convert_image_to_png(self, image_bytes: bytes) -> bytes | None:
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                if getattr(image, "is_animated", False):
                    image.seek(0)

                if image.mode not in {"RGB", "RGBA", "L"}:
                    image = image.convert("RGB")
                elif image.mode == "RGBA":
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background

                output = BytesIO()
                image.save(output, format="PNG", optimize=True)
                return output.getvalue()
        except Exception as exc:  # pragma: no cover - media conversion variability
            LOGGER.warning("Failed to convert ECG image to PNG: %s", exc)
            return None

    def _build_telegram_poll_options(self, options: list[str], correct_option_index: int) -> tuple[list[str], int]:
        if not options:
            raise RuntimeError("No poll options are available for this ECG case.")
        if correct_option_index < 0 or correct_option_index >= len(options):
            raise RuntimeError("The stored correct poll option index is invalid.")

        truncated_options = [self._truncate_poll_option(option) for option in options]
        unique_options = self._deduplicate_truncated_options(truncated_options)
        return unique_options, correct_option_index

    def _truncate_poll_option(self, value: str, limit: int = 100) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3].rstrip() + "..."

    def _deduplicate_truncated_options(self, options: list[str], limit: int = 100) -> list[str]:
        counts: dict[str, int] = {}
        result: list[str] = []

        for option in options:
            count = counts.get(option, 0)
            counts[option] = count + 1
            if count == 0:
                result.append(option)
                continue

            suffix = f" [{count + 1}]"
            trimmed = option
            if len(trimmed) + len(suffix) > limit:
                trimmed = trimmed[: limit - len(suffix) - 3].rstrip() + "..."
            result.append(trimmed + suffix)

        return result

    def _extract_vignette_parts(self, value: str) -> tuple[str, str | None]:
        normalized = self._normalize_text(value)
        difficulty_match = DIFFICULTY_PATTERN.search(normalized)
        if not difficulty_match:
            return normalized, None

        star_count = difficulty_match.group("stars").count("*")
        cleaned = DIFFICULTY_PATTERN.sub("", normalized).strip(" -:")
        difficulty = "&#11088;" * star_count if star_count > 0 else None
        return cleaned, difficulty

    def _format_explanation_paragraphs(self, value: str) -> list[str]:
        normalized_lines = self._normalize_multiline_text(value)
        if not normalized_lines:
            return ["<i>Explanation unavailable.</i>"]

        paragraphs: list[str] = []
        current: list[str] = []

        def flush_current() -> None:
            if not current:
                return

            paragraph = "\n".join(current).strip()
            current.clear()
            if not paragraph:
                return

            paragraphs.extend(self._split_explanation_block(paragraph))

        for line in normalized_lines:
            if not line:
                flush_current()
                continue
            current.append(line)

        flush_current()
        styled = [self._style_explanation_paragraph(paragraph) for paragraph in paragraphs if paragraph.strip()]
        if len(styled) == 1:
            styled = [
                self._style_explanation_paragraph(paragraph)
                for paragraph in self._rebuild_explanation_paragraphs_from_sentences(value)
                if paragraph.strip()
            ] or styled
        return styled or ["<i>Explanation unavailable.</i>"]

    def _style_explanation_paragraph(self, paragraph: str) -> str:
        cleaned = paragraph.strip()
        if not cleaned:
            return ""

        match = re.match(r"^(?P<label>[A-Za-z][A-Za-z /()-]{1,32}:)\s*(?P<body>.+)$", cleaned)
        if match:
            label = html.escape(match.group("label"))
            body = html.escape(match.group("body"))
            return f"<b>{label}</b> {body}"

        return html.escape(cleaned)

    def _normalize_multiline_text(self, value: str) -> list[str]:
        normalized = (
            value.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\xa0", " ")
            .replace("\ufffd", '"')
        )
        return [" ".join(line.split()).strip() for line in normalized.split("\n")]

    def _split_explanation_block(self, paragraph: str) -> list[str]:
        see_match = re.search(r"^(?P<body>.+?)\s+(?P<see>See:\s+.+)$", paragraph, re.DOTALL)
        if see_match:
            body = see_match.group("body").strip()
            see = see_match.group("see").strip()
            if body and see:
                return [body, see]
        return [paragraph]

    def _rebuild_explanation_paragraphs_from_sentences(self, value: str) -> list[str]:
        normalized = self._normalize_text(value)
        if not normalized:
            return []

        segments = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", normalized)
        paragraphs: list[str] = []
        current: list[str] = []

        def flush() -> None:
            if not current:
                return
            paragraph = " ".join(current).strip()
            current.clear()
            if paragraph:
                paragraphs.extend(self._split_explanation_block(paragraph))

        for segment in segments:
            cleaned = segment.strip()
            if not cleaned:
                continue

            current.append(cleaned)
            if len(current) >= 2 or cleaned.startswith("See:"):
                flush()

        flush()
        return paragraphs

    def _wrap_spoiler(self, value: str) -> str:
        if not value:
            return ""
        return f"<tg-spoiler>{value}</tg-spoiler>"

    def _normalize_text(self, value: str) -> str:
        cleaned = value.replace("\xa0", " ").replace("\ufffd", '"')
        return " ".join(cleaned.split()).strip()
