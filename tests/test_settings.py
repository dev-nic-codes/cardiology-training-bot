from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_owner_is_required_and_must_be_positive(self) -> None:
        values = {
            "BOT_TOKEN": "example-token",
            "CHAT_ID": -1001234567890,
            "ECG_TOPIC_ID": 101,
            "ARTICLES_TOPIC_ID": 202,
            "PODCAST_TOPIC_ID": 303,
        }
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, **values)
        with self.assertRaises(ValidationError):
            build_settings(OWNER_USER_ID=0)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not available on Windows")
    def test_runtime_directories_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            settings = build_settings(
                DATABASE_URL=f"sqlite+aiosqlite:///{(base_dir / 'data' / 'test.sqlite3').as_posix()}"
            )
            with patch("bot.config.BASE_DIR", base_dir):
                settings.ensure_runtime_directories()

            for name in ("data", "logs", "downloads"):
                self.assertEqual(stat.S_IMODE((base_dir / name).stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
