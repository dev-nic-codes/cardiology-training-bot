from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.database.base import Base


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, future=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_apply_runtime_schema_updates)


def _apply_runtime_schema_updates(connection) -> None:
    inspector = inspect(connection)
    if "posted_quizzes" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("posted_quizzes")}
    if "explanation_sent_at" in column_names:
        return

    dialect_name = connection.dialect.name.casefold()
    if dialect_name == "postgresql":
        connection.execute(text("ALTER TABLE posted_quizzes ADD COLUMN explanation_sent_at TIMESTAMP WITH TIME ZONE"))
        return

    connection.execute(text("ALTER TABLE posted_quizzes ADD COLUMN explanation_sent_at DATETIME"))
