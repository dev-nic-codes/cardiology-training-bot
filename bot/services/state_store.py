from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database.models import BotState


class StateStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get(self, key: str) -> str | None:
        async with self.session_factory() as session:
            state = await session.get(BotState, key)
            return state.value if state else None

    async def set(self, key: str, value: str) -> None:
        async with self.session_factory() as session:
            state = await session.get(BotState, key)
            if state is None:
                state = BotState(key=key, value=value)
                session.add(state)
            else:
                state.value = value
                state.updated_at = datetime.now(UTC)
            await session.commit()

    async def get_int(self, key: str) -> int | None:
        value = await self.get(key)
        if value is None:
            return None
        return int(value)

    async def set_int(self, key: str, value: int) -> None:
        await self.set(key, str(value))

    async def get_datetime(self, key: str) -> datetime | None:
        value = await self.get(key)
        if value is None:
            return None
        return datetime.fromisoformat(value)

    async def set_datetime(self, key: str, value: datetime) -> None:
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        await self.set(key, normalized.astimezone(UTC).isoformat())
