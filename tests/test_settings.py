from __future__ import annotations

import unittest

from pydantic import ValidationError

from bot.config import Settings


def build_settings(**overrides) -> Settings:
    values = {
        "BOT_TOKEN": "example-token",
        "CHAT_ID": -1001234567890,
        "ECG_TOPIC_ID": 101,
        "ARTICLES_TOPIC_ID": 202,
        "PODCAST_TOPIC_ID": 303,
        "OWNER_USER_ID": 123456789,
        "AI_SUMMARY_ENABLED": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class SettingsTests(unittest.TestCase):
    def test_safe_defaults_are_valid(self) -> None:
        settings = build_settings()
        self.assertEqual(settings.timezone, "UTC")
        self.assertEqual(settings.post_interval_hours, 3)

    def test_nonpositive_post_interval_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            build_settings(POST_INTERVAL_HOURS=0)

    def test_empty_bot_token_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            build_settings(BOT_TOKEN=" ")


if __name__ == "__main__":
    unittest.main()
