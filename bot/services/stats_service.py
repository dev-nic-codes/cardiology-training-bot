from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database.models import ECGCase, PostedArticle, PostedPodcast, PostedQuiz, UserAnswer
from bot.services.cache_service import STATE_LAST_CACHE_FILL_AT
from bot.services.article_service import STATE_LAST_ARTICLE_POSTED_AT
from bot.services.podcast_service import STATE_LAST_PODCAST_POSTED_AT
from bot.services.state_store import StateStore

STATE_LAST_ECG_POSTED_AT = "last_ecg_posted_at"


@dataclass(slots=True)
class ScoreSummary:
    total_answers: int
    correct_answers: int
    incorrect_answers: int
    accuracy_percent: float


@dataclass(slots=True)
class WrongAnswerItem:
    answered_at: datetime
    vignette: str
    diagnosis: str
    source_url: str


@dataclass(slots=True)
class UserScoreSummary:
    user_id: int
    total_answers: int
    correct_answers: int
    incorrect_answers: int
    accuracy_percent: float


@dataclass(slots=True)
class BotStatus:
    total_cases: int
    unused_cases: int
    used_cases: int
    posted_quizzes: int
    posted_articles: int
    posted_podcasts: int
    total_answers: int
    last_cache_fill_at: datetime | None
    last_ecg_posted_at: datetime | None
    last_article_posted_at: datetime | None
    last_podcast_posted_at: datetime | None


class StatsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        state_store: StateStore,
    ) -> None:
        self.session_factory = session_factory
        self.state_store = state_store

    async def get_score(self, *, user_id: int | None = None) -> ScoreSummary:
        async with self.session_factory() as session:
            answers = list(await session.scalars(self._answers_query(user_id=user_id)))

        total = len(answers)
        correct = sum(1 for answer in answers if answer.is_correct)
        incorrect = total - correct
        accuracy = (correct / total * 100) if total else 0.0
        return ScoreSummary(
            total_answers=total,
            correct_answers=correct,
            incorrect_answers=incorrect,
            accuracy_percent=accuracy,
        )

    async def get_recent_wrong_answers(self, *, user_id: int | None = None, limit: int = 10) -> list[WrongAnswerItem]:
        async with self.session_factory() as session:
            query = (
                select(UserAnswer, ECGCase)
                .join(PostedQuiz, PostedQuiz.poll_id == UserAnswer.poll_id)
                .join(ECGCase, ECGCase.id == PostedQuiz.ecg_case_id)
                .where(UserAnswer.is_correct.is_(False))
                .order_by(UserAnswer.created_at.desc())
                .limit(limit)
            )
            if user_id is not None:
                query = query.where(UserAnswer.user_id == user_id)

            rows = (await session.execute(query)).all()

        return [
            WrongAnswerItem(
                answered_at=user_answer.created_at,
                vignette=case.vignette,
                diagnosis=case.diagnosis,
                source_url=case.source_url,
            )
            for user_answer, case in rows
        ]

    async def get_all_user_scores(self) -> list[UserScoreSummary]:
        async with self.session_factory() as session:
            answers = list(await session.scalars(select(UserAnswer).order_by(UserAnswer.user_id.asc(), UserAnswer.created_at.asc())))

        grouped: dict[int, list[UserAnswer]] = {}
        for answer in answers:
            grouped.setdefault(answer.user_id, []).append(answer)

        summaries: list[UserScoreSummary] = []
        for user_id, user_answers in grouped.items():
            total = len(user_answers)
            correct = sum(1 for answer in user_answers if answer.is_correct)
            incorrect = total - correct
            accuracy = (correct / total * 100) if total else 0.0
            summaries.append(
                UserScoreSummary(
                    user_id=user_id,
                    total_answers=total,
                    correct_answers=correct,
                    incorrect_answers=incorrect,
                    accuracy_percent=accuracy,
                )
            )

        summaries.sort(key=lambda item: (-item.total_answers, item.user_id))
        return summaries

    async def get_status(self) -> BotStatus:
        async with self.session_factory() as session:
            total_cases = len((await session.scalars(select(ECGCase.id))).all())
            unused_cases = len((await session.scalars(select(ECGCase.id).where(ECGCase.used.is_(False)))).all())
            posted_quizzes = len((await session.scalars(select(PostedQuiz.id))).all())
            posted_articles = len((await session.scalars(select(PostedArticle.id))).all())
            posted_podcasts = len((await session.scalars(select(PostedPodcast.id))).all())
            total_answers = len((await session.scalars(select(UserAnswer.id))).all())

        return BotStatus(
            total_cases=total_cases,
            unused_cases=unused_cases,
            used_cases=total_cases - unused_cases,
            posted_quizzes=posted_quizzes,
            posted_articles=posted_articles,
            posted_podcasts=posted_podcasts,
            total_answers=total_answers,
            last_cache_fill_at=await self.state_store.get_datetime(STATE_LAST_CACHE_FILL_AT),
            last_ecg_posted_at=await self.state_store.get_datetime(STATE_LAST_ECG_POSTED_AT),
            last_article_posted_at=await self.state_store.get_datetime(STATE_LAST_ARTICLE_POSTED_AT),
            last_podcast_posted_at=await self.state_store.get_datetime(STATE_LAST_PODCAST_POSTED_AT),
        )

    def _answers_query(self, *, user_id: int | None = None):
        query = select(UserAnswer)
        if user_id is not None:
            query = query.where(UserAnswer.user_id == user_id)
        return query
