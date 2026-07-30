from __future__ import annotations

from bot.services.state_store import StateStore

STATE_ECG_TOPIC_ID = "ecg_topic_id"
STATE_ARTICLES_TOPIC_ID = "articles_topic_id"
STATE_PODCAST_TOPIC_ID = "podcast_topic_id"


class TopicManager:
    def __init__(self, state_store: StateStore, default_topic_id: int, *, state_key: str = STATE_ECG_TOPIC_ID) -> None:
        self.state_store = state_store
        self.default_topic_id = default_topic_id
        self.state_key = state_key

    async def get_topic_id(self) -> int:
        stored = await self.state_store.get_int(self.state_key)
        if stored is not None:
            return stored

        await self.state_store.set_int(self.state_key, self.default_topic_id)
        return self.default_topic_id

    async def set_topic_id(self, topic_id: int) -> None:
        await self.state_store.set_int(self.state_key, topic_id)
