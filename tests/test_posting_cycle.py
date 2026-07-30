from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.posting_service import ECGPostingService, PostResult


class PostingCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_cache_starts_a_new_training_cycle(self) -> None:
        service = object.__new__(ECGPostingService)
        service._post_lock = asyncio.Lock()
        service.cache_service = SimpleNamespace(
            fill_cache_if_needed=AsyncMock(
                side_effect=[None, SimpleNamespace(stored=0)]
            ),
            count_total_cases=AsyncMock(return_value=547),
            reset_usage_cycle=AsyncMock(),
        )
        service.topic_manager = SimpleNamespace(get_topic_id=AsyncMock(return_value=101))
        service._release_pending_unanswered_explanations = AsyncMock()
        case = SimpleNamespace(id=1, rownum=1)
        service._load_candidate_cases = AsyncMock(side_effect=[[], [case]])
        expected = PostResult(
            case_id=1,
            poll_id="poll-1",
            diagnosis="Test",
            source_url="https://example.test/case",
        )
        service._post_case_candidate = AsyncMock(return_value=expected)
        service._find_cached_case_media_path = lambda candidate: None

        result = await service.post_next_case(SimpleNamespace())

        self.assertEqual(result, expected)
        service.cache_service.reset_usage_cycle.assert_awaited_once_with()
        self.assertEqual(service._load_candidate_cases.await_count, 2)


if __name__ == "__main__":
    unittest.main()
