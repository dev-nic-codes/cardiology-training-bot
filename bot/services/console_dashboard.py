from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import re
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.services.scheduler import SchedulerService
from bot.services.stats_service import BotStatus, ScoreSummary, StatsService
from bot.services.topic_manager import TopicManager

LOGGER = logging.getLogger(__name__)

ANSI_CLEAR = "\033[2J\033[H"
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"

SUCCESS_EVENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"Startup ECG posted successfully\. case_id=(?P<case_id>\d+) poll_id=(?P<poll_id>.+)$"),
        "ECG startup post | case_id={case_id} | poll={poll_id}",
    ),
    (
        re.compile(r"Posted ECG case rownum=(?P<rownum>\d+) with poll_id=(?P<poll_id>.+)\.$"),
        "ECG posted | case #{case_number} | poll={poll_id}",
    ),
    (
        re.compile(r"Startup article posted successfully\. unique_id=(?P<unique_id>.+) pdf_attached=(?P<pdf>True|False)$"),
        "Article startup post | PDF {pdf_flag}",
    ),
    (
        re.compile(r'Posted article "(?P<title>.+)" to topic_id=(?P<topic_id>\d+)\.$'),
        "Article posted | {title}",
    ),
    (
        re.compile(r'Attached PDF for article "(?P<title>.+)" from .+\.$'),
        "Article PDF attached | {title}",
    ),
)

ERROR_EVENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"Startup ECG posting failed\.$"),
        "ECG startup post failed",
    ),
    (
        re.compile(r"Startup article posting failed\.$"),
        "Article startup post failed",
    ),
    (
        re.compile(r"Scheduled ECG posting failed\.$"),
        "ECG scheduled post failed",
    ),
    (
        re.compile(r"Scheduled article posting failed\.$"),
        "Article scheduled post failed",
    ),
    (
        re.compile(r'Failed to upload PDF for article "(?P<title>.+)": .+$'),
        "Article PDF upload failed | {title}",
    ),
    (
        re.compile(r"Failed to send private notification to user_id=\d+: .+$"),
        "Private ECG notification failed",
    ),
    (
        re.compile(r"Failed to fetch articles from (?P<source>.+): .+$"),
        "Article source failed | {source}",
    ),
    (
        re.compile(r"Failed to fetch Wave-Maven case rownum=(?P<rownum>\d+): .+$"),
        "Wave-Maven case fetch failed | case #{case_number}",
    ),
    (
        re.compile(r"Console dashboard update failed\.$"),
        "Console dashboard update failed",
    ),
)


@dataclass(slots=True)
class DashboardSnapshot:
    now: datetime
    uptime: timedelta
    status: BotStatus
    score: ScoreSummary
    ecg_topic_id: int
    articles_topic_id: int
    podcast_topic_id: int
    next_ecg_run: datetime | None
    next_article_run: datetime | None
    next_podcast_run: datetime | None
    recent_events: list["DashboardEvent"]


@dataclass(slots=True)
class DashboardEvent:
    timestamp: str
    kind: str
    text: str


