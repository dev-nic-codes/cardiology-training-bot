from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = BASE_DIR / "data" / "ecg_bot.sqlite3"
DEFAULT_DATABASE_URL = f"sqlite+aiosqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: str = Field(validation_alias="BOT_TOKEN")
    chat_id: int = Field(validation_alias="CHAT_ID")
    ecg_topic_id: int = Field(validation_alias="ECG_TOPIC_ID")
    articles_topic_id: int = Field(default=197, validation_alias="ARTICLES_TOPIC_ID")
    podcast_topic_id: int = Field(default=194, validation_alias="PODCAST_TOPIC_ID")
    owner_user_id: int | None = Field(default=None, validation_alias="OWNER_USER_ID")
    database_url: str = Field(
        default=DEFAULT_DATABASE_URL,
        validation_alias="DATABASE_URL",
    )
    timezone: str = Field(default="Europe/Sofia", validation_alias="TIMEZONE")
    post_interval_hours: int = Field(default=3, validation_alias="POST_INTERVAL_HOURS")
    article_post_interval_hours: int = Field(default=2, validation_alias="ARTICLE_POST_INTERVAL_HOURS")
    podcast_post_interval_hours: int = Field(default=168, validation_alias="PODCAST_POST_INTERVAL_HOURS")
    cache_refill_threshold: int = Field(default=5, validation_alias="CACHE_REFILL_THRESHOLD")
    cache_batch_size: int = Field(default=10, validation_alias="CACHE_BATCH_SIZE")
    scrape_delay_seconds: float = Field(default=1.5, validation_alias="SCRAPE_DELAY_SECONDS")
    scrape_timeout_seconds: float = Field(default=30.0, validation_alias="SCRAPE_TIMEOUT_SECONDS")
    scrape_bootstrap_training_code: str = Field(
        default="O",
        validation_alias="SCRAPE_BOOTSTRAP_TRAINING_CODE",
    )
    scrape_bootstrap_goal_code: str = Field(
        default="E",
        validation_alias="SCRAPE_BOOTSTRAP_GOAL_CODE",
    )
    wave_maven_base_url: str = Field(
        default="https://ecg.bidmc.harvard.edu/maven/",
        validation_alias="WAVE_MAVEN_BASE_URL",
    )
    ai_summary_enabled: bool = Field(default=True, validation_alias="AI_SUMMARY_ENABLED")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", validation_alias="OPENAI_BASE_URL")
    ai_summary_model: str = Field(default="gpt-5.4-mini", validation_alias="AI_SUMMARY_MODEL")
    ai_summary_max_input_chars: int = Field(default=6000, validation_alias="AI_SUMMARY_MAX_INPUT_CHARS")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    user_agent: str = Field(default=DEFAULT_USER_AGENT, validation_alias="USER_AGENT")
    wrong_answer_limit: int = Field(default=10, validation_alias="WRONG_ANSWER_LIMIT")

    @field_validator("bot_token")
    @classmethod
    def validate_bot_token(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("BOT_TOKEN is required.")
        return value

    @field_validator("post_interval_hours")
    @classmethod
    def validate_post_interval_hours(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("POST_INTERVAL_HOURS must be greater than zero.")
        return value

    @field_validator("article_post_interval_hours")
    @classmethod
    def validate_article_post_interval_hours(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("ARTICLE_POST_INTERVAL_HOURS must be greater than zero.")
        return value

    @field_validator("podcast_post_interval_hours")
    @classmethod
    def validate_podcast_post_interval_hours(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PODCAST_POST_INTERVAL_HOURS must be greater than zero.")
        return value

    @field_validator("cache_refill_threshold")
    @classmethod
    def validate_cache_refill_threshold(cls, value: int) -> int:
        if value < 1:
            raise ValueError("CACHE_REFILL_THRESHOLD must be at least 1.")
        return value

    @field_validator("cache_batch_size")
    @classmethod
    def validate_cache_batch_size(cls, value: int) -> int:
        if value < 4:
            raise ValueError("CACHE_BATCH_SIZE must be at least 4.")
        return value

    @field_validator("scrape_delay_seconds")
    @classmethod
    def validate_scrape_delay_seconds(cls, value: float) -> float:
        if value < 0:
            raise ValueError("SCRAPE_DELAY_SECONDS cannot be negative.")
        return value

    @field_validator("scrape_timeout_seconds")
    @classmethod
    def validate_scrape_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("SCRAPE_TIMEOUT_SECONDS must be greater than zero.")
        return value

    @field_validator("ai_summary_max_input_chars")
    @classmethod
    def validate_ai_summary_max_input_chars(cls, value: int) -> int:
        if value < 500:
            raise ValueError("AI_SUMMARY_MAX_INPUT_CHARS must be at least 500.")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in logging.getLevelNamesMapping():
            raise ValueError(f"Unsupported LOG_LEVEL: {value}")
        return normalized

    def ensure_runtime_directories(self) -> None:
        (BASE_DIR / "data").mkdir(exist_ok=True)
        (BASE_DIR / "logs").mkdir(exist_ok=True)
        (BASE_DIR / "downloads").mkdir(exist_ok=True)

        if self.database_url.startswith("sqlite"):
            parsed = urlparse(self.database_url)
            raw_path = unquote(parsed.path)
            if raw_path.startswith("/") and len(raw_path) > 3 and raw_path[2] == ":":
                raw_path = raw_path[1:]
            Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    @property
    def logs_path(self) -> Path:
        return BASE_DIR / "logs" / "ecg_bot.log"
