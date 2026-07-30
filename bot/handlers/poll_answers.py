from __future__ import annotations

from aiogram import Bot, Router
from aiogram.types import PollAnswer

from bot.config import Settings
from bot.services.posting_service import ECGPostingService


def build_poll_router(settings: Settings, posting_service: ECGPostingService) -> Router:
    router = Router(name="poll_answers")

    @router.poll_answer()
    async def poll_answer_handler(poll_answer: PollAnswer, bot: Bot) -> None:
        if not poll_answer.option_ids:
            return

        await posting_service.handle_poll_answer(
            bot,
            poll_id=poll_answer.poll_id,
            user_id=poll_answer.user.id,
            selected_option=poll_answer.option_ids[0],
        )

    return router
