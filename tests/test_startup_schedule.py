from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from bot.main import startup_post_is_due


class StartupScheduleTests(unittest.TestCase):
    def test_missing_last_post_is_due(self) -> None:
        self.assertTrue(startup_post_is_due(None, 4))

    def test_recent_post_is_not_due(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        self.assertFalse(startup_post_is_due(now - timedelta(hours=1), 4, now=now))

    def test_elapsed_interval_is_due(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        self.assertTrue(startup_post_is_due(now - timedelta(hours=4), 4, now=now))

    def test_naive_database_timestamp_is_treated_as_utc(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        previous = datetime(2026, 7, 30, 8, 0)
        self.assertTrue(startup_post_is_due(previous, 4, now=now))


if __name__ == "__main__":
    unittest.main()
