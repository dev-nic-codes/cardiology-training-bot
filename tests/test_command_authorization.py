from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import Settings
from bot.handlers.commands import build_commands_router


def build_settings() -> Settings:
    return Settings(
        _env_file=None,
        BOT_TOKEN="example-token",
        CHAT_ID=-1001234567890,
        ECG_TOPIC_ID=101,
        ARTICLES_TOPIC_ID=202,
        PODCAST_TOPIC_ID=303,
        OWNER_USER_ID=123456789,
        AI_SUMMARY_ENABLED=False,
    )


class CommandAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = build_settings()
        self.posting_service = SimpleNamespace(post_next_case=AsyncMock())
        self.router = build_commands_router(
            settings=self.settings,
            posting_service=self.posting_service,
            article_posting_service=SimpleNamespace(),
            podcast_posting_service=SimpleNamespace(),
            stats_service=SimpleNamespace(),
            scheduler_service=SimpleNamespace(),
            cache_service=SimpleNamespace(),
            topic_manager=SimpleNamespace(),
            article_topic_manager=SimpleNamespace(),
            podcast_topic_manager=SimpleNamespace(),
        )
        self.handlers = {
            handler.callback.__name__: handler.callback
            for handler in self.router.message.handlers
        }

    def unauthorized_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            from_user=SimpleNamespace(id=987654321),
            chat=SimpleNamespace(id=self.settings.chat_id),
            message_thread_id=None,
            answer=AsyncMock(),
        )

    async def test_next_rejects_non_owner_before_posting(self) -> None:
        message = self.unauthorized_message()

        await self.handlers["next_command"](message, SimpleNamespace())

        message.answer.assert_awaited_once_with("🚫 This command is restricted to the configured owner.")
        self.posting_service.post_next_case.assert_not_awaited()

    async def test_stats_rejects_non_owner(self) -> None:
        message = self.unauthorized_message()

        await self.handlers["stats_command"](message)

        message.answer.assert_awaited_once_with("🚫 This command is restricted to the configured owner.")


if __name__ == "__main__":
    unittest.main()
