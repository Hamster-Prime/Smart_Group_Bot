from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bot.utils.timezone import now_shanghai_naive


class Base(DeclarativeBase):
    pass


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # TG chat_id
    title: Mapped[str] = mapped_column(String(255), default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    moderation_rules: Mapped[list[ModerationRule]] = relationship(back_populates="group")
    violations: Mapped[list[Violation]] = relationship(back_populates="group")
    context_summary: Mapped[GroupContextSummary | None] = relationship(back_populates="group", uselist=False)
    permanent_memories: Mapped[list[GroupPermanentMemory]] = relationship(back_populates="group")


class GroupContextSummary(Base):
    __tablename__ = "group_context_summaries"

    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    group: Mapped[Group] = relationship(back_populates="context_summary")


class GroupPermanentMemory(Base):
    __tablename__ = "group_permanent_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    group: Mapped[Group] = relationship(back_populates="permanent_memories")


class ModerationRule(Base):
    __tablename__ = "moderation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    rule_type: Mapped[str] = mapped_column(String(32))  # keyword, regex, llm
    pattern: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(32), default="warn")  # warn, delete, ban
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    group: Mapped[Group] = relationship(back_populates="moderation_rules")


class Violation(Base):
    __tablename__ = "violations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    rule_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("moderation_rules.id"), nullable=True)
    message_text: Mapped[str] = mapped_column(Text, default="")
    action_taken: Mapped[str] = mapped_column(String(32), default="warn")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    group: Mapped[Group] = relationship(back_populates="violations")


class UserWarning(Base):
    __tablename__ = "user_warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_warn_group_user", "group_id", "user_id", unique=True),)


class ModerationExemption(Base):
    __tablename__ = "moderation_exemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_exempt_group_user", "group_id", "user_id", unique=True),)


class ReplyMute(Base):
    __tablename__ = "reply_mutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_reply_mute_group_user", "group_id", "user_id", unique=True),)


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(32), default="admin")

    __table_args__ = (Index("ix_admin_group_user", "group_id", "user_id", unique=True),)


class AuthorizedGroup(Base):
    __tablename__ = "authorized_groups"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    authorized_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RuntimeConfigRecord(Base):
    """Validated global runtime configuration stored as one JSON document."""

    __tablename__ = "runtime_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RuntimeConfigSecret(Base):
    """Encrypted secret values referenced by the runtime configuration."""

    __tablename__ = "runtime_config_secrets"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GlobalBan(Base):
    """Ban registry; enforced on join and on every message."""

    __tablename__ = "global_bans"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    # manual / join_screening / profile_screening / moderation_challenge_timeout
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class JoinScreeningExemption(Base):
    """Users unbanned via /unban: skip name/bio screening afterwards."""

    __tablename__ = "join_screening_exemptions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class JoinVerification(Base):
    """Pending Turnstile verification; the user stays fully restricted until
    they pass the Cloudflare challenge inside the Telegram Mini App.

    kind="join": issued on member join; missing the deadline kicks (the user
    may rejoin and retry). kind="moderation": issued when a message is judged
    violating with low confidence; missing the deadline bans permanently.

    No secret token: the Mini App submits Telegram-signed initData, so the
    verified user identity comes from the signature, keyed by user_id here.
    """

    __tablename__ = "join_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(
        String(32), default="join", server_default="join"
    )
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    display_name: Mapped[str] = mapped_column(String(255), default="")
    prompt_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_join_verification_group_user", "group_id", "user_id", unique=True),)


class UserProfileScreen(Base):
    """Last screened profile signature per user, for on-message re-screening."""

    __tablename__ = "user_profile_screens"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String(64), default="")
    checked_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SpeechStyleSample(Base):
    """Raw utterances collected from the persona-mimic target user."""

    __tablename__ = "speech_style_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_style_sample_group_user_id", "group_id", "user_id", "id"),
    )


class MessageVector(Base):
    """Active per-group dialogue history row."""

    __tablename__ = "message_vectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    role: Mapped[str] = mapped_column(String(16), default="user")
    importance_score: Mapped[float] = mapped_column(default=0.0)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    vector_id: Mapped[str] = mapped_column(String(64), default="")
    embedding: Mapped[bytes | None] = mapped_column(nullable=True)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str] = mapped_column(String(255), default="")
    message_type: Mapped[str] = mapped_column(String(64), default="text")
    content: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_message_vectors_group_created", "group_id", "created_at"),
    )


class StickerLibraryRecord(Base):
    __tablename__ = "sticker_library"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    file_id: Mapped[str] = mapped_column(String(255))
    emoji: Mapped[str] = mapped_column(String(32), default="")
    set_name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    seen_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(64), default="group_message")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_sticker_group_file", "group_id", "file_id", unique=True),)
