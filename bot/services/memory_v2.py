
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import MemoryV2Config
from bot.db.models import MessageVector, SemanticFact, UserPreference
from bot.services.llm import LLMService

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return _utc_now()
    return _utc_now()


@dataclass(slots=True)
class AttentionPlan:
    top_k: int
    candidate_multiplier: int
    similarity_weight: float
    time_weight: float
    importance_weight: float
    working_items: int
    include_semantic: bool
    include_procedural: bool
    include_episodic: bool


class ImportanceScorer:
    """Message importance scorer."""

    def __init__(
        self,
        llm: LLMService,
        *,
        llm_enabled: bool,
        llm_min: float,
        llm_max: float,
    ) -> None:
        self.llm = llm
        self.llm_enabled = llm_enabled
        self.llm_min = min(1.0, max(0.0, llm_min))
        self.llm_max = min(1.0, max(0.0, llm_max))
        self.weights = {
            "has_time_reference": 0.30,
            "has_decision": 0.40,
            "has_question": 0.20,
            "has_entity": 0.15,
            "is_factual": 0.25,
            "emotional_intensity": -0.10,
        }

    def rule_score(self, content: str) -> float:
        text = (content or "").strip()
        if not text:
            return 0.0

        score = 0.5
        time_patterns = [
            r"\d+[年月日号天周]",
            r"明天|后天|下周|下月|今晚|明早|今天|昨天|前天",
            r"\d{1,2}:\d{1,2}",
        ]
        if any(re.search(p, text) for p in time_patterns):
            score += self.weights["has_time_reference"]

        decision_keywords = ["决定", "确定", "安排", "计划", "约定", "同意", "批准", "必须", "应该"]
        if any(kw in text for kw in decision_keywords):
            score += self.weights["has_decision"]

        if "?" in text or "？" in text or text.startswith(("什么", "怎么", "为什么", "哪里", "何时")):
            score += self.weights["has_question"]

        if re.search(r"@[\w_]{3,}|[\u4e00-\u9fff]{2,4}(?:先生|女士|同学|老师)?", text):
            score += self.weights["has_entity"]

        factual_markers = ["是", "有", "在", "位于", "发生", "完成", "发布", "上线"]
        if any(m in text for m in factual_markers) and len(text) >= 8:
            score += self.weights["is_factual"]

        if len(text) < 10 and re.fullmatch(r"[哈呵嘿嗯啊哦哎呀!！?？。.~\s]+", text):
            score += self.weights["emotional_intensity"]

        return max(0.0, min(1.0, score))

    async def score(self, content: str, context: dict[str, Any] | None = None) -> float:
        rule = self.rule_score(content)
        if not self.llm_enabled:
            return rule
        if not (self.llm_min <= rule <= self.llm_max):
            return rule

        prompt = (
            "评估以下消息的重要性(0-1分)。\n\n"
            f"消息: {content}\n"
            f"上下文: {json.dumps(context or {}, ensure_ascii=False)[:500]}\n\n"
            "评分标准:\n"
            "- 0.9-1.0: 关键决策、重要约定、核心事实\n"
            "- 0.7-0.9: 有价值信息、明确问答\n"
            "- 0.5-0.7: 一般对话、观点表达\n"
            "- 0.3-0.5: 闲聊、简单回应\n"
            "- 0.0-0.3: 纯表情、无意义内容\n\n"
            "只返回数字分数。"
        )
        try:
            result = await self.llm.decision("你是消息重要性评估专家", prompt)
            llm_score = float(str(result).strip())
            return max(0.0, min(1.0, (rule + llm_score) / 2))
        except Exception:
            return rule


class AttentionController:
    """Decide which memory channels to prioritize for current query."""

    def __init__(self, config: MemoryV2Config) -> None:
        self.config = config

    def plan(self, query: str) -> AttentionPlan:
        text = (query or "").strip()
        similarity = max(0.0, self.config.similarity_weight)
        time_w = max(0.0, self.config.time_weight)
        importance = max(0.0, self.config.importance_weight)
        top_k = max(1, self.config.hybrid_top_k)

        include_semantic = True
        include_procedural = bool(re.search(r"喜欢|偏好|习惯|通常|规则|约定", text))
        include_episodic = True

        if re.search(r"昨天|今天|明天|刚刚|最近|上周|下周", text):
            time_w += 0.15
        if re.search(r"决定|安排|计划|约定|总结", text):
            importance += 0.10
        if re.search(r"什么|谁|哪里|为何|为什么|怎么|何时|\?|？", text):
            top_k = min(top_k + 5, top_k * 2)

        total = similarity + time_w + importance
        if total <= 0:
            similarity, time_w, importance = 0.4, 0.3, 0.3
        else:
            similarity /= total
            time_w /= total
            importance /= total

        return AttentionPlan(
            top_k=top_k,
            candidate_multiplier=max(1, self.config.retrieval_candidate_multiplier),
            similarity_weight=similarity,
            time_weight=time_w,
            importance_weight=importance,
            working_items=max(1, self.config.working_recent_items),
            include_semantic=include_semantic,
            include_procedural=include_procedural,
            include_episodic=include_episodic,
        )

    @staticmethod
    def to_system_text(plan: AttentionPlan) -> str:
        return (
            "[attention-controller]\n"
            f"top_k={plan.top_k} candidate_multiplier={plan.candidate_multiplier}\n"
            f"weights(similarity={plan.similarity_weight:.3f}, recency={plan.time_weight:.3f}, importance={plan.importance_weight:.3f})\n"
            f"channels(episodic={plan.include_episodic}, semantic={plan.include_semantic}, procedural={plan.include_procedural})"
        )

