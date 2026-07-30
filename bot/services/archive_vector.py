from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextvars import Context
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import logging
import math
import re
import struct
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import GroupMessageArchive, GroupMessageArchiveEmbedding
from bot.services.llm import EmbeddingBatchResult, LLMService
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 16
_DEFAULT_BACKFILL_PER_PASS = 64
_DEFAULT_INPUT_MAX_CHARS = 2048
_DEFAULT_SCAN_LIMIT = 4096
_DEFAULT_CANDIDATE_LIMIT = 64
_DEFAULT_QUERY_TIMEOUT_SECONDS = 2.5
_DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 5.0
_INDEXING_LEASE_SECONDS = 120.0
_RETRY_BASE_SECONDS = 30.0
_RETRY_MAX_SECONDS = 60.0 * 60.0
_FLOAT16_BYTES = 2


@dataclass(frozen=True, slots=True)
class SQLiteArchiveVectorCandidate:
    """One semantic candidate compatible with MemoryService's protocol."""

    message_key: str
    score: float


@dataclass(frozen=True, slots=True)
class _IndexSource:
    archive_id: int
    group_id: int
    message_key: str
    attempt_count: int
    text: str
    source_hash: str


@dataclass(frozen=True, slots=True)
class _StoredVector:
    message_key: str
    dimensions: int
    encoding: str
    embedding: bytes
    embedding_norm: float


