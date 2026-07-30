from __future__ import annotations

import asyncio
from collections import deque
import html
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import quote, urljoin, urlparse, urlunsplit

import cloudscraper
import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional dependency during bootstrap
    curl_requests = None

from api.article_sources import (
    ArticleCandidate,
    build_article_sources,
    extract_background_from_html,
    infer_pdf_url_from_article_url,
    limit_text,
    looks_like_navigation_noise,
    reconstruct_openalex_abstract,
    summarize_article_text,
)
from bot.database.models import PostedArticle
from bot.services.ai_summary_service import AISummaryService
from bot.services.state_store import StateStore
from bot.services.topic_manager import TopicManager

LOGGER = logging.getLogger(__name__)

STATE_LAST_ARTICLE_POSTED_AT = "last_article_posted_at"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
PDF_ACCEPT = "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
NON_ARTICLE_PDF_MARKERS: tuple[str, ...] = (
    "supplementary",
    "supplemental",
    "supplementary-material",
    "supplemental-material",
    "supporting-information",
    "supporting_info",
    "supportinginfo",
    "data-supplement",
    "datasupplement",
    "suppl-data",
    "suppl_file",
    "suppl-file",
    "suppl_material",
    "suppl-material",
    "suppl_appendix",
    "suppl-appendix",
    "downloadsupplement",
    "eappendix",
    "esupplement",
    "appendix",
    "appendices",
    "checklist",
    "peer-review",
    "peer_review",
    "reviewer-response",
    "reviewer_response",
    "response-to-reviewers",
    "supporting-information",
    "video",
    "audio",
    "multimedia",
)


@dataclass(slots=True)
class ArticlePostResult:
    unique_id: str
    title: str
    source: str
    url: str
    pdf_attached: bool


@dataclass(slots=True)
class PageFetchResult:
    url: str
    final_url: str
    html: str


@dataclass(slots=True)
class PdfFetchResult:
    filename: str
    content: bytes
    source_url: str


class ArticlePostingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        state_store: StateStore,
        topic_manager: TopicManager,
        *,
        chat_id: int,
        user_agent: str,
        timeout_seconds: float,
        legacy_db_path: Path,
        ai_summary_service: AISummaryService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.state_store = state_store
        self.topic_manager = topic_manager
        self.chat_id = chat_id
        self.legacy_db_path = legacy_db_path
        self.sources = build_article_sources()
        self.timeout_seconds = timeout_seconds
        self.ai_summary_service = ai_summary_service
        self._http_headers = {
            "User-Agent": user_agent,
            "Accept": DEFAULT_ACCEPT,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
        }
        self._pdf_headers = {
            **self._http_headers,
            "Accept": PDF_ACCEPT,
        }
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_seconds,
            headers=self._http_headers,
        )
        self._legacy_ids: set[str] | None = None
        self._scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self._curl_session = curl_requests.Session() if curl_requests is not None else None

    async def close(self) -> None:
        await self._client.aclose()
        if self._curl_session is not None:
            self._curl_session.close()
        if self.ai_summary_service is not None:
            await self.ai_summary_service.close()

    async def post_next_article(self, bot: Bot) -> ArticlePostResult | None:
        LOGGER.info("Checking article sources for the next unseen article.")
        current_ids = await self._load_current_posted_ids()
        legacy_ids = self._load_legacy_posted_ids()

        candidates: list[ArticleCandidate] = []
        successful_sources = 0
        source_failures: list[str] = []
        for source in self.sources:
            try:
                source_candidates = await source.fetch(self._client)
                successful_sources += 1
                LOGGER.info(
                    "Fetched %s article candidates from %s.",
                    len(source_candidates),
                    getattr(source, "name", type(source).__name__),
                )
                candidates.extend(source_candidates)
            except Exception as exc:  # pragma: no cover - network/source failure path
                source_name = getattr(source, "name", type(source).__name__)
                source_failures.append(source_name)
                if self._is_expected_source_fetch_failure(exc):
                    LOGGER.info("Article source unavailable | %s: %s", source_name, exc)
                else:
                    LOGGER.warning("Article source fetch failed | %s: %s", source_name, exc)

        if successful_sources == 0 and source_failures:
            raise RuntimeError(
                "No article sources could be fetched successfully: " + ", ".join(sorted(source_failures))
            )

        deduplicated = deduplicate_article_candidates(candidates)
        deduplicated.sort(key=article_sort_key, reverse=True)

        next_candidate = next(
            (
                candidate
                for candidate in deduplicated
                if candidate.unique_id not in current_ids
                and candidate.unique_id not in legacy_ids
            ),
            None,
        )
        if next_candidate is None:
            LOGGER.info("No unseen article was found across configured sources.")
            return None

        page_result = await self._fetch_article_page(next_candidate.url)
        next_candidate.summary = await self._resolve_summary(next_candidate, page_result=page_result)

        topic_id = await self.topic_manager.get_topic_id()
        LOGGER.info(
            'Posting article "%s" from %s to topic_id=%s.',
            next_candidate.title,
            next_candidate.source,
            topic_id,
        )
        message = await bot.send_message(
            chat_id=self.chat_id,
            message_thread_id=topic_id,
            text=self._build_article_message(next_candidate),
            disable_web_page_preview=True,
        )
        pdf_attached = await self._try_send_article_pdf(
            bot,
            topic_id,
            next_candidate,
            page_result=page_result,
            article_message_id=message.message_id,
        )

        async with self.session_factory() as session:
            session.add(
                PostedArticle(
                    unique_id=next_candidate.unique_id,
                    title=next_candidate.title,
                    source=next_candidate.source,
                    url=next_candidate.url,
                    summary=next_candidate.summary,
                    published_at=next_candidate.published_at,
                    chat_id=self.chat_id,
                    topic_id=topic_id,
                    telegram_message_id=message.message_id,
                )
            )
            await session.commit()

        await self.state_store.set_datetime(STATE_LAST_ARTICLE_POSTED_AT, datetime.now(UTC))
        LOGGER.info('Posted article "%s" to topic_id=%s.', next_candidate.title, topic_id)
        return ArticlePostResult(
            unique_id=next_candidate.unique_id,
            title=next_candidate.title,
            source=next_candidate.source,
            url=next_candidate.url,
            pdf_attached=pdf_attached,
        )

    def _is_expected_source_fetch_failure(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {401, 403, 404, 429}

        message = str(exc).casefold()
        return "status=403" in message or "forbidden" in message or "status=429" in message

    async def count_posted_articles(self) -> int:
        async with self.session_factory() as session:
            return len((await session.scalars(select(PostedArticle.id))).all())

    async def _load_current_posted_ids(self) -> set[str]:
        async with self.session_factory() as session:
            rows = await session.scalars(select(PostedArticle.unique_id))
            return set(rows.all())

    def _load_legacy_posted_ids(self) -> set[str]:
        if self._legacy_ids is not None:
            return self._legacy_ids

        legacy_ids: set[str] = set()
        if self.legacy_db_path.exists():
            try:
                connection = sqlite3.connect(self.legacy_db_path)
                cursor = connection.cursor()
                for row in cursor.execute("select unique_id, url from posted_items where item_type = 'article'"):
                    for value in row:
                        if value:
                            legacy_ids.add(value)
                connection.close()
            except sqlite3.Error as exc:  # pragma: no cover - legacy db failure path
                LOGGER.warning("Failed to read legacy article history from %s: %s", self.legacy_db_path, exc)

        self._legacy_ids = legacy_ids
        LOGGER.info("Loaded %s legacy article ids for dedupe.", len(self._legacy_ids))
        return self._legacy_ids

    def _build_article_message(self, candidate: ArticleCandidate) -> str:
        publication_date = self._format_publication_date(candidate.published_at)
        article_url = html.escape(candidate.url, quote=True)
        lines = [
            f"&#129728; <b>{html.escape(self._normalize_text(candidate.title))}</b>",
            "",
            f"&#128218; <b>Journal:</b> {html.escape(self._normalize_text(candidate.source))}",
            "",
            f"&#128197; <b>Publication date:</b> {html.escape(publication_date)}",
            "",
            f"&#128279; <b>Article link:</b> <a href=\"{article_url}\">Open article</a>",
        ]
        if candidate.summary:
            lines.extend(
                [
                    "",
                    "&#128221; <b>Background:</b>",
                    html.escape(candidate.summary),
                ]
            )
        return "\n".join(lines)

    async def _try_send_article_pdf(
        self,
        bot: Bot,
        topic_id: int,
        candidate: ArticleCandidate,
        *,
        page_result: PageFetchResult | None,
        article_message_id: int | None,
    ) -> bool:
        pdf_result = await self._download_article_pdf(candidate, page_result=page_result)
        if pdf_result is None:
            return False

        try:
            await bot.send_document(
                chat_id=self.chat_id,
                message_thread_id=topic_id,
                document=BufferedInputFile(pdf_result.content, filename=pdf_result.filename),
                caption="&#128196; <b>PDF version</b>",
                reply_to_message_id=article_message_id,
            )
            LOGGER.info('Attached PDF for article "%s" from %s.', candidate.title, pdf_result.source_url)
            return True
        except Exception as exc:  # pragma: no cover - Telegram upload variability
            LOGGER.warning('Failed to upload PDF for article "%s": %s', candidate.title, exc)
            return False

    async def _download_article_pdf(
        self,
        candidate: ArticleCandidate,
        *,
        page_result: PageFetchResult | None,
    ) -> PdfFetchResult | None:
        candidate_urls = self._candidate_pdf_urls(candidate, page_result=page_result)
        article_referer = page_result.final_url if page_result else candidate.url
        pending: deque[str] = deque(candidate_urls)
        attempted: set[str] = set()
        open_access_checked = False

        while pending:
            pdf_url = pending.popleft()
            if pdf_url in attempted:
                continue
            attempted.add(pdf_url)

            result = await self._fetch_pdf_with_httpx(pdf_url, referer=article_referer)
            if result is not None:
                return result

            result = await self._fetch_pdf_with_cloudscraper(pdf_url, referer=article_referer)
            if result is not None:
                return result

            result = await self._fetch_pdf_with_curl(pdf_url, referer=article_referer)
            if result is not None:
                return result

            expanded_page = await self._fetch_html_document(pdf_url, referer=article_referer)
            if expanded_page is not None:
                self._extend_unique_urls(
                    pending,
                    attempted,
                    self._build_pdf_url_variants(expanded_page.final_url),
                )
                self._extend_unique_urls(
                    pending,
                    attempted,
                    self._extract_pdf_urls_from_page(expanded_page.html, expanded_page.final_url),
                )

            if not pending and not open_access_checked:
                open_access_checked = True
                self._extend_unique_urls(
                    pending,
                    attempted,
                    await self._lookup_open_access_pdf_urls(candidate, page_result=page_result),
                )

        LOGGER.info('No accessible PDF could be downloaded for article "%s".', candidate.title)
        return None

    def _candidate_pdf_urls(self, candidate: ArticleCandidate, *, page_result: PageFetchResult | None) -> list[str]:
        raw_urls: list[str] = []

        def add(value: str | None) -> None:
            normalized = self._normalize_candidate_url(value)
            if not normalized:
                return
            if self._looks_like_non_article_pdf(normalized):
                return
            if normalized in raw_urls:
                return
            raw_urls.append(normalized)

        add(candidate.pdf_url)
        add(infer_pdf_url_from_article_url(candidate.url))

        if page_result is not None:
            add(infer_pdf_url_from_article_url(page_result.final_url))
            for extracted in self._extract_pdf_urls_from_page(page_result.html, page_result.final_url):
                add(extracted)

        urls: list[str] = []
        for value in raw_urls:
            for variant in self._build_pdf_url_variants(value):
                if variant not in urls:
                    urls.append(variant)

        return urls

    def _extract_pdf_urls_from_page(self, html_text: str, base_url: str) -> list[str]:
        soup = BeautifulSoup(html_text, "html.parser")
        urls: list[str] = []

        def add(value: str | None, *, context: str = "") -> None:
            normalized = self._normalize_candidate_url(value)
            if not normalized:
                return
            absolute = self._normalize_candidate_url(urljoin(base_url, normalized))
            if not self._is_plausible_pdf_candidate(absolute):
                return
            if self._looks_like_non_article_pdf(absolute, context=context):
                return
            if absolute not in urls:
                urls.append(absolute)

        for meta in soup.select("meta[name='citation_pdf_url'], meta[property='citation_pdf_url']"):
            add(meta.get("content"))

        for element in soup.select("a[href], link[href]"):
            href = element.get("href", "")
            text = element.get_text(" ", strip=True)
            title_attr = str(element.get("title", "") or "")
            context = f"{text} {title_attr}".strip()
            lowered = href.lower()
            if (
                lowered.endswith(".pdf")
                or "/pdf/" in lowered
                or "pdfft" in lowered
                or "downloadpdf" in lowered
                or "pdf" in text.lower()
                or ("download" in text.lower() and "pdf" in text.lower())
            ):
                add(href, context=context)

        for element in soup.select("iframe[src], embed[src], source[src]"):
            add(element.get("src"))
        for element in soup.select("object[data]"):
            add(element.get("data"))

        for element in soup.select("[data-doi]"):
            doi = element.get("data-doi")
            if doi and "ahajournals.org" in urlparse(base_url).netloc.lower():
                add(f"/doi/pdf/{doi}?download=true")
                add(f"/doi/epdf/{doi}")

        text = soup.get_text(" ", strip=True)
        for script in soup.select("script"):
            payload = script.string or script.get_text(" ", strip=True)
            if not payload:
                continue
            for pattern in (
                r'"(?:citation_pdf_url|pdfUrl|pdfURL|pdfPath|downloadPdfUrl|pdfDownloadUrl|exportPdfUrl|pdf_link)"\s*:\s*"([^"]+)"',
                r"'(?:citation_pdf_url|pdfUrl|pdfURL|pdfPath|downloadPdfUrl|pdfDownloadUrl|exportPdfUrl|pdf_link)'\s*:\s*'([^']+)'",
            ):
                for match in re.findall(pattern, payload, re.IGNORECASE):
                    add(match.replace("\\/", "/"))
        for match in re.findall(r"https?://[^\s\"']+\.pdf(?:\?[^\s\"']*)?", html_text, re.IGNORECASE):
            add(match)
        for match in re.findall(r"/[^\s\"']*(?:pdf|pdfft|epdf|downloadpdf)[^\s\"']*", html_text, re.IGNORECASE):
            add(match)

        if "download pdf" in text.casefold() and "ahajournals.org" in urlparse(base_url).netloc.lower():
            inferred = infer_pdf_url_from_article_url(base_url)
            if inferred:
                add(inferred.replace("/doi/pdf/", "/doi/pdf/") + ("&download=true" if "?" in inferred else "?download=true"))

        return urls

    def _is_plausible_pdf_candidate(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False

        host = parsed.netloc.lower()
        if host in {
            "www.googletagmanager.com",
            "googletagmanager.com",
            "www.google-analytics.com",
            "google-analytics.com",
        }:
            return False

        path = parsed.path.lower()
        if path.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf")):
            return False

        return True

    def _looks_like_non_article_pdf(self, url: str, *, context: str = "", filename: str = "") -> bool:
        parsed = urlparse(url or "")
        lowered_path = parsed.path.casefold()
        lowered_query = parsed.query.casefold()
        if "/action/downloadsupplement" in lowered_path:
            return True
        if "file=" in lowered_query and any(marker in lowered_query for marker in ("checklist", "supplement", "appendix")):
            return True

        haystack = " ".join(
            part
            for part in (
                unquote(url or ""),
                unquote(context or ""),
                unquote(filename or ""),
            )
            if part
        ).casefold()
        if not haystack:
            return False

        for marker in NON_ARTICLE_PDF_MARKERS:
            if marker in haystack:
                return True
        return False

    def _normalize_candidate_url(self, value: str | None) -> str:
        normalized = (value or "").strip().replace("&amp;", "&")
        if not normalized:
            return ""

        parsed = urlparse(normalized)
        if not parsed.scheme or not parsed.netloc:
            return normalized.rstrip("#&?")

        cleaned_query = parsed.query.rstrip("&")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, cleaned_query, ""))

    async def _fetch_article_page(self, url: str) -> PageFetchResult | None:
        return await self._fetch_html_document(url)

    async def _resolve_summary(self, candidate: ArticleCandidate, *, page_result: PageFetchResult | None) -> str:
        candidates: list[tuple[str, str]] = []

        feed_summary = self._prepare_summary_candidate(candidate.summary, source_name=candidate.source, title=candidate.title)
        if feed_summary:
            candidates.append(("feed", feed_summary))

        if page_result is not None:
            page_summary = self._extract_summary_from_page_html(page_result.html)
            page_summary = self._prepare_summary_candidate(page_summary, source_name="", title=candidate.title)
            if page_summary:
                candidates.append(("page", page_summary))

        metadata_summary = await self._lookup_summary_metadata(candidate, page_result=page_result)
        metadata_summary = self._prepare_summary_candidate(metadata_summary, source_name="", title=candidate.title)
        if metadata_summary:
            candidates.append(("metadata", metadata_summary))

        best_summary = self._pick_best_summary(candidates, title=candidate.title)
        if best_summary:
            if self.ai_summary_service is not None:
                ai_summary = await self.ai_summary_service.summarize_article(
                    title=candidate.title,
                    journal=candidate.source,
                    url=candidate.url,
                    source_text=best_summary,
                )
                ai_summary = self._clean_summary_for_post(ai_summary, "")
                if ai_summary and not self._looks_like_title_only_summary(ai_summary, candidate.title):
                    return ai_summary
            return best_summary

        return ""

    def _extract_summary_from_page_html(self, html_text: str) -> str:
        soup = BeautifulSoup(html_text, "html.parser")
        meta_candidates = [
            ("meta", {"name": "citation_abstract"}),
            ("meta", {"name": "dc.description.abstract"}),
            ("meta", {"name": "dc.description"}),
            ("meta", {"name": "description"}),
            ("meta", {"property": "og:description"}),
            ("meta", {"name": "twitter:description"}),
        ]
        for tag_name, attrs in meta_candidates:
            tag = soup.find(tag_name, attrs=attrs)
            content = tag.get("content", "") if tag else ""
            cleaned = self._clean_summary_for_post(content, "")
            if cleaned:
                summarized = summarize_article_text(cleaned, limit=700)
                if summarized:
                    return summarized
                return limit_text(cleaned, 700)

        for selector in (
            "[data-title='Summary']",
            "[id*='abstract']",
            "[id*='summary']",
            "[class*='abstract']",
            "[class*='summary']",
            "[data-title='Abstract']",
            "section.abstract",
            "div.abstract",
        ):
            node = soup.select_one(selector)
            if not node:
                continue
            cleaned = self._clean_summary_for_post(node.get_text(" ", strip=True), "")
            if cleaned:
                summarized = summarize_article_text(cleaned, limit=700)
                if summarized:
                    return summarized
                return limit_text(cleaned, 700)

        for script in soup.select("script[type='application/ld+json']"):
            payload = script.string or script.get_text(" ", strip=True)
            if not payload:
                continue
            for pattern in (
                r'"description"\s*:\s*"([^"]+)"',
                r'"abstract"\s*:\s*"([^"]+)"',
            ):
                for match in re.findall(pattern, payload, re.IGNORECASE):
                    cleaned = self._clean_summary_for_post(match.replace("\\/", "/"), "")
                    if cleaned:
                        summarized = summarize_article_text(cleaned, limit=700)
                        if summarized:
                            return summarized

        extracted = extract_background_from_html(html_text, limit=700)
        cleaned = self._clean_summary_for_post(extracted, "")
        if cleaned:
            summarized = summarize_article_text(cleaned, limit=700)
            if summarized:
                return summarized
        return cleaned

    async def _lookup_summary_metadata(
        self,
        candidate: ArticleCandidate,
        *,
        page_result: PageFetchResult | None,
    ) -> str:
        doi = self._extract_doi(candidate, page_result=page_result)
        summary = await self._lookup_openalex_summary(candidate, doi=doi)
        if summary:
            return summary

        summary = await self._lookup_crossref_summary(candidate)
        if summary:
            return summary

        return ""

    async def _lookup_openalex_summary(self, candidate: ArticleCandidate, *, doi: str | None) -> str:
        params: dict[str, str | int]
        if doi:
            params = {"filter": f"doi:{doi}", "per-page": 5}
        else:
            params = {"search": candidate.title, "per-page": 5}

        try:
            response = await self._client.get(OPENALEX_WORKS_URL, params=params, headers=self._http_headers)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # pragma: no cover - remote API variability
            LOGGER.warning("OpenAlex summary lookup failed for %s: %s", candidate.title, exc)
            return ""

        results = payload.get("results") or []
        for work in results:
            if not self._openalex_matches_candidate(candidate, work, doi=doi):
                continue

            abstract = reconstruct_openalex_abstract(work.get("abstract_inverted_index"))
            if abstract:
                summarized = summarize_article_text(abstract, limit=700)
                if summarized and not self._looks_like_title_only_summary(summarized, candidate.title):
                    LOGGER.info("Using OpenAlex summary for article \"%s\".", candidate.title)
                    return summarized

            for field in ("display_name",):
                raw_value = work.get(field)
                cleaned = self._clean_summary_for_post(str(raw_value or ""), "")
                if cleaned and cleaned.casefold() != candidate.title.casefold():
                    summarized = summarize_article_text(cleaned, limit=700)
                    if summarized:
                        LOGGER.info("Using OpenAlex fallback text for article \"%s\".", candidate.title)
                        return summarized

        return ""

    async def _lookup_crossref_summary(self, candidate: ArticleCandidate) -> str:
        try:
            response = await self._client.get(
                "https://api.crossref.org/works",
                params={"query.title": candidate.title, "rows": 5},
                headers=self._http_headers,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # pragma: no cover - remote API variability
            LOGGER.warning("Crossref summary lookup failed for %s: %s", candidate.title, exc)
            return ""

        for item in payload.get("message", {}).get("items", []):
            titles = item.get("title") or []
            if not titles:
                continue
            if self._normalize_title(titles[0]) != self._normalize_title(candidate.title):
                continue

            abstract_html = item.get("abstract")
            if not abstract_html:
                continue

            abstract_text = BeautifulSoup(abstract_html, "html.parser").get_text(" ", strip=True)
            cleaned = self._clean_summary_for_post(abstract_text, "")
            if not cleaned:
                continue

            summarized = summarize_article_text(cleaned, limit=700)
            if summarized and not self._looks_like_title_only_summary(summarized, candidate.title):
                LOGGER.info("Using Crossref summary for article \"%s\".", candidate.title)
                return summarized

        return ""

    def _openalex_matches_candidate(self, candidate: ArticleCandidate, work: dict, *, doi: str | None) -> bool:
        if doi:
            work_doi = str(work.get("doi") or "").removeprefix("https://doi.org/").removeprefix("http://doi.org/")
            if work_doi and work_doi.casefold() == doi.casefold():
                return True

        display_name = str(work.get("display_name") or "")
        return self._normalize_title(display_name) == self._normalize_title(candidate.title)

    def _normalize_title(self, value: str) -> str:
        cleaned = self._normalize_text(value)
        cleaned = re.sub(r"[^\w\s]", "", cleaned, flags=re.UNICODE)
        return cleaned.casefold()

    def _looks_like_title_only_summary(self, summary: str, title: str) -> bool:
        if not summary:
            return False
        return self._normalize_title(summary) == self._normalize_title(title)

    async def _fetch_html_document(self, url: str, *, referer: str | None = None) -> PageFetchResult | None:
        fallback: PageFetchResult | None = None

        for fetcher in (
            self._fetch_page_with_httpx,
            self._fetch_page_with_cloudscraper,
            self._fetch_page_with_curl,
        ):
            result = await fetcher(url, referer=referer)
            if result is None:
                continue
            if not self._looks_like_challenge_page(result.html):
                return result
            fallback = fallback or result

        return fallback

    async def _fetch_page_with_httpx(self, url: str, *, referer: str | None = None) -> PageFetchResult | None:
        try:
            response = await self._client.get(url, headers={**self._http_headers, "Referer": referer or self._http_headers["Referer"]})
            response.raise_for_status()
            return PageFetchResult(url=url, final_url=str(response.url), html=response.text)
        except Exception as exc:  # pragma: no cover - provider/network variability
            LOGGER.warning("HTTP article-page fetch failed for %s: %s", url, exc)
            return None

    async def _fetch_page_with_cloudscraper(self, url: str, *, referer: str | None = None) -> PageFetchResult | None:
        try:
            response = await asyncio.to_thread(
                self._scraper.get,
                url,
                timeout=self.timeout_seconds,
                allow_redirects=True,
                headers={**self._http_headers, "Referer": referer or self._http_headers["Referer"]},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"status={response.status_code}")
            return PageFetchResult(url=url, final_url=str(response.url), html=response.text)
        except Exception as exc:  # pragma: no cover - provider/network variability
            LOGGER.warning("Cloudscraper article-page fetch failed for %s: %s", url, exc)
            return None

    async def _fetch_page_with_curl(self, url: str, *, referer: str | None = None) -> PageFetchResult | None:
        if self._curl_session is None:
            return None

        try:
            response = await asyncio.to_thread(
                self._curl_session.get,
                url,
                timeout=self.timeout_seconds,
                allow_redirects=True,
                impersonate="chrome",
                headers={**self._http_headers, "Referer": referer or self._http_headers["Referer"]},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"status={response.status_code}")
            return PageFetchResult(url=url, final_url=str(response.url), html=response.text)
        except Exception as exc:  # pragma: no cover - provider/network variability
            LOGGER.warning("Browser-impersonated article-page fetch failed for %s: %s", url, exc)
            return None

    async def _fetch_pdf_with_httpx(self, url: str, *, referer: str) -> PdfFetchResult | None:
        headers = {**self._pdf_headers, "Referer": referer}
        try:
            response = await self._client.get(url, headers=headers)
            response.raise_for_status()
            if self._is_pdf_response(response.headers.get("content-type", ""), response.content):
                final_url = str(response.url)
                filename = self._filename_from_response_url(
                    final_url,
                    content_disposition=response.headers.get("content-disposition", ""),
                )
                if self._looks_like_non_article_pdf(final_url, filename=filename):
                    LOGGER.info("Skipping non-article PDF candidate %s.", final_url)
                    return None
                return PdfFetchResult(
                    filename=filename,
                    content=response.content,
                    source_url=final_url,
                )
        except Exception as exc:  # pragma: no cover - provider/network variability
            LOGGER.warning("HTTP PDF fetch failed for %s: %s", url, exc)
            return None

        return None

    async def _fetch_pdf_with_cloudscraper(self, url: str, *, referer: str) -> PdfFetchResult | None:
        headers = {**self._pdf_headers, "Referer": referer}
        try:
            response = await asyncio.to_thread(
                self._scraper.get,
                url,
                timeout=self.timeout_seconds,
                allow_redirects=True,
                headers=headers,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"status={response.status_code}")
            if self._is_pdf_response(response.headers.get("content-type", ""), response.content):
                final_url = str(response.url)
                filename = self._filename_from_response_url(
                    final_url,
                    content_disposition=response.headers.get("content-disposition", ""),
                )
                if self._looks_like_non_article_pdf(final_url, filename=filename):
                    LOGGER.info("Skipping non-article PDF candidate %s.", final_url)
                    return None
                return PdfFetchResult(
                    filename=filename,
                    content=response.content,
                    source_url=final_url,
                )
        except Exception as exc:  # pragma: no cover - provider/network variability
            LOGGER.warning("Cloudscraper PDF fetch failed for %s: %s", url, exc)
            return None

        return None

    async def _fetch_pdf_with_curl(self, url: str, *, referer: str) -> PdfFetchResult | None:
        if self._curl_session is None:
            return None

        headers = {**self._pdf_headers, "Referer": referer}
        try:
            response = await asyncio.to_thread(
                self._curl_session.get,
                url,
                timeout=self.timeout_seconds,
                allow_redirects=True,
                impersonate="chrome",
                headers=headers,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"status={response.status_code}")
            if self._is_pdf_response(response.headers.get("content-type", ""), response.content):
                final_url = str(response.url)
                filename = self._filename_from_response_url(
                    final_url,
                    content_disposition=response.headers.get("content-disposition", ""),
                )
                if self._looks_like_non_article_pdf(final_url, filename=filename):
                    LOGGER.info("Skipping non-article PDF candidate %s.", final_url)
                    return None
                return PdfFetchResult(
                    filename=filename,
                    content=response.content,
                    source_url=final_url,
                )
        except Exception as exc:  # pragma: no cover - provider/network variability
            LOGGER.warning("Browser-impersonated PDF fetch failed for %s: %s", url, exc)
            return None

        return None

    def _extend_unique_urls(self, pending: deque[str], attempted: set[str], values: list[str]) -> None:
        for value in values:
            normalized = self._normalize_candidate_url(value)
            if not normalized:
                continue
            if normalized in attempted or normalized in pending:
                continue
            pending.append(normalized)

    def _build_pdf_url_variants(self, url: str) -> list[str]:
        normalized = self._normalize_candidate_url(url)
        if not normalized:
            return []
        if self._looks_like_non_article_pdf(normalized):
            return []

        parsed = urlparse(normalized)
        if not parsed.scheme or not parsed.netloc:
            return []

        host = parsed.netloc.lower()
        path = parsed.path
        variants: list[str] = []

        def add(value: str | None) -> None:
            candidate = self._normalize_candidate_url(value)
            if not candidate:
                return
            if candidate not in variants:
                variants.append(candidate)

        add(normalized)
        add(infer_pdf_url_from_article_url(normalized))

        doi = self._extract_doi_from_url(normalized)

        if "ahajournals.org" in host and doi:
            add(f"{parsed.scheme}://{parsed.netloc}/doi/pdf/{doi}")
            add(f"{parsed.scheme}://{parsed.netloc}/doi/pdf/{doi}?download=true")
            add(f"{parsed.scheme}://{parsed.netloc}/doi/epdf/{doi}")
            add(f"{parsed.scheme}://{parsed.netloc}/doi/epdf/{doi}?download=true")

        if "sciencedirect.com" in host and "/science/article/pii/" in path:
            pii = path.rstrip("/").split("/")[-1]
            article_base = f"{parsed.scheme}://{parsed.netloc}/science/article/pii/{pii}"
            add(f"{article_base}/pdfft?isDTMRedir=true&download=true")
            add(f"{article_base}/pdfft")
            add(f"{article_base}/pdf")

        if "nature.com" in host and "/articles/" in path:
            article_base = f"{parsed.scheme}://{parsed.netloc}{path.removesuffix('.pdf')}"
            add(f"{article_base}.pdf")
            add(f"{article_base}?pdf=button%20sticky")

        return variants

    async def _lookup_open_access_pdf_urls(
        self,
        candidate: ArticleCandidate,
        *,
        page_result: PageFetchResult | None,
    ) -> list[str]:
        doi = self._extract_doi(candidate, page_result=page_result)
        if not doi:
            return []

        try:
            response = await self._client.get(
                f"{OPENALEX_WORKS_URL}?filter=doi:{quote(doi, safe='')}",
                headers=self._http_headers,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # pragma: no cover - remote API variability
            LOGGER.warning("OpenAlex lookup failed for DOI %s: %s", doi, exc)
            return []

        results = payload.get("results") or []
        if not results:
            return []

        work = results[0]
        urls: list[str] = []

        def add(value: str | None) -> None:
            normalized = self._normalize_candidate_url(value)
            if not normalized:
                return
            if not normalized.startswith(("http://", "https://")):
                return
            for variant in self._build_pdf_url_variants(normalized):
                if variant not in urls:
                    urls.append(variant)

        open_access = work.get("open_access") or {}
        if not (open_access.get("is_oa") or open_access.get("any_repository_has_fulltext")):
            return []

        add(open_access.get("oa_url"))

        def add_landing_page(value: str | None) -> None:
            normalized = self._normalize_candidate_url(value)
            if not normalized:
                return
            host = urlparse(normalized).netloc.lower()
            if host in {"doi.org", "pubmed.ncbi.nlm.nih.gov"}:
                return
            add(normalized)

        for location_key in ("best_oa_location", "primary_location"):
            location = work.get(location_key) or {}
            add(location.get("pdf_url"))
            add_landing_page(location.get("landing_page_url"))

        for location in work.get("locations") or []:
            add(location.get("pdf_url"))
            add_landing_page(location.get("landing_page_url"))

        if urls:
            LOGGER.info("OpenAlex returned %s PDF candidates for DOI %s.", len(urls), doi)
        return urls

    def _extract_doi(self, candidate: ArticleCandidate, *, page_result: PageFetchResult | None) -> str | None:
        for value in (
            self._extract_doi_from_url(candidate.url),
            self._extract_doi_from_url(page_result.final_url) if page_result is not None else None,
            self._extract_doi_from_html(page_result.html) if page_result is not None else None,
        ):
            if value:
                return value
        return None

    def _extract_doi_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path

        if "academic.oup.com" in host:
            match = re.search(r"/doi/(10\.\d{4,9})/([^/]+)/([^/]+)/", path, re.IGNORECASE)
            if match:
                return f"{match.group(1)}/{match.group(2)}/{match.group(3)}"

        if "nature.com" in host and "/articles/" in path:
            article_code = path.rstrip("/").split("/")[-1].removesuffix(".pdf")
            if article_code.startswith("s"):
                return f"10.1038/{article_code}"

        match = re.search(r"/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^?#]+)", path, re.IGNORECASE)
        if match:
            return match.group(1).rstrip("/")

        return None

    def _extract_doi_from_html(self, html_text: str) -> str | None:
        soup = BeautifulSoup(html_text, "html.parser")

        for selector in (
            "meta[name='citation_doi']",
            "meta[name='dc.identifier']",
            "meta[name='prism.doi']",
        ):
            tag = soup.select_one(selector)
            content = (tag.get("content", "") if tag else "").strip()
            if content.lower().startswith("doi:"):
                content = content[4:].strip()
            if re.match(r"^10\.\d{4,9}/\S+$", content, re.IGNORECASE):
                return content

        match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", html_text, re.IGNORECASE)
        return match.group(0) if match else None

    def _is_pdf_response(self, content_type: str, content: bytes) -> bool:
        if content.startswith(b"%PDF"):
            return True
        return "application/pdf" in (content_type or "").lower()

    def _looks_like_challenge_page(self, html_text: str) -> bool:
        lowered = html_text.casefold()
        return (
            "just a moment" in lowered
            or "enable javascript and cookies to continue" in lowered
            or "cf-chl" in lowered
            or "cf-browser-verification" in lowered
            or "tdm-reservation" in lowered
        )

    def _filename_from_response_url(self, url: str, *, content_disposition: str = "") -> str:
        disposition_name = self._filename_from_content_disposition(content_disposition)
        if disposition_name:
            return disposition_name
        return self._filename_from_url(url)

    def _filename_from_content_disposition(self, value: str) -> str | None:
        if not value:
            return None

        utf_match = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", value, re.IGNORECASE)
        if utf_match:
            filename = unquote(utf_match.group(1).strip().strip('"'))
            return filename or None

        plain_match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE)
        if plain_match:
            return plain_match.group(1).strip() or None

        bare_match = re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
        if bare_match:
            return bare_match.group(1).strip().strip('"') or None

        return None

    def _filename_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix == ".pdf":
            return Path(parsed.path).name or "article.pdf"

        stem = Path(parsed.path).name or "article"
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "article"
        return f"{safe_stem}.pdf"

    def _clean_summary_for_post(self, summary: str, source_name: str) -> str:
        cleaned = self._normalize_text(summary)
        if not cleaned:
            return ""

        if source_name and cleaned.casefold().startswith(source_name.casefold()):
            cleaned = cleaned[len(source_name) :].lstrip(" ,:-")

        if re.match(r"^(volume|issue|page|pages|doi)\b", cleaned, re.IGNORECASE):
            return ""

        if "volume" in cleaned.casefold() and len(cleaned.split()) <= 18:
            return ""

        if looks_like_navigation_noise(cleaned):
            return ""

        return cleaned

    def _prepare_summary_candidate(self, summary: str, *, source_name: str, title: str) -> str:
        cleaned = self._clean_summary_for_post(summary, source_name)
        if not cleaned or self._looks_like_title_only_summary(cleaned, title):
            return ""

        summarized = summarize_article_text(cleaned, limit=700)
        if summarized:
            return summarized

        if len(cleaned.split()) < 10:
            return ""
        return limit_text(cleaned, 700)

    def _pick_best_summary(self, candidates: list[tuple[str, str]], *, title: str) -> str:
        scored: list[tuple[int, str, str]] = []
        for origin, summary in candidates:
            if not summary or self._looks_like_title_only_summary(summary, title):
                continue
            score = self._score_summary(summary, origin=origin, title=title)
            if score <= 0:
                continue
            scored.append((score, origin, summary))

        if not scored:
            return ""

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_origin, best_summary = scored[0]
        LOGGER.info(
            'Selected %s article summary for "%s" with score=%s.',
            best_origin,
            title,
            best_score,
        )
        return best_summary

    def _score_summary(self, summary: str, *, origin: str, title: str) -> int:
        normalized = self._normalize_text(summary)
        if not normalized:
            return 0

        lowered = normalized.casefold()
        if self._looks_like_title_only_summary(normalized, title):
            return 0
        if looks_like_navigation_noise(normalized):
            return 0
        if any(marker in lowered for marker in ("click here", "read more", "full text", "download pdf")):
            return 0

        words = normalized.split()
        word_count = len(words)
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]

        score = 0
        if word_count >= 20:
            score += min(word_count, 90)
        if 25 <= word_count <= 120:
            score += 120
        elif 15 <= word_count < 25:
            score += 40
        elif word_count > 120:
            score -= min(word_count - 120, 60)

        if 2 <= len(sentences) <= 4:
            score += 120
        elif len(sentences) == 1 and word_count >= 18:
            score += 45
        elif len(sentences) > 5:
            score -= 40

        if any(token in lowered for token in ("study", "trial", "investigated", "examined", "evaluated", "assessed", "found", "showed", "demonstrated", "suggest", "concluded", "aimed")):
            score += 70
        if any(token in lowered for token in ("background", "objective", "results", "conclusion", "conclusions")):
            score += 20
        if any(char.isdigit() for char in normalized):
            score += 20

        if origin == "metadata":
            score += 40
        elif origin == "page":
            score += 20

        return score

    def _normalize_text(self, value: str) -> str:
        cleaned = (
            value.replace("\xa0", " ")
            .replace("\ufffd", '"')
            .replace("ą", "±")
            .replace("â‰¥", ">=")
            .replace("â‰¤", "<=")
            .replace("â€“", "-")
            .replace("â€”", "-")
            .replace("â€²", "'")
        )
        return " ".join(cleaned.split()).strip()

    def _format_publication_date(self, value: datetime | None) -> str:
        if value is None:
            return "Unknown"
        return value.astimezone(UTC).strftime("%d %b %Y")


def deduplicate_article_candidates(candidates: list[ArticleCandidate]) -> list[ArticleCandidate]:
    unique: dict[str, ArticleCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.unique_id, candidate)
    return list(unique.values())


def article_sort_key(candidate: ArticleCandidate) -> tuple[datetime, str]:
    published = candidate.published_at or datetime(1970, 1, 1, tzinfo=UTC)
    return published, candidate.title.casefold()
