from __future__ import annotations

import asyncio
import logging
import re
import ssl
from dataclasses import dataclass
from html import unescape
from typing import Final
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional dependency during bootstrap
    curl_requests = None

LOGGER = logging.getLogger(__name__)

CASE_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"dispcase\.asp\?rownum=(?P<rownum>\d+)&ans=0(?:&caseid=(?P<caseid>\d+))?",
    re.IGNORECASE,
)
ONCLICK_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"window\.open\('([^']+)'\)|window\.open\(\"([^\"]+)\"\)")


@dataclass(slots=True)
class CaseIndexEntry:
    rownum: int
    case_id: int | None
    title: str
    quiz_url: str
    answer_url: str


@dataclass(slots=True)
class ParsedWaveMavenCase:
    rownum: int
    case_id: int | None
    source_url: str
    image_url: str
    vignette: str
    diagnosis: str
    explanation: str
    source_options: list[str]
    correct_option: str | None


class WaveMavenClient:
    """Async client for ECG Wave-Maven with legacy TLS compatibility."""

    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        request_delay_seconds: float,
        timeout_seconds: float,
        bootstrap_training_code: str,
        bootstrap_goal_code: str,
        max_request_attempts: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.user_agent = user_agent
        self.request_delay_seconds = request_delay_seconds
        self.timeout_seconds = timeout_seconds
        self.bootstrap_training_code = bootstrap_training_code
        self.bootstrap_goal_code = bootstrap_goal_code
        self.max_request_attempts = max(1, max_request_attempts)
        self._client: httpx.AsyncClient | None = None
        self._curl_session = curl_requests.Session() if curl_requests is not None else None
        self._bootstrapped = False
        self._last_request_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._curl_session is not None:
            self._curl_session.close()
            self._curl_session = None
        self._bootstrapped = False

    async def fetch_case_index(self) -> list[CaseIndexEntry]:
        response = await self._bootstrap_and_request("displist.asp?ans=0")
        return self._parse_case_index(response.text)

    async def fetch_case(self, entry: CaseIndexEntry) -> ParsedWaveMavenCase:
        response = await self._request(entry.answer_url)
        if not self._looks_like_case_page(response.text):
            response = await self._bootstrap_and_request(
                self._relative_reference(entry.answer_url),
                direct_url=entry.answer_url,
            )
        return self._parse_case_page(response.text, entry)

    async def fetch_case_from_source(
        self,
        *,
        rownum: int,
        case_id: int | None,
        answer_url: str,
    ) -> ParsedWaveMavenCase:
        return await self.fetch_case(
            CaseIndexEntry(
                rownum=rownum,
                case_id=case_id,
                title="",
                quiz_url="",
                answer_url=answer_url,
            )
        )

    async def download_binary(self, url: str) -> tuple[bytes, str]:
        binary_timeout = min(self.timeout_seconds, 5.0)
        try:
            response = await self._request(url, timeout_seconds=binary_timeout, max_attempts=2)
        except Exception as exc:
            if not self._should_retry_binary_after_bootstrap(exc):
                raise
            LOGGER.warning(
                "Direct Wave-Maven binary download failed for %s: %s. Retrying after bootstrap.",
                url,
                exc,
            )
            response = await self._bootstrap_and_request(
                self._full_relative_reference(url),
                direct_url=url,
                timeout_seconds=binary_timeout,
                max_attempts=1,
            )
        return response.content, response.headers.get("content-type", "")

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            ssl_context = ssl.create_default_context()
            if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
                ssl_context.options |= ssl.OP_LEGACY_SERVER_CONNECT

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=self.timeout_seconds,
                verify=ssl_context,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def _throttle(self) -> None:
        if self.request_delay_seconds <= 0:
            return
        now = asyncio.get_running_loop().time()
        delay = self.request_delay_seconds - (now - self._last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _request(
        self,
        url: str,
        *,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> httpx.Response:
        async with self._lock:
            last_error: Exception | None = None
            attempts = max(1, max_attempts or self.max_request_attempts)
            for attempt in range(1, attempts + 1):
                client = await self._ensure_client()
                try:
                    await self._throttle()
                    response = await client.get(url, timeout=timeout_seconds or self.timeout_seconds)
                    self._last_request_at = asyncio.get_running_loop().time()
                    response.raise_for_status()
                    return response
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    self._last_request_at = asyncio.get_running_loop().time()
                    if not self._should_retry_status(exc.response.status_code):
                        break
                    if attempt >= attempts:
                        break
                    LOGGER.warning(
                        "Wave-Maven request attempt %s/%s returned status=%s for %s. Retrying.",
                        attempt,
                        attempts,
                        exc.response.status_code,
                        url,
                    )
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt >= attempts:
                        break
                    LOGGER.warning(
                        "Wave-Maven request attempt %s/%s failed for %s: %s. Retrying.",
                        attempt,
                        attempts,
                        url,
                        exc,
                    )

                await self._reset_client()
                await asyncio.sleep(self._retry_delay_seconds(attempt))

            if self._curl_session is not None:
                LOGGER.warning("Wave-Maven HTTPX transport failed for %s. Falling back to browser impersonation.", url)
                return await self._request_with_curl(url, timeout_seconds=timeout_seconds)

            if last_error is not None:
                raise last_error
            raise RuntimeError(f"Wave-Maven request failed without an explicit exception for {url}")

    async def _request_with_curl(self, url: str, *, timeout_seconds: float | None = None) -> httpx.Response:
        if self._curl_session is None:
            raise RuntimeError("curl_cffi is not available for Wave-Maven fallback requests.")

        absolute_url = urljoin(self.base_url, url)
        response = await asyncio.to_thread(
            self._curl_session.get,
            absolute_url,
            timeout=timeout_seconds or self.timeout_seconds,
            allow_redirects=True,
            impersonate="chrome",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": self.base_url,
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"status={response.status_code}")
        request = httpx.Request("GET", absolute_url)
        return httpx.Response(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    async def _reset_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._bootstrapped = False

    def _should_retry_status(self, status_code: int) -> bool:
        return status_code in {408, 425, 429, 500, 502, 503, 504}

    def _retry_delay_seconds(self, attempt: int) -> float:
        base_delay = self.request_delay_seconds or 1.0
        return min(base_delay * attempt, 5.0)

    def _should_retry_binary_after_bootstrap(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {401, 403}

        message = str(exc).casefold()
        if any(marker in message for marker in ("timed out", "resolving timed out", "connection timed out", "connecterror")):
            return False
        return False

    async def _bootstrap_and_request(
        self,
        reference: str,
        *,
        direct_url: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> httpx.Response:
        bootstrap_url = (
            f"stats.asp?ref={reference}&training={self.bootstrap_training_code}"
            f"&goal={self.bootstrap_goal_code}&height=1080&width=1920"
        )
        response = await self._request(
            bootstrap_url,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        self._bootstrapped = True
        if direct_url:
            return await self._request(
                direct_url,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        return response

    def _relative_reference(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.rsplit("/", 1)[-1]
        return f"{path}?{parsed.query}" if parsed.query else path

    def _full_relative_reference(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        return f"{path}?{parsed.query}" if parsed.query else path

    def _looks_like_case_page(self, html_text: str) -> bool:
        return "Display Case:" in html_text and 'class="caseframe"' in html_text

    def _parse_case_index(self, html_text: str) -> list[CaseIndexEntry]:
        soup = BeautifulSoup(html_text, "html.parser")
        entries: list[CaseIndexEntry] = []
        seen_rownums: set[int] = set()

        # TODO: If Wave-Maven changes the list markup, revisit these selectors.
        for table in soup.select("table.displist"):
            anchors = table.select("a[href*='dispcase.asp']")
            if not anchors:
                continue

            title_anchor = anchors[-1]
            href = title_anchor.get("href", "")
            match = CASE_LINK_PATTERN.search(href)
            if not match:
                continue

            rownum = int(match.group("rownum"))
            if rownum in seen_rownums:
                continue

            case_id = match.group("caseid")
            answer_url = urljoin(self.base_url, f"dispcase.asp?ans=1&rownum={rownum}")
            if case_id:
                answer_url += f"&caseid={case_id}"

            entries.append(
                CaseIndexEntry(
                    rownum=rownum,
                    case_id=int(case_id) if case_id else None,
                    title=self._clean_text(title_anchor.get_text(" ", strip=True)),
                    quiz_url=urljoin(self.base_url, href),
                    answer_url=answer_url,
                )
            )
            seen_rownums.add(rownum)

        entries.sort(key=lambda item: item.rownum)
        return entries

    def _parse_case_page(self, html_text: str, entry: CaseIndexEntry) -> ParsedWaveMavenCase:
        soup = BeautifulSoup(html_text, "html.parser")
        vignette = self._extract_labeled_cell_text(soup, "Question:")
        explanation = self._extract_labeled_cell_text(soup, "Answer:")
        options, correct_option = self._extract_options(html_text)
        image_url = self._extract_image_url(soup)
        diagnosis = correct_option or self._extract_diagnosis_from_explanation(explanation)

        return ParsedWaveMavenCase(
            rownum=entry.rownum,
            case_id=entry.case_id,
            source_url=entry.answer_url,
            image_url=image_url,
            vignette=vignette or entry.title,
            diagnosis=diagnosis,
            explanation=explanation,
            source_options=options,
            correct_option=correct_option,
        )

    def _extract_labeled_cell_text(self, soup: BeautifulSoup, label: str) -> str:
        label_cells = soup.find_all("td", class_="qatitle")
        for cell in label_cells:
            if self._clean_text(cell.get_text(" ", strip=True)) != label:
                continue

            value_cell = cell.find_next_sibling("td")
            if value_cell is None:
                continue

            return self._clean_multiline_text(value_cell.get_text("\n", strip=True))
        return ""

    def _extract_options(self, html_text: str) -> tuple[list[str], str | None]:
        options: list[str] = []
        correct_option: str | None = None
        table_match = re.search(r"<table class=\"mc\">(.*?)</table>", html_text, re.IGNORECASE | re.DOTALL)
        if table_match is None:
            return options, correct_option

        # TODO: Adjust this parser if the site stops using mcgood/mcbad classes.
        cells = re.findall(
            r"<td class=\"(mcgood|mcbad|mc)\">\s*(.*?)\s*</td>",
            table_match.group(1),
            re.IGNORECASE | re.DOTALL,
        )
        for index in range(0, len(cells) - 1, 2):
            label_class, _ = cells[index]
            text_class, text_html = cells[index + 1]
            option_text = self._clean_text(BeautifulSoup(text_html, "html.parser").get_text(" ", strip=True))
            if not option_text:
                continue

            option_classes = {label_class.lower(), text_class.lower()}
            if "mcgood" in option_classes:
                correct_option = option_text
            options.append(option_text)

        return options, correct_option

    def _extract_image_url(self, soup: BeautifulSoup) -> str:
        high_res_button = soup.find("input", attrs={"value": re.compile("High Resolution Image", re.I)})
        if high_res_button is not None:
            onclick = high_res_button.get("onclick", "")
            match = ONCLICK_URL_PATTERN.search(onclick)
            if match:
                raw_url = match.group(1) or match.group(2)
                return urljoin(self.base_url, raw_url)

        image = soup.select_one("img[src*='/mavendata/images/']")
        if image and image.get("src"):
            return urljoin(self.base_url, image["src"])

        raise ValueError("Unable to locate ECG image on the case page.")

    def _extract_diagnosis_from_explanation(self, explanation: str) -> str:
        cleaned = self._clean_text(explanation)
        if not cleaned:
            return "Diagnosis unavailable"

        prefixed_match = re.match(r"^(?:Dx|Diagnosis)\s*:\s*(.+?)(?:\.|;|$)", cleaned, re.IGNORECASE)
        if prefixed_match:
            return self._clean_text(prefixed_match.group(1))

        first_sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
        return self._clean_text(first_sentence[:180])

    def _clean_text(self, value: str) -> str:
        text = unescape(value).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _clean_multiline_text(self, value: str) -> str:
        text = unescape(value).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        cleaned_lines = [" ".join(line.split()).strip() for line in text.split("\n")]
        normalized = "\n".join(cleaned_lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()
