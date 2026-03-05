from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import KBUsageMetric

log = logging.getLogger(__name__)


@dataclass
class KBSearchMetrics:
    """Single knowledge-base retrieval metric event."""

    group_id: int
    query: str
    search_status: str  # success | failed | empty | not_run
    hit_count: int
    reliable_count: int
    max_score: float
    reply_generated: bool
    reply_is_no_answer: bool
    reply_length: int
    elapsed_ms: int


class KBMetricsCollector:
    """Collect and query knowledge-base usage metrics."""

    async def record_search(self, session: AsyncSession, metrics: KBSearchMetrics) -> None:
        """Record a single retrieval metric event."""
        entry = KBUsageMetric(
            group_id=metrics.group_id,
            query=(metrics.query or "")[:200],
            search_status=(metrics.search_status or "unknown")[:20],
            hit_count=max(0, int(metrics.hit_count)),
            reliable_count=max(0, int(metrics.reliable_count)),
            max_score=float(metrics.max_score or 0.0),
            reply_generated=bool(metrics.reply_generated),
            reply_is_no_answer=bool(metrics.reply_is_no_answer),
            reply_length=max(0, int(metrics.reply_length)),
            elapsed_ms=max(0, int(metrics.elapsed_ms)),
            created_at=datetime.now(timezone.utc),
        )
        session.add(entry)
        await session.flush()

        log.info(
            "[KB_METRICS] group=%d status=%s hit=%d reliable=%d no_answer=%s",
            metrics.group_id,
            metrics.search_status,
            metrics.hit_count,
            metrics.reliable_count,
            metrics.reply_is_no_answer,
        )

    async def get_stats(self, session: AsyncSession, group_id: int, days: int = 7) -> dict:
        """Get aggregate KB usage stats for a group."""
        period_days = max(1, int(days))
        since = datetime.now(timezone.utc) - timedelta(days=period_days)

        stmt = select(
            func.count(KBUsageMetric.id).label("total"),
            func.sum(case((KBUsageMetric.search_status == "success", 1), else_=0)).label("success_count"),
            func.sum(case((KBUsageMetric.search_status == "failed", 1), else_=0)).label("failed_count"),
            func.sum(case((KBUsageMetric.search_status == "empty", 1), else_=0)).label("empty_count"),
            func.sum(case((KBUsageMetric.hit_count > 0, 1), else_=0)).label("hit_queries"),
            func.sum(case((KBUsageMetric.reliable_count > 0, 1), else_=0)).label("reliable_queries"),
            func.sum(case((KBUsageMetric.reply_is_no_answer.is_(True), 1), else_=0)).label("no_answer_count"),
            func.avg(KBUsageMetric.max_score).label("avg_max_score"),
            func.avg(KBUsageMetric.elapsed_ms).label("avg_elapsed_ms"),
        ).where(
            KBUsageMetric.group_id == group_id,
            KBUsageMetric.created_at >= since,
        )
        row = (await session.execute(stmt)).one()

        total = int(row.total or 0)
        success_count = int(row.success_count or 0)
        failed_count = int(row.failed_count or 0)
        empty_count = int(row.empty_count or 0)
        hit_queries = int(row.hit_queries or 0)
        reliable_queries = int(row.reliable_queries or 0)
        no_answer_count = int(row.no_answer_count or 0)
        avg_max_score = float(row.avg_max_score or 0.0)
        avg_elapsed_ms = float(row.avg_elapsed_ms or 0.0)

        def _rate(value: int) -> float:
            if total <= 0:
                return 0.0
            return value / total

        return {
            "group_id": group_id,
            "period_days": period_days,
            "since": since.isoformat(),
            "total_events": total,
            "search_status_counts": {
                "success": success_count,
                "failed": failed_count,
                "empty": empty_count,
            },
            "hit_rate": _rate(hit_queries),
            "high_confidence_hit_rate": _rate(reliable_queries),
            "no_trusted_answer_rate": _rate(no_answer_count),
            "average_max_score": avg_max_score,
            "average_elapsed_ms": avg_elapsed_ms,
        }
