from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.ecg_wave_maven import ParsedWaveMavenCase, WaveMavenClient
from bot.database.models import ECGCase
from bot.services.state_store import StateStore

LOGGER = logging.getLogger(__name__)

STATE_LAST_CACHE_FILL_AT = "last_cache_fill_at"


@dataclass(slots=True)
class CacheFillReport:
    requested: int
    stored: int
    skipped: int


class ECGCacheService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        wave_maven_client: WaveMavenClient,
        state_store: StateStore,
        *,
        refill_threshold: int,
        batch_size: int,
    ) -> None:
        self.session_factory = session_factory
        self.wave_maven_client = wave_maven_client
        self.state_store = state_store
        self.refill_threshold = refill_threshold
        self.batch_size = batch_size
        self._fill_lock = asyncio.Lock()

    async def count_total_cases(self) -> int:
        async with self.session_factory() as session:
            return len((await session.scalars(select(ECGCase.id))).all())

    async def count_unused_cases(self) -> int:
        async with self.session_factory() as session:
            cases = await session.scalars(select(ECGCase.id).where(ECGCase.used.is_(False)))
            return len(cases.all())

    async def fill_cache_if_needed(self, *, force: bool = False) -> CacheFillReport:
        async with self._fill_lock:
            if not force:
                unused = await self.count_unused_cases()
                if unused >= self.refill_threshold:
                    LOGGER.info(
                        "Cache check: %s unused ECG cases available. Refill threshold is %s, skipping fetch.",
                        unused,
                        self.refill_threshold,
                    )
                    return CacheFillReport(requested=0, stored=0, skipped=0)

            return await self._fill_cache_locked()

    async def _fill_cache_locked(self) -> CacheFillReport:
        LOGGER.info("Refreshing ECG cache from Wave-Maven.")
        index_entries = await self.wave_maven_client.fetch_case_index()
        LOGGER.info("Wave-Maven case index loaded: %s entries discovered.", len(index_entries))
        async with self.session_factory() as session:
            existing_rownums = set((await session.scalars(select(ECGCase.rownum))).all())
            existing_diagnoses = [item for item in (await session.scalars(select(ECGCase.diagnosis))).all() if item]

        candidates = [entry for entry in reversed(index_entries) if entry.rownum not in existing_rownums]
        candidates = candidates[: self.batch_size]
        if not candidates:
            LOGGER.info("No new ECG cases were found for caching.")
            return CacheFillReport(requested=0, stored=0, skipped=0)

        LOGGER.info("Fetching %s new ECG cases into cache.", len(candidates))
        parsed_cases: list[ParsedWaveMavenCase] = []
        for entry in candidates:
            try:
                LOGGER.info("Fetching Wave-Maven case rownum=%s.", entry.rownum)
                parsed_cases.append(await self.wave_maven_client.fetch_case(entry))
            except Exception as exc:  # pragma: no cover - network/HTML failure path
                LOGGER.warning("Failed to fetch Wave-Maven case rownum=%s: %s", entry.rownum, exc)

        diagnosis_pool = self._deduplicate_labels(
            existing_diagnoses + [item.diagnosis for item in parsed_cases if item.diagnosis]
        )

        stored = 0
        skipped = 0
        async with self.session_factory() as session:
            for parsed_case in parsed_cases:
                prepared = self._prepare_poll_options(parsed_case, diagnosis_pool)
                if prepared is None:
                    skipped += 1
                    LOGGER.warning(
                        "Skipping case rownum=%s because four poll options could not be prepared.",
                        parsed_case.rownum,
                    )
                    continue

                options, correct_index = prepared
                session.add(
                    ECGCase(
                        source_url=parsed_case.source_url,
                        image_url=parsed_case.image_url,
                        vignette=parsed_case.vignette,
                        diagnosis=parsed_case.diagnosis,
                        explanation=parsed_case.explanation,
                        options_json=json.dumps(options, ensure_ascii=True),
                        correct_option_index=correct_index,
                        used=False,
                        rownum=parsed_case.rownum,
                        case_identifier=parsed_case.case_id,
                        source_options_json=json.dumps(parsed_case.source_options, ensure_ascii=True),
                    )
                )
                stored += 1

            await session.commit()

        if stored:
            from datetime import UTC, datetime

            await self.state_store.set_datetime(STATE_LAST_CACHE_FILL_AT, datetime.now(UTC))

        LOGGER.info(
            "ECG cache refresh complete. requested=%s stored=%s skipped=%s",
            len(candidates),
            stored,
            skipped,
        )
        return CacheFillReport(requested=len(candidates), stored=stored, skipped=skipped)

    async def get_next_case(self) -> ECGCase | None:
        await self.fill_cache_if_needed()

        async with self.session_factory() as session:
            case = await session.scalar(
                select(ECGCase)
                .where(ECGCase.used.is_(False))
                .order_by(ECGCase.created_at.asc(), ECGCase.id.asc())
            )
            if case is not None:
                LOGGER.info("Selected cached ECG case rownum=%s.", case.rownum)
                return case

        refill_report = await self.fill_cache_if_needed(force=True)
        if refill_report.stored:
            async with self.session_factory() as session:
                case = await session.scalar(
                    select(ECGCase)
                    .where(ECGCase.used.is_(False))
                    .order_by(ECGCase.created_at.asc(), ECGCase.id.asc())
                )
                if case is not None:
                    LOGGER.info("Selected newly cached ECG case rownum=%s.", case.rownum)
                return case

        LOGGER.info("No unseen ECG cases remain in the cache or on the fetched Wave-Maven index.")
        return None

    async def mark_case_used(self, case_id: int) -> None:
        async with self.session_factory() as session:
            case = await session.get(ECGCase, case_id)
            if case is None:
                return
            case.used = True
            await session.commit()

    async def reset_usage_cycle(self) -> None:
        async with self.session_factory() as session:
            await session.execute(update(ECGCase).values(used=False))
            await session.commit()
        LOGGER.info("Reset all ECG cases to unused.")

    def _prepare_poll_options(self, parsed_case: ParsedWaveMavenCase, diagnosis_pool: list[str]) -> tuple[list[str], int] | None:
        correct = self._clean_label(parsed_case.correct_option or parsed_case.diagnosis)
        if not correct:
            return None

        wrong_options = [
            option
            for option in parsed_case.source_options
            if self._normalize_label(option) != self._normalize_label(correct)
        ]
        wrong_options = self._deduplicate_labels(wrong_options)

        if len(wrong_options) < 3:
            filler = [
                diagnosis
                for diagnosis in diagnosis_pool
                if self._normalize_label(diagnosis) != self._normalize_label(correct)
                and self._normalize_label(diagnosis) not in {self._normalize_label(item) for item in wrong_options}
            ]
            random.shuffle(filler)
            wrong_options.extend(filler[: 3 - len(wrong_options)])

        if len(wrong_options) < 3:
            return None

        poll_options = wrong_options[:3] + [correct]
        random.shuffle(poll_options)
        return poll_options, poll_options.index(correct)

    def _deduplicate_labels(self, values: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = self._clean_label(value)
            if not cleaned:
                continue
            normalized = self._normalize_label(cleaned)
            if normalized in seen:
                continue
            unique.append(cleaned)
            seen.add(normalized)
        return unique

    def _clean_label(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip(" .;:")

    def _normalize_label(self, value: str) -> str:
        return self._clean_label(value).casefold()
