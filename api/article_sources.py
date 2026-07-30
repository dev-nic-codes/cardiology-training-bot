from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import struct_time
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional dependency during bootstrap
    curl_requests = None

LOGGER = logging.getLogger(__name__)
DEFAULT_FEED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
NAVIGATION_NOISE_MARKERS: tuple[str, ...] = (
    "skip to main content",
    "advertisement",
    "become a member",
    "volunteer",
    "donate",
    "browse collections",
    "for authors",
    "for reviewers",
    "for subscribers",
    "for international users",
    "quick search",
    "advanced search",
    "shopping cart",
    "sign in",
    "register",
    "journal metrics",
    "early career resources",
    "alerts",
)

ARTICLE_RSS_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "Circulation",
        "https://www.ahajournals.org/action/showFeed?type=etoc&feed=rss&jc=circ",
    ),
    (
        "European Heart Journal",
        "https://academic.oup.com/rss/site_5375/advanceAccess_3236.xml",
    ),
    (
        "Journal of the American College of Cardiology",
        "https://rss.sciencedirect.com/publication/science/07351097",
    ),
    (
        "JAMA Cardiology",
        "https://jamanetwork.com/rss/site_192/184.xml",
    ),
    (
        "Nature Reviews Cardiology",
        "https://www.nature.com/nrcardio/journal/vaop/ncurrent/rss.rdf",
    ),
    (
        "JACC: Cardiovascular Interventions",
        "https://rss.sciencedirect.com/publication/science/19368798",
    ),
    (
        "JACC: Cardiovascular Imaging",
        "https://rss.sciencedirect.com/publication/science/1936878X",
    ),
    (
        "European Journal of Heart Failure",
        "https://academic.oup.com/rss/site_6719/advanceAccess_4214.xml",
    ),
    (
        "Circulation Research",
        "https://www.ahajournals.org/action/showFeed?type=etoc&feed=rss&jc=res",
    ),
    (
        "Cardiovascular Research",
        "https://academic.oup.com/rss/site_5369/advanceAccess_3230.xml",
    ),
)
MEDSCAPE_CARDIOLOGY_URL = "https://www.medscape.com/cardiology"


@dataclass(slots=True)
class ArticleCandidate:
    unique_id: str
    title: str
    source: str
    url: str
    pdf_url: str | None
    summary: str
    published_at: datetime | None


