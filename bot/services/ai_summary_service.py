from __future__ import annotations

import logging
from typing import Any

import httpx

from api.article_sources import clean_text, looks_like_navigation_noise

LOGGER = logging.getLogger(__name__)


class AISummaryService:
    def __init__(
        self,
        *,
        enabled: bool,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float,
        max_input_chars: int,
    ) -> None:
        self.enabled = enabled and bool((api_key or "").strip())
        self.model = model
        self.max_input_chars = max_input_chars
        self._client: httpx.AsyncClient | None = None

        if not self.enabled:
            return

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {(api_key or '').strip()}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def summarize_article(
        self,
        *,
        title: str,
        journal: str,
        url: str,
        source_text: str,
    ) -> str:
        if not self.enabled or self._client is None:
            return ""

        cleaned_source = clean_text(source_text)
        if not cleaned_source or looks_like_navigation_noise(cleaned_source):
            return ""

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You summarize cardiology journal articles for a private Telegram feed. "
                        "Write 2 to 3 plain factual sentences in clear medical English. "
                        "Do not use bullets, markdown, title repetition, journal names, navigation text, "
                        "or introductory phrases. Summarize only the supplied article text. "
                        "If the supplied text is too noisy, too thin, or not article content, reply with exactly SKIP."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Title: {clean_text(title)}\n"
                        f"Journal: {clean_text(journal)}\n"
                        f"URL: {clean_text(url)}\n\n"
                        "Article text to summarize:\n"
                        f"{cleaned_source[: self.max_input_chars]}"
                    ),
                },
            ],
            "max_completion_tokens": 220,
        }

        try:
            response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
            response_data = response.json()
        except Exception as exc:  # pragma: no cover - network/API variability
            LOGGER.warning("AI article summarization failed for %s: %s", clean_text(title), exc)
            return ""

        content = self._extract_content(response_data)
        cleaned_summary = self._clean_output(content)
        if not cleaned_summary or cleaned_summary == "SKIP":
            return ""
        if looks_like_navigation_noise(cleaned_summary):
            return ""

        LOGGER.info('Using AI summary rewrite for article "%s".', clean_text(title))
        return cleaned_summary

    def _extract_content(self, response_data: dict[str, Any]) -> str:
        choices = response_data.get("choices") or []
        if not choices:
            return ""

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
            return " ".join(text_parts).strip()

        return ""

    def _clean_output(self, value: str) -> str:
        cleaned = clean_text(value).strip(' "\'')
        if not cleaned:
            return ""
        if cleaned.casefold() == "skip":
            return "SKIP"
        return cleaned