def _normalized_part(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _canonical_archive_text(row: Any, *, max_chars: int) -> str:
    """Build a bounded semantic document without changing the raw archive."""

    parts: list[str] = []
    seen: set[str] = set()

    def append(label: str, value: Any) -> None:
        normalized = _normalized_part(value)
        if not normalized:
            return
        dedupe_key = normalized.casefold()
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        parts.append(f"{label}: {normalized}")

    sender = _normalized_part(getattr(row, "sender_display_name", ""))
    username = _normalized_part(getattr(row, "sender_username", ""))
    if sender or username:
        append("sender", " ".join(value for value in (sender, username) if value))
    append("message", getattr(row, "raw_text", ""))
    append("message", getattr(row, "content", ""))
    append("derived", getattr(row, "derived_text", ""))
    append("reply_context", getattr(row, "reply_to_content", ""))

    text = "\n".join(parts)
    return text[: max(1, int(max_chars))]


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack_float16(vector: Iterable[float]) -> tuple[bytes, int, float] | None:
    values: list[float] = []
    norm_squared = 0.0
    for raw_value in vector:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        values.append(value)
        norm_squared += value * value
    if not values or norm_squared <= 0.0 or not math.isfinite(norm_squared):
        return None
    try:
        blob = struct.pack(f"<{len(values)}e", *values)
    except (OverflowError, struct.error):
        return None
    return blob, len(values), math.sqrt(norm_squared)


def _rank_stored_vectors(
    query_vector: list[float],
    rows: list[_StoredVector],
    *,
    limit: int,
) -> list[SQLiteArchiveVectorCandidate]:
    query_values: list[float] = []
    query_norm_squared = 0.0
    for raw_value in query_vector:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(value):
            return []
        query_values.append(value)
        query_norm_squared += value * value
    if not query_values or query_norm_squared <= 0.0:
        return []
    query_norm = math.sqrt(query_norm_squared)

    ranked: list[SQLiteArchiveVectorCandidate] = []
    for row in rows:
        if row.encoding != "f16le" or row.dimensions != len(query_values):
            continue
        if len(row.embedding) != row.dimensions * _FLOAT16_BYTES:
            continue
        if row.embedding_norm <= 0.0 or not math.isfinite(row.embedding_norm):
            continue
        try:
            values = struct.unpack(f"<{row.dimensions}e", row.embedding)
        except struct.error:
            continue
        dot = sum(left * right for left, right in zip(query_values, values))
        score = dot / (query_norm * row.embedding_norm)
        if not math.isfinite(score):
            continue
        ranked.append(
            SQLiteArchiveVectorCandidate(
                message_key=row.message_key,
                score=max(-1.0, min(1.0, score)),
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[: max(1, int(limit))]


class SQLiteArchiveVectorRecallProvider:
    """SQLite-backed, dependency-free semantic recall for archive messages.

    The implementation deliberately favors predictable deployment over an ANN
    extension: it scans at most ``scan_limit`` recent ready vectors and moves
    float16 decoding/cosine work to a bounded worker thread.  FTS5 remains the
    complete lexical path; this provider is supplemental and returns an empty
    result on embedding or index failures.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMService,
        retention_days: int = 7,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        backfill_per_pass: int = _DEFAULT_BACKFILL_PER_PASS,
        input_max_chars: int = _DEFAULT_INPUT_MAX_CHARS,
        scan_limit: int = _DEFAULT_SCAN_LIMIT,
        candidate_limit: int = _DEFAULT_CANDIDATE_LIMIT,
        query_timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS,
        maintenance_interval_seconds: float = (
            _DEFAULT_MAINTENANCE_INTERVAL_SECONDS
        ),
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self.retention_days = min(365, max(1, int(retention_days)))
        self.batch_size = min(64, max(1, int(batch_size)))
        self.backfill_per_pass = min(512, max(1, int(backfill_per_pass)))
        self.input_max_chars = min(16_384, max(128, int(input_max_chars)))
        self.scan_limit = min(4096, max(64, int(scan_limit)))
        self.candidate_limit = min(64, max(1, int(candidate_limit)))
        self.query_timeout_seconds = min(
            10.0,
            max(0.25, float(query_timeout_seconds)),
        )
        self.maintenance_interval_seconds = max(
            0.25,
            float(maintenance_interval_seconds),
        )
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._index_lock = asyncio.Lock()
        self._scan_slots = asyncio.Semaphore(2)

    def reconfigure(self, *, retention_days: int) -> None:
        self.retention_days = min(365, max(1, int(retention_days)))
        self.notify_archive_changed()

    def notify_archive_changed(self) -> None:
        self._wake_event.set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self.run(),
            name="archive-vector-indexer",
            context=Context(),
        )

    async def shutdown(self, *, timeout_seconds: float = 3.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        task = self._task
        if task is None:
            return
        if not task.done():
            try:
                async with asyncio.timeout(max(0.05, float(timeout_seconds))):
                    await task
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def run(self) -> None:
        """Continuously drain persistent pending jobs with bounded batches."""

        while not self._stop_event.is_set():
            processed = 0
            try:
                processed = await self.index_pending_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("archive vector indexing pass failed")

            if self._stop_event.is_set():
                break
            if processed >= self.backfill_per_pass:
                await asyncio.sleep(0.25)
                continue

            self._wake_event.clear()
            try:
                async with asyncio.timeout(self.maintenance_interval_seconds):
                    await self._wake_event.wait()
            except TimeoutError:
                pass

    async def _claim_pending_batch(
        self,
        *,
        space_id: str,
        limit: int,
    ) -> tuple[list[_IndexSource], int]:
        now = now_shanghai_naive()
        cutoff = now - timedelta(days=self.retention_days)
        lease_until = now + timedelta(seconds=_INDEXING_LEASE_SECONDS)
        retry_ready = or_(
            GroupMessageArchiveEmbedding.next_attempt_at.is_(None),
            GroupMessageArchiveEmbedding.next_attempt_at <= now,
        )
        eligible = or_(
            GroupMessageArchiveEmbedding.status == "pending",
            and_(
                GroupMessageArchiveEmbedding.status == "failed",
                retry_ready,
            ),
            and_(
                GroupMessageArchiveEmbedding.status == "indexing",
                retry_ready,
            ),
            and_(
                GroupMessageArchiveEmbedding.status == "ready",
                GroupMessageArchiveEmbedding.space_id != space_id,
            ),
        )

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        GroupMessageArchiveEmbedding.archive_id,
                        GroupMessageArchiveEmbedding.group_id,
                        GroupMessageArchiveEmbedding.message_key,
                        GroupMessageArchiveEmbedding.attempt_count,
                        GroupMessageArchive.sender_display_name,
                        GroupMessageArchive.sender_username,
                        GroupMessageArchive.content,
                        GroupMessageArchive.raw_text,
                        GroupMessageArchive.derived_text,
                        GroupMessageArchive.reply_to_content,
                    )
                    .join(
                        GroupMessageArchive,
                        GroupMessageArchive.id
                        == GroupMessageArchiveEmbedding.archive_id,
                    )
                    .where(
                        GroupMessageArchive.group_id
                        == GroupMessageArchiveEmbedding.group_id,
                        GroupMessageArchive.sent_at >= cutoff,
                        eligible,
                    )
                    .order_by(
                        GroupMessageArchiveEmbedding.updated_at.asc(),
                        GroupMessageArchiveEmbedding.archive_id.asc(),
                    )
                    .limit(max(1, int(limit)))
                )
            ).all()

            claimed: list[_IndexSource] = []
            skipped = 0
            for row in rows:
                text = _canonical_archive_text(row, max_chars=self.input_max_chars)
                digest = _source_hash(text)
                if not text:
                    await session.execute(
                        update(GroupMessageArchiveEmbedding)
                        .where(
                            GroupMessageArchiveEmbedding.archive_id
                            == int(row.archive_id)
                        )
                        .values(
                            source_hash=digest,
                            space_id=space_id,
                            dimensions=0,
                            encoding="f16le",
                            embedding=None,
                            embedding_norm=None,
                            status="skipped",
                            attempt_count=0,
                            next_attempt_at=None,
                            last_error="",
                            updated_at=now,
                        )
                    )
                    skipped += 1
                    continue
                await session.execute(
                    update(GroupMessageArchiveEmbedding)
                    .where(
                        GroupMessageArchiveEmbedding.archive_id
                        == int(row.archive_id)
                    )
                    .values(
                        source_hash=digest,
                        space_id=space_id,
                        status="indexing",
                        next_attempt_at=lease_until,
                        last_error="",
                        updated_at=now,
                    )
                )
                claimed.append(
                    _IndexSource(
                        archive_id=int(row.archive_id),
                        group_id=int(row.group_id),
                        message_key=str(row.message_key),
                        attempt_count=max(0, int(row.attempt_count or 0)),
                        text=text,
                        source_hash=digest,
                    )
                )
            await session.commit()
        return claimed, skipped

    async def _mark_failed(
        self,
        sources: list[_IndexSource],
        *,
        space_id: str,
        error: str,
    ) -> None:
        now = now_shanghai_naive()
        safe_error = _normalized_part(error)[:512]
        async with self._session_factory() as session:
            for source in sources:
                attempts = source.attempt_count + 1
                delay = min(
                    _RETRY_MAX_SECONDS,
                    _RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 7)),
                )
                await session.execute(
                    update(GroupMessageArchiveEmbedding)
                    .where(
                        GroupMessageArchiveEmbedding.archive_id
                        == source.archive_id,
                        GroupMessageArchiveEmbedding.status == "indexing",
                        GroupMessageArchiveEmbedding.source_hash
                        == source.source_hash,
                        GroupMessageArchiveEmbedding.space_id == space_id,
                    )
                    .values(
                        status="failed",
                        attempt_count=attempts,
                        next_attempt_at=now + timedelta(seconds=delay),
                        last_error=safe_error,
                        updated_at=now,
                    )
                )
            await session.commit()

    async def _store_embedding_batch(
        self,
        sources: list[_IndexSource],
        result: EmbeddingBatchResult,
    ) -> int:
        now = now_shanghai_naive()
        stored = 0
        invalid: list[_IndexSource] = []
        async with self._session_factory() as session:
            for source, vector in zip(sources, result.vectors):
                packed = _pack_float16(vector)
                if packed is None:
                    invalid.append(source)
                    continue
                blob, dimensions, norm = packed
                if dimensions != result.dimensions:
                    invalid.append(source)
                    continue
                update_result = await session.execute(
                    update(GroupMessageArchiveEmbedding)
                    .where(
                        GroupMessageArchiveEmbedding.archive_id
                        == source.archive_id,
                        GroupMessageArchiveEmbedding.status == "indexing",
                        GroupMessageArchiveEmbedding.source_hash
                        == source.source_hash,
                        GroupMessageArchiveEmbedding.space_id == result.space_id,
                    )
                    .values(
                        dimensions=dimensions,
                        encoding="f16le",
                        embedding=blob,
                        embedding_norm=norm,
                        status="ready",
                        attempt_count=0,
                        next_attempt_at=None,
                        last_error="",
                        updated_at=now,
                    )
                )
                stored += max(0, int(update_result.rowcount or 0))
            await session.commit()
        if invalid:
            await self._mark_failed(
                invalid,
                space_id=result.space_id,
                error="invalid embedding vector",
            )
        return stored

    async def index_pending_once(self, *, max_rows: int | None = None) -> int:
        """Index a bounded number of persistent jobs and return work consumed."""

        async with self._index_lock:
            target = min(
                self.backfill_per_pass,
                max(1, int(max_rows or self.backfill_per_pass)),
            )
            processed = 0
            while processed < target:
                space_id = self._llm.primary_embedding_space_id()
                if not space_id:
                    return processed
                sources, skipped = await self._claim_pending_batch(
                    space_id=space_id,
                    limit=min(self.batch_size, target - processed),
                )
                processed += skipped
                if not sources:
                    return processed

                result = await self._llm.embed_primary_with_space(
                    [source.text for source in sources],
                )
                if result is None:
                    await self._mark_failed(
                        sources,
                        space_id=space_id,
                        error="primary embedding provider unavailable",
                    )
                    processed += len(sources)
                    continue
                if (
                    result.space_id != space_id
                    or len(result.vectors) != len(sources)
                ):
                    await self._mark_failed(
                        sources,
                        space_id=space_id,
                        error="embedding space or response count changed",
                    )
                    processed += len(sources)
                    continue
                await self._store_embedding_batch(sources, result)
                processed += len(sources)
            return processed

    async def recall(
        self,
        *,
        group_id: int,
        query: str,
        cutoff: datetime,
        limit: int,
        exclude_message_keys: tuple[str, ...],
    ) -> list[SQLiteArchiveVectorCandidate]:
        """Return group-scoped semantic candidates or an empty fallback."""

        normalized_query = _normalized_part(query)[:1024]
        if not normalized_query:
            return []
        try:
            result = await self._llm.embed_primary_with_space(
                [normalized_query],
                total_deadline_sec=self.query_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("archive vector query embedding failed | group=%s", group_id)
            return []
        if result is None or len(result.vectors) != 1:
            return []

        excluded = {
            str(value)
            for value in exclude_message_keys
            if str(value or "").strip()
        }
        async with self._session_factory() as session:
            stmt = (
                select(
                    GroupMessageArchiveEmbedding.message_key,
                    GroupMessageArchiveEmbedding.dimensions,
                    GroupMessageArchiveEmbedding.encoding,
                    GroupMessageArchiveEmbedding.embedding,
                    GroupMessageArchiveEmbedding.embedding_norm,
                )
                .join(
                    GroupMessageArchive,
                    GroupMessageArchive.id
                    == GroupMessageArchiveEmbedding.archive_id,
                )
                .where(
                    GroupMessageArchiveEmbedding.group_id == int(group_id),
                    GroupMessageArchive.group_id == int(group_id),
                    GroupMessageArchive.sent_at >= cutoff,
                    GroupMessageArchiveEmbedding.space_id == result.space_id,
                    GroupMessageArchiveEmbedding.status == "ready",
                    GroupMessageArchiveEmbedding.dimensions == result.dimensions,
                    GroupMessageArchiveEmbedding.embedding.is_not(None),
                    GroupMessageArchiveEmbedding.embedding_norm.is_not(None),
                )
                .order_by(
                    GroupMessageArchive.sent_at.desc(),
                    GroupMessageArchive.id.desc(),
                )
                .limit(self.scan_limit + len(excluded))
            )
            if excluded:
                stmt = stmt.where(
                    GroupMessageArchiveEmbedding.message_key.not_in(excluded)
                )
            rows = (await session.execute(stmt)).all()

        stored = [
            _StoredVector(
                message_key=str(row.message_key),
                dimensions=int(row.dimensions or 0),
                encoding=str(row.encoding or ""),
                embedding=bytes(row.embedding or b""),
                embedding_norm=float(row.embedding_norm or 0.0),
            )
            for row in rows[: self.scan_limit]
        ]
        if not stored:
            return []
        result_limit = min(
            self.candidate_limit,
            max(1, int(limit)),
        )
        async with self._scan_slots:
            return await asyncio.to_thread(
                _rank_stored_vectors,
                result.vectors[0],
                stored,
                limit=result_limit,
            )