class RSSArticleSource:
    def __init__(self, *, name: str, feed_url: str, limit: int = 20) -> None:
        self.name = name
        self.feed_url = feed_url
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> list[ArticleCandidate]:
        feed_text = await self._fetch_feed_text(client)

        parsed = feedparser.parse(feed_text)
        if getattr(parsed, "bozo", False):
            LOGGER.warning("Feed parsing warning for %s: %s", self.name, parsed.bozo_exception)

        candidates: list[ArticleCandidate] = []
        for entry in parsed.entries[: self.limit]:
            url = entry.get("link") or entry.get("id")
            if not url:
                continue

            normalized_url = normalize_article_url(url)
            title = clean_text(entry.get("title", "Untitled article"))
            if not title:
                continue
            if should_skip_entry(title=title, entry=entry):
                continue

            summary = extract_background_from_entry(entry)
            unique_id = normalize_article_url(entry.get("id", "")) if str(entry.get("id", "")).startswith("http") else normalized_url
            candidates.append(
                ArticleCandidate(
                    unique_id=unique_id or normalized_url,
                    title=title,
                    source=self.name,
                    url=url,
                    pdf_url=extract_pdf_url_from_entry(entry, url),
                    summary=summary,
                    published_at=parse_feed_datetime(entry),
                )
            )

        return candidates

    async def _fetch_feed_text(self, client: httpx.AsyncClient) -> str:
        try:
            response = await client.get(self.feed_url)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            if curl_requests is None:
                raise

            LOGGER.warning(
                "HTTP feed fetch failed for %s (%s). Retrying with browser impersonation.",
                self.feed_url,
                exc,
            )
            return await asyncio.to_thread(self._fetch_feed_text_with_curl)

    def _fetch_feed_text_with_curl(self) -> str:
        response = curl_requests.get(
            self.feed_url,
            timeout=30,
            allow_redirects=True,
            impersonate="chrome",
            headers={
                "User-Agent": DEFAULT_FEED_USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}")
        return response.text


class MedscapeCardiologySource:
    def __init__(self, *, url: str = MEDSCAPE_CARDIOLOGY_URL, limit: int = 20) -> None:
        self.url = url
        self.limit = limit
        self.name = "Medscape Cardiology"

    async def fetch(self, client: httpx.AsyncClient) -> list[ArticleCandidate]:
        response = await self._fetch_page(client)

        soup = BeautifulSoup(response.text, "html.parser")
        seen: set[str] = set()
        candidates: list[ArticleCandidate] = []

        for anchor in soup.select("a[href*='/viewarticle/'], a[href*='/s/viewarticle/']"):
            href = anchor.get("href", "")
            title = clean_text(anchor.get_text(" ", strip=True))
            if not href or not title:
                continue

            url = urljoin(str(response.url), href)
            unique_id = normalize_article_url(url)
            if unique_id in seen:
                continue
            seen.add(unique_id)

            candidates.append(
                ArticleCandidate(
                    unique_id=unique_id,
                    title=title,
                    source=self.name,
                    url=url,
                    pdf_url=None,
                    summary="",
                    published_at=None,
                )
            )

            if len(candidates) >= self.limit:
                break

        return candidates

    async def _fetch_page(self, client: httpx.AsyncClient):
        try:
            response = await client.get(self.url)
            response.raise_for_status()
            return response
        except Exception as exc:
            if curl_requests is None:
                raise

            LOGGER.info(
                "HTTP page fetch failed for %s (%s). Retrying with browser impersonation.",
                self.url,
                exc,
            )
            return await asyncio.to_thread(self._fetch_page_with_curl)

    def _fetch_page_with_curl(self):
        response = curl_requests.get(
            self.url,
            timeout=30,
            allow_redirects=True,
            impersonate="chrome",
            headers={
                "User-Agent": DEFAULT_FEED_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}")
        return response


def build_article_sources() -> list[RSSArticleSource | MedscapeCardiologySource]:
    sources: list[RSSArticleSource | MedscapeCardiologySource] = [
        RSSArticleSource(name=name, feed_url=feed_url) for name, feed_url in ARTICLE_RSS_SOURCES
    ]
    sources.append(MedscapeCardiologySource())
    return sources


def normalize_article_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def should_skip_entry(*, title: str, entry: dict) -> bool:
    normalized_title = title.casefold().strip()
    if normalized_title in {"jama cardiology", "full issue pdf"}:
        return True

    raw_summary = " ".join(
        str(value or "")
        for value in (
            entry.get("summary"),
            entry.get("description"),
            " ".join(str(item.get("value", "")) for item in entry.get("content", [])),
        )
    ).casefold()

    if "mission statement" in raw_summary:
        return True

    return False


def extract_pdf_url_from_entry(entry: dict, article_url: str) -> str | None:
    for link in entry.get("links", []):
        href = link.get("href")
        if not href:
            continue

        href_lower = href.lower()
        link_type = str(link.get("type", "")).lower()
        title = str(link.get("title", "")).lower()
        if href_lower.endswith(".pdf") or "/pdf/" in href_lower or "pdf" in link_type or "pdf" in title:
            return urljoin(article_url, href)

    return infer_pdf_url_from_article_url(article_url)


def infer_pdf_url_from_article_url(article_url: str) -> str | None:
    parsed = urlparse(article_url)
    host = parsed.netloc.lower()
    path = parsed.path

    if path.lower().endswith(".pdf"):
        return article_url

    if "ahajournals.org" in host and "/doi/" in path:
        return urlunparse((parsed.scheme, parsed.netloc, path.replace("/doi/abs/", "/doi/pdf/").replace("/doi/full/", "/doi/pdf/"), "", "", ""))

    if "academic.oup.com" in host and "/advance-article/doi/" in path:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 7:
            journal = parts[0]
            doi_prefix = parts[3]
            doi_middle = parts[4]
            doi_suffix = parts[5]
            article_id = parts[6]
            pdf_path = (
                f"/{journal}/advance-article-pdf/doi/"
                f"{doi_prefix}/{doi_middle}/{doi_suffix}/{article_id}/{doi_suffix}.pdf"
            )
            return urlunparse((parsed.scheme, parsed.netloc, pdf_path, "", "", ""))

    if "sciencedirect.com" in host and "/science/article/pii/" in path:
        return urlunparse((parsed.scheme, parsed.netloc, f"{path.rstrip('/')}/pdfft", "", "isDTMRedir=true&download=true", ""))

    if "nature.com" in host and "/articles/" in path:
        article_path = path.removesuffix(".pdf")
        return urlunparse((parsed.scheme, parsed.netloc, f"{article_path}.pdf", "", "", ""))

    return None


def clean_text(value: str) -> str:
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_summary(value: str, limit: int = 800) -> str:
    summary = clean_text(value)
    if len(summary) <= limit:
        return summary
    return summary[: limit - 3].rstrip() + "..."


def summarize_article_text(value: str, *, limit: int = 700) -> str:
    text = clean_text(value)
    if not text:
        return ""

    text = strip_feed_metadata(text)
    if not text or looks_like_metadata_only(text) or looks_like_navigation_noise(text):
        return ""

    sections = extract_structured_sections(text)
    preferred: list[str] = []
    for key in ("background", "objective"):
        section_text = sections.get(key)
        if section_text:
            preferred.extend(_select_sentences(section_text, count=1))

    for key in ("results", "conclusion"):
        section_text = sections.get(key)
        if section_text:
            preferred.extend(_select_sentences(section_text, count=1))

    if preferred:
        return limit_text(join_summary_sentences(preferred), limit)

    methods_text = sections.get("methods")
    if methods_text:
        method_summary = join_summary_sentences(_select_sentences(methods_text, count=2))
        if method_summary:
            return limit_text(method_summary, limit)

    sentences = _select_sentences(text, count=4)
    if len(sentences) >= 3:
        fallback_summary = join_summary_sentences([sentences[0], sentences[1], sentences[-1]])
    else:
        fallback_summary = join_summary_sentences(sentences)
    return limit_text(fallback_summary, limit)


def extract_background_from_entry(entry: dict, *, limit: int = 1400) -> str:
    html_candidates: list[str] = []
    for item in entry.get("content", []):
        value = item.get("value")
        if value:
            html_candidates.append(str(value))

    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            html_candidates.append(str(value))

    for html_value in html_candidates:
        background = extract_background_from_html(html_value, limit=limit)
        if background:
            return background

    return ""


def extract_background_from_html(html_value: str, *, limit: int = 1400) -> str:
    if not html_value:
        return ""

    if re.search(r"Author\(s\):", html_value, re.IGNORECASE) and not re.search(
        r"\bBACKGROUND\b|\bABSTRACT\b|\bMETHODS\b|\bRESULTS\b",
        html_value,
        re.IGNORECASE,
    ):
        return ""

    text = clean_text(html_value)
    if not text:
        return ""

    text = strip_feed_metadata(text)
    if not text:
        return ""

    if re.search(r"\bAuthor\(s\):", text, re.IGNORECASE):
        return ""

    if looks_like_metadata_only(text) or looks_like_navigation_noise(text):
        return ""

    summary = summarize_article_text(text, limit=limit)
    if summary:
        return summary

    sections = extract_structured_sections(text)
    for key in ("background", "objective", "results", "conclusion"):
        section_text = sections.get(key)
        if section_text:
            return limit_text(clean_text(section_text), limit)

    return limit_text(text, limit)


def extract_structured_sections(text: str) -> dict[str, str]:
    patterns = {
        "background": (
            r"(?:^|[\s.;])BACKGROUND AND AIMS\s*:\s*(.+?)(?=(?:^|[\s.;])METHODS\b|(?:^|[\s.;])METHODS AND RESULTS\b|(?:^|[\s.;])RESULTS\b|(?:^|[\s.;])CONCLUSIONS?\b|(?:^|[\s.;])OBJECTIVES?\b|$)",
            r"(?:^|[\s.;])BACKGROUND\s*:\s*(.+?)(?=(?:^|[\s.;])METHODS\b|(?:^|[\s.;])METHODS AND RESULTS\b|(?:^|[\s.;])RESULTS\b|(?:^|[\s.;])CONCLUSIONS?\b|(?:^|[\s.;])OBJECTIVES?\b|$)",
        ),
        "objective": (
            r"(?:^|[\s.;])OBJECTIVES?\s*:\s*(.+?)(?=(?:^|[\s.;])METHODS\b|(?:^|[\s.;])METHODS AND RESULTS\b|(?:^|[\s.;])RESULTS\b|(?:^|[\s.;])CONCLUSIONS?\b|$)",
            r"(?:^|[\s.;])AIMS?\s*:\s*(.+?)(?=(?:^|[\s.;])METHODS\b|(?:^|[\s.;])METHODS AND RESULTS\b|(?:^|[\s.;])RESULTS\b|(?:^|[\s.;])CONCLUSIONS?\b|$)",
        ),
        "methods": (
            r"(?:^|[\s.;])METHODS AND RESULTS\s*:\s*(.+?)(?=(?:^|[\s.;])CONCLUSIONS?\b|$)",
            r"(?:^|[\s.;])METHODS\s*:\s*(.+?)(?=(?:^|[\s.;])RESULTS\b|(?:^|[\s.;])CONCLUSIONS?\b|$)",
        ),
        "results": (
            r"(?:^|[\s.;])RESULTS\s*:\s*(.+?)(?=(?:^|[\s.;])CONCLUSIONS?\b|$)",
        ),
        "conclusion": (
            r"(?:^|[\s.;])CONCLUSIONS?\s*:\s*(.+)$",
        ),
    }

    sections: dict[str, str] = {}
    for key, key_patterns in patterns.items():
        for pattern in key_patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                cleaned = clean_text(match.group(1))
                if cleaned:
                    sections[key] = cleaned
                    break
    return sections


def _select_sentences(text: str, *, count: int) -> list[str]:
    sentences = [
        sentence.strip(" ;")
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", clean_text(text))
        if sentence.strip()
    ]
    return sentences[:count]


def join_summary_sentences(sentences: list[str]) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        cleaned = clean_text(sentence)
        if not cleaned:
            continue
        normalized = cleaned.casefold()
        if normalized in seen:
            continue
        unique.append(cleaned)
        seen.add(normalized)
    return " ".join(unique).strip()


def reconstruct_openalex_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    if not inverted_index:
        return ""

    tokens: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for position in positions:
            tokens.append((position, word))

    if not tokens:
        return ""

    ordered_words = [word for _, word in sorted(tokens)]
    text = " ".join(ordered_words)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return clean_text(text)


def strip_feed_metadata(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"^\s*[^.]{0,250}\bdoi:\s*10\.\d{4,9}/\S+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*[^.]{0,250}\bPublished online:\s*[^;:.]{1,80}[;:.]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*[^.]{0,250}\bAvailable online\b[^;:.]{0,120}[;:.]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*[^.]{0,200}\bVolume\b[^.]*\.\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Publication date:\s*.*?(?=\bSource:|\bAuthor\(s\):|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bSource:\s*.*?(?=\bAuthor\(s\):|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bAuthor\(s\):\s*.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*Abstract\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*(?:doi:)?10\.\d{4,9}/\S+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*\d{3,5}/\S+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*[A-Za-z]+(?:\s+[A-Za-z]+)*,\s*Published online:\s*", "", cleaned, flags=re.IGNORECASE)
    return clean_text(cleaned)


def looks_like_metadata_only(text: str) -> bool:
    lowered = text.casefold()
    if "mission statement" in lowered:
        return True
    if lowered.startswith("publication date:") or lowered.startswith("source:"):
        return True
    if len(text.split()) <= 8 and "pdf" in lowered:
        return True
    return False


def looks_like_navigation_noise(text: str) -> bool:
    lowered = clean_text(text).casefold()
    if not lowered:
        return False

    marker_hits = sum(1 for marker in NAVIGATION_NOISE_MARKERS if marker in lowered)
    if marker_hits >= 4:
        return True

    if "searching:" in lowered and marker_hits >= 2:
        return True

    if "|" in lowered[:160] and "skip to main content" in lowered:
        return True

    return False


def limit_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def parse_feed_datetime(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return struct_time_to_datetime(value)

    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    return None


def struct_time_to_datetime(value: struct_time) -> datetime:
    return datetime(*value[:6], tzinfo=UTC)
