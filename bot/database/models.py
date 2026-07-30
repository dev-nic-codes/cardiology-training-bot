from __future__ import annotations

from datetime import datetime

from sqlalchemy import BIGINT, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.base import Base


class ECGCase(Base):
    __tablename__ = "ecg_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_url: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    image_url: Mapped[str] = mapped_column(String(500))
    vignette: Mapped[str] = mapped_column(Text)
    diagnosis: Mapped[str] = mapped_column(Text, index=True)
    explanation: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text)
    correct_option_index: Mapped[int] = mapped_column(Integer)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    rownum: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    case_identifier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_options_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    posted_quizzes: Mapped[list["PostedQuiz"]] = relationship(back_populates="ecg_case")


class PostedQuiz(Base):
    __tablename__ = "posted_quizzes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ecg_case_id: Mapped[int] = mapped_column(ForeignKey("ecg_cases.id", ondelete="CASCADE"), index=True)
    poll_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(BIGINT)
    topic_id: Mapped[int] = mapped_column(Integer)
    explanation_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    ecg_case: Mapped[ECGCase] = relationship(back_populates="posted_quizzes")
    answers: Mapped[list["UserAnswer"]] = relationship(back_populates="posted_quiz")


class UserAnswer(Base):
    __tablename__ = "user_answers"
    __table_args__ = (UniqueConstraint("poll_id", "user_id", name="uq_user_answers_poll_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[str] = mapped_column(ForeignKey("posted_quizzes.poll_id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BIGINT, index=True)
    selected_option: Mapped[int] = mapped_column(Integer)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    posted_quiz: Mapped[PostedQuiz] = relationship(back_populates="answers")


class BotState(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PostedArticle(Base):
    __tablename__ = "posted_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unique_id: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chat_id: Mapped[int] = mapped_column(BIGINT)
    topic_id: Mapped[int] = mapped_column(Integer)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class PostedPodcast(Base):
    __tablename__ = "posted_podcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unique_id: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    audio_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chat_id: Mapped[int] = mapped_column(BIGINT)
    topic_id: Mapped[int] = mapped_column(Integer)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