class DashboardEventBuffer(logging.Handler):
    def __init__(self, *, max_events: int = 5) -> None:
        super().__init__(level=logging.INFO)
        self._events: deque[DashboardEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        event = self._build_event(record)
        if event is None:
            return

        with self._lock:
            self._events.append(event)

    def snapshot(self) -> list[DashboardEvent]:
        with self._lock:
            return list(self._events)

    def _build_event(self, record: logging.LogRecord) -> DashboardEvent | None:
        message = record.getMessage().replace("\n", " ").strip()
        if not message:
            return None

        normalized = " ".join(message.split())
        event = self._match_event(normalized, levelno=record.levelno)
        if event is None:
            return None

        event.timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        return event

    def _match_event(self, message: str, *, levelno: int) -> DashboardEvent | None:
        for pattern, template in SUCCESS_EVENT_PATTERNS:
            match = pattern.search(message)
            if not match:
                continue
            groups = self._with_derived_fields(match.groupdict())
            return DashboardEvent(timestamp="", kind="success", text=template.format(**groups))

        for pattern, template in ERROR_EVENT_PATTERNS:
            match = pattern.search(message)
            if not match:
                continue
            groups = self._with_derived_fields(match.groupdict())
            return DashboardEvent(timestamp="", kind="error", text=template.format(**groups))

        if levelno >= logging.ERROR:
            return DashboardEvent(timestamp="", kind="error", text=message)
        return None

    def _with_derived_fields(self, groups: dict[str, str]) -> dict[str, str]:
        derived = dict(groups)
        if "rownum" in groups:
            try:
                derived["case_number"] = str(int(groups["rownum"]) + 1)
            except ValueError:
                derived["case_number"] = groups["rownum"]
        if "pdf" in groups:
            derived["pdf_flag"] = "attached" if groups["pdf"] == "True" else "not attached"
        return derived


class ConsoleDashboard:
    def __init__(
        self,
        *,
        settings: Settings,
        stats_service: StatsService,
        scheduler_service: SchedulerService,
        topic_manager: TopicManager,
        article_topic_manager: TopicManager,
        podcast_topic_manager: TopicManager,
        event_buffer: DashboardEventBuffer,
        refresh_interval_seconds: float = 10.0,
    ) -> None:
        self.settings = settings
        self.stats_service = stats_service
        self.scheduler_service = scheduler_service
        self.topic_manager = topic_manager
        self.article_topic_manager = article_topic_manager
        self.podcast_topic_manager = podcast_topic_manager
        self.event_buffer = event_buffer
        self.refresh_interval_seconds = refresh_interval_seconds
        self.display_zone = ZoneInfo(settings.timezone)
        self.started_at = datetime.now(self.display_zone)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._supports_color = self._enable_ansi_output()

    async def start(self) -> None:
        if self._task is not None:
            return

        self._task = asyncio.create_task(self._run(), name="console-dashboard")

    async def stop(self) -> None:
        if self._task is None:
            return

        self._stop_event.set()
        try:
            await self._task
        finally:
            self._task = None
            sys.stdout.write("\n")
            sys.stdout.flush()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = await self._build_snapshot()
                self._render(snapshot)
            except Exception:
                LOGGER.exception("Console dashboard update failed.")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.refresh_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _build_snapshot(self) -> DashboardSnapshot:
        now = datetime.now(self.display_zone)
        status = await self.stats_service.get_status()
        score = await self.stats_service.get_score(user_id=self.settings.owner_user_id)
        ecg_topic_id = await self.topic_manager.get_topic_id()
        articles_topic_id = await self.article_topic_manager.get_topic_id()
        podcast_topic_id = await self.podcast_topic_manager.get_topic_id()

        return DashboardSnapshot(
            now=now,
            uptime=now - self.started_at,
            status=status,
            score=score,
            ecg_topic_id=ecg_topic_id,
            articles_topic_id=articles_topic_id,
            podcast_topic_id=podcast_topic_id,
            next_ecg_run=self.scheduler_service.next_ecg_run_time(),
            next_article_run=self.scheduler_service.next_article_run_time(),
            next_podcast_run=self.scheduler_service.next_podcast_run_time(),
            recent_events=self.event_buffer.snapshot(),
        )

    def _render(self, snapshot: DashboardSnapshot) -> None:
        width = max(80, min(shutil.get_terminal_size((120, 40)).columns, 160))
        lines = [
            self._paint(self._line("CARDIOLOGY BOT DASHBOARD", width, fill="="), ANSI_CYAN, bold=True),
            self._paint(
                f"Now {snapshot.now.strftime('%Y-%m-%d %H:%M %Z')} | Uptime {self._format_duration(snapshot.uptime)}",
                ANSI_WHITE,
                bold=True,
            ),
            self._paint(
                f"Chat {self.settings.chat_id} | Owner {self.settings.owner_user_id or 'not set'}",
                ANSI_DIM,
            ),
            "",
            self._paint("[ ECG ]", ANSI_GREEN, bold=True),
            f"  Topic {snapshot.ecg_topic_id} | Cache {snapshot.status.unused_cases} unused / {snapshot.status.total_cases} total | Posted {snapshot.status.posted_quizzes} | Answers {snapshot.status.total_answers}",
            f"  Last post {self._format_datetime(snapshot.status.last_ecg_posted_at)}",
            f"  Next post {self._format_next_run(snapshot.now, snapshot.next_ecg_run)}",
            "",
            self._paint("[ ARTICLES ]", ANSI_YELLOW, bold=True),
            f"  Topic {snapshot.articles_topic_id} | Posted {snapshot.status.posted_articles}",
            f"  Last post {self._format_datetime(snapshot.status.last_article_posted_at)}",
            f"  Next post {self._format_next_run(snapshot.now, snapshot.next_article_run)}",
            "",
            self._paint("[ PODCASTS ]", ANSI_CYAN, bold=True),
            f"  Topic {snapshot.podcast_topic_id} | Posted {snapshot.status.posted_podcasts}",
            f"  Last post {self._format_datetime(snapshot.status.last_podcast_posted_at)}",
            f"  Next post {self._format_next_run(snapshot.now, snapshot.next_podcast_run)}",
            "",
            self._paint("[ SCORE ]", ANSI_MAGENTA, bold=True),
            f"  Total {snapshot.score.total_answers} | Correct {snapshot.score.correct_answers} | Wrong {snapshot.score.incorrect_answers} | Accuracy {snapshot.score.accuracy_percent:.1f}%",
            "",
            self._paint("[ ACTIVITY ]", ANSI_BLUE, bold=True),
        ]

        if snapshot.recent_events:
            for event in snapshot.recent_events[-5:]:
                badge = self._event_badge(event.kind)
                event_line = f"  {event.timestamp} {badge} {event.text}"
                lines.append(event_line)
        else:
            lines.append(self._paint("  Waiting for successful posts or errors...", ANSI_DIM))

        output = "\n".join(self._truncate(line, width) for line in lines)
        sys.stdout.write(ANSI_CLEAR + output)
        sys.stdout.flush()

    def _format_datetime(self, value: datetime | None) -> str:
        if value is None:
            return "not yet"
        return value.astimezone(self.display_zone).strftime("%Y-%m-%d %H:%M:%S %Z")

    def _format_next_run(self, now: datetime, value: datetime | None) -> str:
        if value is None:
            return "not scheduled"

        localized = value.astimezone(self.display_zone)
        remaining = localized - now
        if remaining <= timedelta(seconds=0):
            return f"due now ({localized.strftime('%Y-%m-%d %H:%M %Z')})"

        return f"in {self._format_duration(remaining)} ({localized.strftime('%Y-%m-%d %H:%M %Z')})"

    def _format_duration(self, value: timedelta) -> str:
        total_seconds = max(0, int(value.total_seconds()))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _seconds = divmod(remainder, 60)
        if days:
            return f"{days}d {hours:02d}h {minutes:02d}m"
        hours, remainder = divmod(total_seconds, 3600)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m"
        return "under 1m"

    def _line(self, text: str, width: int, *, fill: str = "-") -> str:
        padding = max(0, width - len(text) - 1)
        return f"{text} {fill * padding}"

    def _truncate(self, value: str, limit: int) -> str:
        if self._visible_length(value) <= limit:
            return value
        return self._truncate_ansi(value, limit)

    def _paint(self, value: str, *codes: str, bold: bool = False) -> str:
        if not self._supports_color:
            return value
        prefix = "".join(codes)
        if bold:
            prefix += ANSI_BOLD
        return f"{prefix}{value}{ANSI_RESET}"

    def _event_badge(self, kind: str) -> str:
        if kind == "success":
            return self._paint("OK ", ANSI_GREEN, bold=True)
        return self._paint("ERR", ANSI_RED, bold=True)

    def _visible_length(self, value: str) -> int:
        return len(re.sub(r"\x1b\[[0-9;]*m", "", value))

    def _truncate_ansi(self, value: str, limit: int) -> str:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", value)
        if len(plain) <= limit:
            return value

        visible = 0
        out: list[str] = []
        index = 0
        while index < len(value) and visible < max(0, limit - 3):
            if value[index] == "\x1b":
                match = re.match(r"\x1b\[[0-9;]*m", value[index:])
                if match:
                    out.append(match.group(0))
                    index += len(match.group(0))
                    continue
            out.append(value[index])
            visible += 1
            index += 1

        out.append("...")
        if self._supports_color and not "".join(out).endswith(ANSI_RESET):
            out.append(ANSI_RESET)
        return "".join(out)

    def _enable_ansi_output(self) -> bool:
        if not sys.stdout.isatty():
            return False
        if os.name != "nt":
            return True

        try:
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if mode.value & 0x0004:
                return True
            return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:
            return False