class VectorMemoryStore:
    """Episodic memory store: vectors in qdrant, metadata in message_vectors table."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        host: str,
        port: int,
        collection_prefix: str,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("qdrant-client is required for memory v2") from exc

        self.session_factory = session_factory
        self.client = QdrantClient(host=host, port=port)
        self.collection_prefix = collection_prefix
        self._distance = Distance
        self._point_ids_list = PointIdsList
        self._point_struct = PointStruct
        self._vector_params = VectorParams
        self._collections_ready: set[str] = set()
        self._collection_lock = asyncio.Lock()

    def _collection(self, group_id: int) -> str:
        return f"{self.collection_prefix}_{group_id}"

    @staticmethod
    def _point_id(message_id: str) -> str:
        # Qdrant point id must be uint or UUID; use deterministic UUID to keep idempotent upsert/delete.
        return str(uuid5(NAMESPACE_URL, f"memory-v2:{message_id}"))

    async def _ensure_collection(self, group_id: int, vector_size: int) -> str:
        name = self._collection(group_id)
        if name in self._collections_ready:
            return name
        async with self._collection_lock:
            if name in self._collections_ready:
                return name
            try:
                await asyncio.to_thread(self.client.get_collection, name)
            except Exception:
                await asyncio.to_thread(
                    self.client.create_collection,
                    collection_name=name,
                    vectors_config=self._vector_params(size=vector_size, distance=self._distance.COSINE),
                )
            self._collections_ready.add(name)
        return name

    async def upsert_messages(self, *, group_id: int, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        vector_size = len(items[0]["embedding"])
        name = await self._ensure_collection(group_id, vector_size)

        points = []
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            message_id = str(item.get("message_id", "")).strip()[:64]
            if not message_id:
                continue
            point_id = self._point_id(message_id)
            metadata = item["metadata"]
            points.append(
                self._point_struct(
                    id=point_id,
                    vector=item["embedding"],
                    payload={
                        "message_id": message_id,
                        "role": metadata.get("role", "user"),
                        "content": metadata.get("content", ""),
                        "timestamp": _to_utc(metadata.get("timestamp")).isoformat(),
                        "importance_score": float(metadata.get("importance_score", 0.5)),
                        "user_id": metadata.get("user_id"),
                        "message_type": metadata.get("message_type", "text"),
                    },
                )
            )
            normalized_items.append(
                {
                    "message_id": message_id,
                    "point_id": point_id,
                    "metadata": metadata,
                }
            )
        if not normalized_items:
            return
        await asyncio.to_thread(self.client.upsert, collection_name=name, points=points)

        message_ids = [it["message_id"] for it in normalized_items]
        async with self.session_factory() as session:
            stmt = select(MessageVector).where(MessageVector.message_id.in_(message_ids))
            existing = {row.message_id: row for row in (await session.execute(stmt)).scalars().all()}
            for item in normalized_items:
                message_id = item["message_id"]
                metadata = item["metadata"]
                created_at = _to_utc(metadata.get("timestamp"))
                importance = float(metadata.get("importance_score", 0.5))
                role = str(metadata.get("role", "user"))[:16]
                content = str(metadata.get("content", ""))

                row = existing.get(message_id)
                if row is None:
                    session.add(
                        MessageVector(
                            group_id=group_id,
                            message_id=message_id,
                            role=role,
                            content=content,
                            importance_score=max(0.0, min(1.0, importance)),
                            vector_id=item["point_id"],
                            created_at=created_at,
                        )
                    )
                else:
                    row.group_id = group_id
                    row.role = role
                    row.content = content
                    row.importance_score = max(0.0, min(1.0, importance))
                    row.vector_id = item["point_id"]
                    row.created_at = created_at
            await session.commit()

    async def upsert_message(
        self,
        *,
        group_id: int,
        message_id: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        await self.upsert_messages(
            group_id=group_id,
            items=[{"message_id": message_id, "embedding": embedding, "metadata": metadata}],
        )

    async def _access_map(self, message_ids: list[str]) -> dict[str, int]:
        if not message_ids:
            return {}
        async with self.session_factory() as session:
            stmt = select(MessageVector).where(MessageVector.message_id.in_(message_ids))
            rows = list((await session.execute(stmt)).scalars().all())
        return {row.message_id: int(row.access_count or 0) for row in rows}

    async def search(self, *, group_id: int, query_vector: list[float], limit: int) -> list[dict[str, Any]]:
        name = self._collection(group_id)
        try:
            result = await asyncio.to_thread(
                self.client.search,
                collection_name=name,
                query_vector=query_vector,
                limit=max(1, limit),
                with_payload=True,
            )
        except Exception as exc:
            # For new groups (collection not created yet) or temporary vector backend issues,
            # fall back to working/semantic/procedural memories instead of breaking replies.
            log.debug("episodic search skipped: group=%s reason=%s", group_id, exc)
            return []

        search_ids: list[str] = []
        for point in result:
            payload = dict(getattr(point, "payload", {}) or {})
            message_id = str(payload.get("message_id") or getattr(point, "id", "")).strip()
            if message_id:
                search_ids.append(message_id)
        access_map = await self._access_map(search_ids)
        rows: list[dict[str, Any]] = []
        for point in result:
            payload = dict(getattr(point, "payload", {}) or {})
            message_id = str(payload.get("message_id") or getattr(point, "id", "")).strip()
            if not message_id:
                continue
            rows.append(
                {
                    "id": message_id,
                    "score": float(getattr(point, "score", 0.0)),
                    "metadata": {
                        "role": str(payload.get("role", "user")),
                        "content": str(payload.get("content", "")),
                        "timestamp": _to_utc(payload.get("timestamp")),
                        "importance_score": float(payload.get("importance_score", 0.5)),
                        "user_id": payload.get("user_id"),
                        "message_type": str(payload.get("message_type", "text")),
                        "access_count": access_map.get(message_id, 0),
                    },
                }
            )
        return rows

    async def bump_access(self, *, message_ids: list[str]) -> None:
        if not message_ids:
            return
        async with self.session_factory() as session:
            stmt = (
                update(MessageVector)
                .where(MessageVector.message_id.in_(message_ids))
                .values(access_count=MessageVector.access_count + 1, last_accessed=_utc_now())
            )
            await session.execute(stmt)
            await session.commit()

    async def search_by_filter(
        self,
        *,
        group_id: int,
        newer_than: datetime | None = None,
        older_than: datetime | None = None,
        min_importance: float | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        stmt = select(MessageVector).where(MessageVector.group_id == group_id)
        if newer_than is not None:
            stmt = stmt.where(MessageVector.created_at >= newer_than)
        if older_than is not None:
            stmt = stmt.where(MessageVector.created_at < older_than)
        if min_importance is not None:
            stmt = stmt.where(MessageVector.importance_score >= min_importance)
        stmt = stmt.order_by(MessageVector.created_at.desc()).limit(max(1, limit))

        async with self.session_factory() as session:
            rows = list((await session.execute(stmt)).scalars().all())

        return [
            {
                "id": row.message_id,
                "metadata": {
                    "role": row.role,
                    "content": row.content,
                    "timestamp": _to_utc(row.created_at),
                    "importance_score": float(row.importance_score or 0.5),
                    "access_count": int(row.access_count or 0),
                },
            }
            for row in rows
        ]

    async def fetch_recent_messages(self, *, group_id: int, limit: int) -> list[dict[str, str]]:
        stmt = (
            select(MessageVector)
            .where(MessageVector.group_id == group_id)
            .order_by(MessageVector.created_at.desc())
            .limit(max(1, limit))
        )
        async with self.session_factory() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()
        return [{"role": row.role, "content": row.content} for row in rows]

    async def list_group_ids(self) -> list[int]:
        async with self.session_factory() as session:
            stmt = select(MessageVector.group_id).distinct()
            result = await session.execute(stmt)
            return [int(v) for v in result.scalars().all() if v is not None]

    async def delete_batch(self, *, group_id: int, message_ids: list[str]) -> int:
        if not message_ids:
            return 0

        name = self._collection(group_id)
        try:
            point_ids = [self._point_id(mid) for mid in message_ids if mid]
            selector = self._point_ids_list(points=point_ids)
            await asyncio.to_thread(self.client.delete, collection_name=name, points_selector=selector)
        except Exception:
            log.exception("qdrant delete failed: group=%s count=%d", group_id, len(message_ids))

        async with self.session_factory() as session:
            stmt = delete(MessageVector).where(
                MessageVector.group_id == group_id,
                MessageVector.message_id.in_(message_ids),
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)


class HybridRetriever:
    """Hybrid retrieval: vector similarity + recency decay + importance."""

    def __init__(self, vector_store: VectorMemoryStore, *, time_decay_factor: float) -> None:
        self.vector_store = vector_store
        self.time_decay_factor = min(1.0, max(0.0, time_decay_factor))

    async def retrieve(
        self,
        *,
        group_id: int,
        query_vector: list[float],
        plan: AttentionPlan,
    ) -> list[dict[str, Any]]:
        recall = max(1, plan.top_k) * max(1, plan.candidate_multiplier)
        candidates = await self.vector_store.search(
            group_id=group_id,
            query_vector=query_vector,
            limit=recall,
        )
        now = _utc_now()
        scored: list[dict[str, Any]] = []
        for item in candidates:
            metadata = item["metadata"]
            sim = float(item.get("score", 0.0))
            msg_time = _to_utc(metadata.get("timestamp"))
            days_ago = max(0.0, (now - msg_time).total_seconds() / 86400.0)
            recency = self.time_decay_factor**days_ago if self.time_decay_factor > 0 else 0.0
            importance = float(metadata.get("importance_score", 0.5))
            final_score = (
                plan.similarity_weight * sim
                + plan.time_weight * recency
                + plan.importance_weight * importance
            )
            scored.append({**item, "final_score": final_score})

        scored.sort(key=lambda x: x["final_score"], reverse=True)
        top = scored[: max(1, plan.top_k)]
        await self.vector_store.bump_access(message_ids=[str(x["id"]) for x in top if x.get("id")])
        return top

class SemanticMemoryStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def upsert_fact(self, *, group_id: int, fact: dict[str, Any], source_message_ids: list[str]) -> bool:
        subject = str(fact.get("subject", "")).strip()
        predicate = str(fact.get("predicate", "")).strip()
        obj = str(fact.get("object", "")).strip()
        if not subject or not predicate or not obj:
            return False

        fact_type = str(fact.get("type", "fact")).strip().lower()[:32] or "fact"
        confidence = max(0.0, min(1.0, float(fact.get("confidence", 0.8))))
        event_time = _to_utc(fact.get("time")) if fact.get("time") else None
        source_ids = sorted({x.strip() for x in source_message_ids if x and x.strip()})

        async with self.session_factory() as session:
            stmt = select(SemanticFact).where(
                SemanticFact.group_id == group_id,
                SemanticFact.subject == subject,
                SemanticFact.predicate == predicate,
                SemanticFact.object == obj,
                SemanticFact.is_active.is_(True),
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                existing.confidence = max(float(existing.confidence or 0.0), confidence)
                existing.fact_type = fact_type
                existing.event_time = event_time or existing.event_time
                existing.source_message_ids = sorted({*(existing.source_message_ids or []), *source_ids})
            else:
                session.add(
                    SemanticFact(
                        group_id=group_id,
                        subject=subject,
                        predicate=predicate,
                        object=obj,
                        fact_type=fact_type,
                        confidence=confidence,
                        source_message_ids=source_ids,
                        event_time=event_time,
                        is_active=True,
                    )
                )
            await session.commit()
            return True

    async def summarize(self, *, group_id: int, limit: int = 8) -> str:
        async with self.session_factory() as session:
            stmt = (
                select(SemanticFact)
                .where(SemanticFact.group_id == group_id, SemanticFact.is_active.is_(True))
                .order_by(SemanticFact.updated_at.desc(), SemanticFact.created_at.desc())
                .limit(max(1, limit))
            )
            rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return ""
        lines = ["[semantic-memory]"]
        for row in rows:
            lines.append(f"- ({float(row.confidence or 0.0):.2f}) {row.subject} -> {row.predicate} -> {row.object}")
        return "\n".join(lines)


class ProceduralMemoryStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    def extract_preferences(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        extracted: list[dict[str, Any]] = []
        for item in messages:
            metadata = item.get("metadata", {}) or {}
            text = str(metadata.get("content", "")).strip()
            if not text:
                continue
            user_id = metadata.get("user_id")

            m1 = re.search(r"我(?:比较)?(?:喜欢|偏好|习惯|通常)\s*([^。！!\n]{1,60})", text)
            if m1 and user_id is not None:
                extracted.append(
                    {
                        "user_id": int(user_id),
                        "key": "personal.preference",
                        "value": m1.group(1).strip(),
                        "confidence": 0.85,
                        "source_ids": [str(item.get("id", ""))],
                    }
                )

            m2 = re.search(r"(?:本群|群里)(?:规则|约定|习惯)[:：]?\s*([^\n]{1,80})", text)
            if m2:
                extracted.append(
                    {
                        "user_id": 0,
                        "key": "group.rule",
                        "value": m2.group(1).strip(),
                        "confidence": 0.8,
                        "source_ids": [str(item.get("id", ""))],
                    }
                )
        return extracted

    async def upsert_preferences(self, *, group_id: int, entries: list[dict[str, Any]]) -> int:
        if not entries:
            return 0
        count = 0
        async with self.session_factory() as session:
            for item in entries:
                key = str(item.get("key", "")).strip()[:64]
                value = str(item.get("value", "")).strip()
                if not key or not value:
                    continue
                user_id = int(item.get("user_id", 0))
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
                source_ids = sorted({x for x in item.get("source_ids", []) if x})

                stmt = select(UserPreference).where(
                    UserPreference.group_id == group_id,
                    UserPreference.user_id == user_id,
                    UserPreference.preference_key == key,
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    session.add(
                        UserPreference(
                            group_id=group_id,
                            user_id=user_id,
                            preference_key=key,
                            preference_value=value,
                            confidence=confidence,
                            learned_from=source_ids,
                        )
                    )
                else:
                    row.preference_value = value
                    row.confidence = max(float(row.confidence or 0.0), confidence)
                    row.learned_from = sorted({*(row.learned_from or []), *source_ids})
                count += 1
            await session.commit()
        return count

    async def summarize(self, *, group_id: int, limit: int = 8) -> str:
        async with self.session_factory() as session:
            stmt = (
                select(UserPreference)
                .where(UserPreference.group_id == group_id)
                .order_by(UserPreference.created_at.desc())
                .limit(max(1, limit))
            )
            rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return ""
        lines = ["[procedural-memory]"]
        for row in rows:
            scope = f"user:{row.user_id}" if int(row.user_id or 0) > 0 else "group"
            lines.append(f"- ({float(row.confidence or 0.0):.2f}) {scope} {row.preference_key} = {row.preference_value}")
        return "\n".join(lines)


class KnowledgeGraph:
    """Optional neo4j graph writer for entity relations."""

    def __init__(self, *, enabled: bool, uri: str, user: str, password: str) -> None:
        self.enabled = enabled and bool(uri and user and password)
        self.driver = None
        if not self.enabled:
            return
        try:
            from neo4j import GraphDatabase

            self.driver = GraphDatabase.driver(uri, auth=(user, password))
        except Exception:
            self.enabled = False
            self.driver = None
            log.exception("neo4j init failed, knowledge graph disabled")

    def add_entity_relation(self, entity1: str, relation: str, entity2: str, context: str) -> None:
        if not self.enabled or self.driver is None:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    """
                    MERGE (a:Entity {name: $entity1})
                    MERGE (b:Entity {name: $entity2})
                    MERGE (a)-[r:RELATION {type: $relation, context: $context}]->(b)
                    SET r.timestamp = datetime()
                    """,
                    entity1=entity1,
                    entity2=entity2,
                    relation=relation,
                    context=context,
                )
        except Exception:
            log.exception("neo4j relation write failed")


class MemoryConsolidation:
    def __init__(
        self,
        *,
        llm: LLMService,
        vector_store: VectorMemoryStore,
        semantic_store: SemanticMemoryStore,
        procedural_store: ProceduralMemoryStore,
        knowledge_graph: KnowledgeGraph,
        min_importance: float,
    ) -> None:
        self.llm = llm
        self.vector_store = vector_store
        self.semantic_store = semantic_store
        self.procedural_store = procedural_store
        self.knowledge_graph = knowledge_graph
        self.min_importance = min(1.0, max(0.0, min_importance))

    @staticmethod
    def _extract_json_array(raw: str) -> list[dict[str, Any]]:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"```$", "", text).strip()
        if "[" not in text or "]" not in text:
            return []
        payload = text[text.find("[") : text.rfind("]") + 1]
        try:
            data = json.loads(payload)
            return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        except Exception:
            return []

    async def _extract_facts(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lines: list[str] = []
        for msg in messages[:200]:
            metadata = msg.get("metadata", {}) or {}
            content = str(metadata.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"[{_to_utc(metadata.get('timestamp')).isoformat()}] {content}")
        if not lines:
            return []

        prompt = (
            "从以下对话中提取关键事实，以 JSON 数组返回，不要输出其他文字。\n\n"
            + "\n".join(lines)
            + "\n\n"
            + "格式: [{\"type\":\"event|decision|preference|fact\",\"subject\":\"主体\",\"predicate\":\"关系/动作\",\"object\":\"客体\",\"time\":\"可选时间\",\"confidence\":0.0}]"
        )
        raw = await self.llm.generate("你是知识提取专家，擅长从对话中抽取结构化信息。", prompt)
        return self._extract_json_array(raw)

    async def consolidate_daily(self, group_id: int) -> dict[str, int]:
        recent = await self.vector_store.search_by_filter(
            group_id=group_id,
            newer_than=_utc_now() - timedelta(days=1),
            min_importance=self.min_importance,
            limit=600,
        )
        if not recent:
            return {"processed": 0, "facts": 0, "preferences": 0}

        facts = await self._extract_facts(recent)
        fact_count = 0
        source_ids = [str(item.get("id", "")) for item in recent if item.get("id")]
        for fact in facts:
            ok = await self.semantic_store.upsert_fact(
                group_id=group_id,
                fact=fact,
                source_message_ids=source_ids,
            )
            if ok:
                fact_count += 1
                s = str(fact.get("subject", "")).strip()
                p = str(fact.get("predicate", "")).strip()
                o = str(fact.get("object", "")).strip()
                if s and p and o:
                    self.knowledge_graph.add_entity_relation(s, p, o, "daily_consolidation")

        preferences = self.procedural_store.extract_preferences(recent)
        pref_count = await self.procedural_store.upsert_preferences(group_id=group_id, entries=preferences)
        return {"processed": len(recent), "facts": fact_count, "preferences": pref_count}


class MemoryPruner:
    def __init__(self, *, vector_store: VectorMemoryStore, retention_days: int) -> None:
        self.vector_store = vector_store
        self.retention_days = max(1, retention_days)

    async def prune_old_memories(self, group_id: int) -> tuple[int, int]:
        cutoff = _utc_now() - timedelta(days=self.retention_days)
        old = await self.vector_store.search_by_filter(group_id=group_id, older_than=cutoff, limit=5000)
        if not old:
            return 0, 0

        to_delete: list[str] = []
        for item in old:
            metadata = item.get("metadata", {}) or {}
            keep = (
                float(metadata.get("importance_score", 0.0)) > 0.7
                or int(metadata.get("access_count", 0)) > 3
                or bool(metadata.get("is_referenced", False))
            )
            if not keep:
                mid = str(item.get("id", "")).strip()
                if mid:
                    to_delete.append(mid)

        deleted = await self.vector_store.delete_batch(group_id=group_id, message_ids=to_delete)
        return len(old), deleted


class MemoryMetrics:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def collect_metrics(self, group_id: int) -> dict[str, float | int]:
        async with self.session_factory() as session:
            vectors = list((await session.execute(select(MessageVector).where(MessageVector.group_id == group_id))).scalars().all())
            facts = list(
                (
                    await session.execute(
                        select(SemanticFact).where(SemanticFact.group_id == group_id, SemanticFact.is_active.is_(True))
                    )
                ).scalars().all()
            )

        total = len(vectors)
        avg_importance = (sum(float(v.importance_score or 0.0) for v in vectors) / total) if total else 0.0
        high_value = sum(1 for v in vectors if float(v.importance_score or 0.0) >= 0.7)
        high_ratio = (high_value / total) if total else 0.0
        fact_rate = (len(facts) / total) if total else 0.0
        return {
            "total_messages": total,
            "vector_count": total,
            "semantic_facts": len(facts),
            "avg_importance_score": round(avg_importance, 4),
            "high_value_ratio": round(high_ratio, 4),
            "fact_extraction_rate": round(fact_rate, 4),
            "retrieval_latency_p95": 0.0,
            "consolidation_duration": 0.0,
            "storage_size_mb": 0.0,
            "context_relevance_score": 0.0,
            "user_satisfaction": 0.0,
        }

class LegacyMemoryMigrator:
    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir = memory_dir

    def load_grouped_records(self) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        if not self.memory_dir.exists():
            return grouped

        for group_dir in self.memory_dir.iterdir():
            if not group_dir.is_dir():
                continue
            try:
                group_id = int(group_dir.name)
            except ValueError:
                continue

            history_file = group_dir / "_history.jsonl"
            if not history_file.exists():
                continue

            records: list[dict[str, Any]] = []
            try:
                with history_file.open("r", encoding="utf-8") as f:
                    for idx, line in enumerate(f, start=1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except Exception:
                            continue
                        role = str(data.get("role", "")).strip().lower()
                        if role not in {"system", "user", "assistant"}:
                            continue
                        content = str(data.get("content", "")).strip()
                        if not content:
                            continue
                        records.append(
                            {
                                "message_id": f"legacy-{group_id}-{idx}"[:64],
                                "role": role,
                                "content": content,
                                "timestamp": _to_utc(data.get("ts")).isoformat(),
                                "user_id": None,
                                "message_type": "legacy",
                            }
                        )
            except Exception:
                log.exception("legacy migration read failed: %s", history_file)
                continue

            if records:
                grouped[group_id] = records
        return grouped


class MemoryV2Manager:
    """Multi-layer memory manager (working + attention + episodic + semantic + procedural)."""

    def __init__(self, *, config: MemoryV2Config, llm: LLMService, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.config = config
        self.llm = llm
        self.session_factory = session_factory

        self.scorer = ImportanceScorer(
            llm,
            llm_enabled=config.importance_llm_enabled,
            llm_min=config.importance_llm_min,
            llm_max=config.importance_llm_max,
        )
        self.attention = AttentionController(config)
        self.vector_store = VectorMemoryStore(
            session_factory=session_factory,
            host=config.qdrant_host,
            port=config.qdrant_port,
            collection_prefix=config.qdrant_collection_prefix,
        )
        self.retriever = HybridRetriever(self.vector_store, time_decay_factor=config.time_decay_factor)
        self.semantic_store = SemanticMemoryStore(session_factory)
        self.procedural_store = ProceduralMemoryStore(session_factory)
        self.knowledge_graph = KnowledgeGraph(
            enabled=config.kg_enabled,
            uri=config.kg_uri,
            user=config.kg_user,
            password=config.kg_password,
        )
        self.consolidator = MemoryConsolidation(
            llm=llm,
            vector_store=self.vector_store,
            semantic_store=self.semantic_store,
            procedural_store=self.procedural_store,
            knowledge_graph=self.knowledge_graph,
            min_importance=config.consolidation_min_importance,
        )
        self.pruner = MemoryPruner(vector_store=self.vector_store, retention_days=config.prune_days)
        self.metrics = MemoryMetrics(session_factory)

        self._index_tasks: set[asyncio.Task[None]] = set()
        self._index_sema = asyncio.Semaphore(max(1, config.max_concurrent_index_tasks))
        self._maintenance_lock = asyncio.Lock()
        self._last_maintenance_day = ""

    @staticmethod
    def _scoped_message_id(group_id: int, message_id: str | None) -> str:
        raw = str(message_id or uuid4().hex).strip()
        if not raw:
            raw = uuid4().hex
        prefix = f"{group_id}:"
        scoped = raw if raw.startswith(prefix) else f"{prefix}{raw}"
        return scoped[:64]

    def schedule_index_message(
        self,
        *,
        group_id: int,
        role: str,
        content: str,
        user_id: int | None = None,
        message_type: str = "text",
        message_id: str | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._ingest_single(
                group_id=group_id,
                role=role,
                content=content,
                user_id=user_id,
                message_type=message_type,
                message_id=message_id,
                timestamp=_utc_now().isoformat(),
                use_llm_importance=True,
            ),
            name=f"memory-index-{group_id}",
        )
        self._index_tasks.add(task)
        task.add_done_callback(self._on_index_done)

    def _on_index_done(self, task: asyncio.Task[None]) -> None:
        self._index_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.exception("memory index task failed: %s", exc)

    async def _ingest_single(
        self,
        *,
        group_id: int,
        role: str,
        content: str,
        user_id: int | None,
        message_type: str,
        message_id: str | None,
        timestamp: str,
        use_llm_importance: bool,
    ) -> None:
        text = (content or "").strip()
        if not text:
            return

        async with self._index_sema:
            importance = (
                await self.scorer.score(text, context={"group_id": group_id, "role": role})
                if use_llm_importance
                else self.scorer.rule_score(text)
            )
            vectors = await self.llm.embed([text])
            if not vectors:
                return
            mid = self._scoped_message_id(group_id, message_id)
            await self.vector_store.upsert_message(
                group_id=group_id,
                message_id=mid,
                embedding=vectors[0],
                metadata={
                    "role": role,
                    "content": text,
                    "timestamp": timestamp,
                    "importance_score": importance,
                    "user_id": user_id,
                    "message_type": message_type,
                },
            )

    async def build_context_messages(
        self,
        *,
        group_id: int,
        query: str,
        working_messages: list[dict[str, str]],
        max_working_items: int | None,
    ) -> list[dict[str, str]]:
        plan = self.attention.plan(query)
        if max_working_items is not None:
            plan.working_items = max(1, min(plan.working_items, max_working_items))

        messages: list[dict[str, str]] = [{"role": "system", "content": self.attention.to_system_text(plan)}]

        if plan.include_semantic:
            semantic = await self.semantic_store.summarize(group_id=group_id)
            if semantic:
                messages.append({"role": "system", "content": semantic})

        if plan.include_procedural:
            procedural = await self.procedural_store.summarize(group_id=group_id)
            if procedural:
                messages.append({"role": "system", "content": procedural})

        if plan.include_episodic and (query or "").strip():
            query_vecs = await self.llm.embed([query])
            if query_vecs:
                episodic = await self.retriever.retrieve(
                    group_id=group_id,
                    query_vector=query_vecs[0],
                    plan=plan,
                )
                summary = self._episodic_summary(episodic)
                if summary:
                    messages.append({"role": "system", "content": summary})

        working_tail = working_messages[-plan.working_items :]
        messages.extend({"role": m.get("role", "user"), "content": m.get("content", "")} for m in working_tail)
        return messages

    @staticmethod
    def _episodic_summary(items: list[dict[str, Any]], *, max_items: int = 8) -> str:
        if not items:
            return ""
        lines = ["[episodic-memory]"]
        for item in items[: max(1, max_items)]:
            metadata = item.get("metadata", {}) or {}
            ts = _to_utc(metadata.get("timestamp")).strftime("%Y-%m-%d %H:%M")
            role = str(metadata.get("role", "user"))
            content = str(metadata.get("content", "")).replace("\n", " ").strip()
            if len(content) > 180:
                content = content[:180] + "..."
            lines.append(f"- ({float(item.get('final_score', item.get('score', 0.0))):.3f}) [{ts}] [{role}] {content}")
        return "\n".join(lines)

    async def maybe_run_daily_maintenance(self) -> dict[str, int]:
        today = _utc_now().strftime("%Y-%m-%d")
        if self._last_maintenance_day == today:
            return {"groups": 0, "consolidated_messages": 0, "facts": 0, "preferences": 0, "pruned": 0}

        async with self._maintenance_lock:
            if self._last_maintenance_day == today:
                return {"groups": 0, "consolidated_messages": 0, "facts": 0, "preferences": 0, "pruned": 0}

            groups = await self.vector_store.list_group_ids()
            stats = {"groups": len(groups), "consolidated_messages": 0, "facts": 0, "preferences": 0, "pruned": 0}
            for group_id in groups:
                if self.config.consolidation_enabled:
                    c = await self.consolidator.consolidate_daily(group_id)
                    stats["consolidated_messages"] += c.get("processed", 0)
                    stats["facts"] += c.get("facts", 0)
                    stats["preferences"] += c.get("preferences", 0)
                if self.config.prune_enabled:
                    _, deleted = await self.pruner.prune_old_memories(group_id)
                    stats["pruned"] += deleted

            self._last_maintenance_day = today
            return stats

    async def migrate_legacy_if_needed(self) -> dict[str, int]:
        if not self.config.migrate_legacy_on_start:
            return {"groups": 0, "migrated_messages": 0}

        marker = Path(self.config.legacy_migration_marker)
        if marker.exists():
            return {"groups": 0, "migrated_messages": 0}

        migrator = LegacyMemoryMigrator(Path(self.config.legacy_memory_dir))
        grouped = migrator.load_grouped_records()
        migrated = 0
        for group_id, records in grouped.items():
            for idx in range(0, len(records), 24):
                chunk = records[idx : idx + 24]
                texts = [item["content"] for item in chunk]
                embeddings = await self.llm.embed(texts)
                if len(embeddings) != len(chunk):
                    continue

                items: list[dict[str, Any]] = []
                for rec, emb in zip(chunk, embeddings):
                    items.append(
                        {
                            "message_id": rec["message_id"],
                            "embedding": emb,
                            "metadata": {
                                "role": rec["role"],
                                "content": rec["content"],
                                "timestamp": rec["timestamp"],
                                "importance_score": self.scorer.rule_score(rec["content"]),
                                "user_id": rec.get("user_id"),
                                "message_type": rec.get("message_type", "legacy"),
                            },
                        }
                    )
                await self.vector_store.upsert_messages(group_id=group_id, items=items)
                migrated += len(items)

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"migrated_at": _utc_now().isoformat(), "groups": len(grouped), "migrated_messages": migrated}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"groups": len(grouped), "migrated_messages": migrated}

    async def load_working_memory(self, *, max_items: int) -> dict[int, list[dict[str, str]]]:
        grouped: dict[int, list[dict[str, str]]] = {}
        for group_id in await self.vector_store.list_group_ids():
            grouped[group_id] = await self.vector_store.fetch_recent_messages(group_id=group_id, limit=max_items)
        return grouped

    async def flush_pending_index_tasks(self, timeout_sec: float = 5.0) -> None:
        if not self._index_tasks:
            return
        pending = list(self._index_tasks)
        done, still = await asyncio.wait(pending, timeout=max(0.1, timeout_sec))
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                log.exception("memory index task failed during flush: %s", exc)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)
