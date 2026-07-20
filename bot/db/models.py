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
    text,
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

    __table_args__ = (
        Index("ix_moderation_rules_group_enabled_id", "group_id", "enabled", "id"),
    )


class KeywordReply(Base):
    """Per-group keyword auto replies, managed from the Mini App.

    match_type: "contains" (substring), "exact" (whole message), or "regex".
    Replies support the shared safe Markdown renderer and optional inline
    buttons; pin_message optionally pins the sent reply and auto_delete opts
    it into the global "keyword" retention category.
    """

    __tablename__ = "keyword_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    keyword: Mapped[str] = mapped_column(String(255), default="")
    match_type: Mapped[str] = mapped_column(String(16), default="contains")
    reply_text: Mapped[str] = mapped_column(Text, default="")
    # Shared template-button schema: [{text, action, value, row}, ...].
    # JSON keeps the feature extensible without a join table for a small,
    # ordered collection capped by the settings API.
    buttons: Mapped[list] = mapped_column(JSON, default=list)
    pin_message: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_delete: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )


class ScheduledMessage(Base):
    """Per-group timed announcements, managed from the Mini App.

    schedule_type: "daily" fires once per day at HH:MM (Asia/Shanghai,
    schedule_time); "interval" fires every interval_minutes. next_run_at /
    last_run_at bookkeeping follows the patrol convention (compare against
    the computed due instant so restarts never double-fire).
    """

    __tablename__ = "scheduled_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    buttons: Mapped[list] = mapped_column(JSON, default=list)
    schedule_type: Mapped[str] = mapped_column(String(16), default="daily")
    schedule_time: Mapped[str] = mapped_column(String(5), default="09:00")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    pin_message: Mapped[bool] = mapped_column(Boolean, default=False)
    unpin_previous: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    # Python-side default keeps created_at in Asia/Shanghai naive time; the
    # due computation compares it against now_shanghai_naive().
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )


class ScheduledMessageOccurrence(Base):
    """Durable delivery attempt for one computed schedule occurrence.

    The unique occurrence key and live lease prevent concurrent workers from
    normally publishing the same due event. Delivery is intentionally
    at-least-once across process crashes or lease expiry: an old worker that
    loses its lease cannot complete the row, while a later worker may replay it.
    Failed attempts retain the row with exponential backoff until completion can
    advance ``ScheduledMessage.last_run_at`` atomically.
    """

    __tablename__ = "scheduled_message_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheduled_message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scheduled_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_scheduled_message_occurrence_unique",
            "scheduled_message_id",
            "occurrence_at",
            unique=True,
        ),
        Index(
            "ix_scheduled_message_occurrence_recovery",
            "next_attempt_at",
            "lease_until",
            "occurrence_at",
        ),
    )


class VoteBanSession(Base):
    """Democratic vote-ban: one active poll per (group, target).

    Any member replies to a message with /voteban to open a poll against the
    replied-to user; approvals at or above the per-group threshold ban the
    target in that group. The prompt message carries live buttons, so no
    auto-delete timer is scheduled while status is "active"; the finalized
    (edited) outcome notice joins the "vote" retention category. Expiry timers
    are in-memory (raid-guard convention): a restart drops them, but the next
    button press lazily finalizes an overdue session. Group admins may resolve
    a poll early through dedicated buttons; the resolution columns record who
    cancelled or banned so recovery and audit stay truthful after a crash.
    """

    __tablename__ = "vote_ban_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger)
    target_display: Mapped[str] = mapped_column(String(255), default="")
    target_username: Mapped[str] = mapped_column(String(255), default="")
    starter_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    starter_display: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="command")
    target_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    threshold: Mapped[int] = mapped_column(Integer, default=5)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # active / enforcing / passed / failed / expired / cancelled
    status: Mapped[str] = mapped_column(String(16), default="active")
    # "" = resolved by vote threshold / timeout; "admin_ban" / "admin_cancel"
    # = a group admin resolved the poll early via its admin buttons;
    # "manual_unban" = a newer explicit unban cancelled open enforcement.
    resolution: Mapped[str] = mapped_column(
        String(16), default="", server_default=""
    )
    resolver_user_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    resolver_display: Mapped[str] = mapped_column(
        String(255), default="", server_default=""
    )
    # Lease timestamp for the Telegram ban side effect.  If a worker dies
    # while status is ``enforcing``, another worker may safely retry the
    # idempotent ban after the lease becomes stale.
    enforcing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_vote_ban_group_status", "group_id", "status"),
        Index("ix_vote_ban_status_deadline", "status", "deadline_at"),
        # One open/enforcing poll per (group, target), enforced at the DB level so
        # two concurrent /voteban commands cannot both open a session.
        Index(
            "ix_vote_ban_open_target",
            "group_id",
            "target_user_id",
            unique=True,
            sqlite_where=text("status IN ('active', 'enforcing')"),
            postgresql_where=text("status IN ('active', 'enforcing')"),
        ),
        {"sqlite_autoincrement": True},
    )


class VoteBanVote(Base):
    __tablename__ = "vote_ban_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("vote_ban_sessions.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_vote_ban_vote_session_user", "session_id", "user_id", unique=True),
    )


class VoteBanQuotaBucket(Base):
    """Persistent fixed-window quota for opening democratic vote-ban polls."""

    __tablename__ = "vote_ban_quota_buckets"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
    )
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        onupdate=now_shanghai_naive,
    )


class BanAuditEvent(Base):
    """Append-only facts about ban/unban decisions and Telegram outcomes.

    ``group_id == 0`` denotes a global policy entry.  Current policy state
    remains in ``GlobalBan`` / ``UserWarning``; this table preserves who,
    why, source, and actual outcome for the bot's trusted knowledge context.
    """

    __tablename__ = "ban_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_display: Mapped[str] = mapped_column(String(255), default="")
    target_username: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(16), default="ban")
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    actor_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    actor_display: Mapped[str] = mapped_column(String(255), default="")
    outcome: Mapped[str] = mapped_column(String(32), default="succeeded")
    reference_type: Mapped[str] = mapped_column(String(32), default="")
    reference_id: Mapped[int] = mapped_column(BigInteger, default=0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_ban_audit_group_created", "group_id", "created_at"),
        Index("ix_ban_audit_group_id_desc", "group_id", "id"),
        Index(
            "ix_ban_audit_group_target_created",
            "group_id",
            "target_user_id",
            "created_at",
        ),
        Index(
            "ix_ban_audit_reference",
            "reference_type",
            "reference_id",
        ),
    )


class Violation(Base):
    __tablename__ = "violations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    rule_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("moderation_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_text: Mapped[str] = mapped_column(Text, default="")
    action_taken: Mapped[str] = mapped_column(String(32), default="warn")
    # Telegram message ids are unique within a chat.  Durable webhook retries
    # can dispatch the same update more than once, so the pair below is the
    # stable idempotency key for every moderation side effect derived from one
    # source message.  NULL keeps legacy/manual rows unrestricted.
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Count captured by the atomic warning transaction for this event.  It lets
    # retries render the original warning state without incrementing again.
    warning_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULL means a threshold/sender-chat ban has not completed its Telegram +
    # audit persistence stage.  False is an attempted but unconfirmed ban.
    ban_enforced: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Notification delivery is a separate retryable stage.  The unavoidable
    # send-success/process-crash-before-commit window is intentionally tiny.
    notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    group: Mapped[Group] = relationship(back_populates="violations")

    __table_args__ = (
        Index(
            "ix_violations_group_user_ban",
            "group_id",
            "user_id",
            "ban_enforced",
        ),
        Index(
            "ix_violations_group_source_message",
            "group_id",
            "source_message_id",
            unique=True,
        ),
    )


class UserWarning(Base):
    __tablename__ = "user_warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_warn_group_user", "group_id", "user_id", unique=True),)


class BotScreening(Base):
    """Per-group screening progress for bot senders.

    A bot's messages are moderated until it accumulates the configured number
    of clean messages, then it is whitelisted and skipped permanently.
    Violations reset the counter.
    """

    __tablename__ = "bot_screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    whitelisted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_bot_screening_group_bot", "group_id", "bot_id", unique=True),)


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
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("authorized_groups.group_id", ondelete="CASCADE"),
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(32), default="admin")

    __table_args__ = (Index("ix_admin_group_user", "group_id", "user_id", unique=True),)


class AuthorizedGroup(Base):
    __tablename__ = "authorized_groups"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    authorized_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Keep the durable authorization intent when Telegram reports that the bot
    # left or was kicked.  Inactive rows are excluded from normal authorization
    # and global fan-out queries, but can be reactivated by a later
    # ``my_chat_member`` join without resurrecting a manually deleted grant.
    bot_present: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
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
    """Pending provider verification; the user stays restricted until passing.

    kind="join": issued on member join; missing the deadline kicks (the user
    may rejoin and retry). kind="moderation": issued when a message is judged
    violating with low confidence; missing the deadline bans permanently.
    kind="patrol": issued when the profile patrol flags a member; missing the
    deadline kicks without banning (the user may rejoin). kind="raid": issued
    by the raid guard's retroactive sweep after a join-flood lockdown; same
    kick-without-ban timeout semantics as patrol.

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
    # Snapshot the provider selected when this challenge was issued. This
    # keeps an in-flight page valid when the global or group default changes.
    provider: Mapped[str] = mapped_column(
        String(32), default="turnstile", server_default="turnstile"
    )
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default="pending",
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    prompt_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_join_verification_group_user", "group_id", "user_id", unique=True),
        Index("ix_join_verifications_status_lease", "status", "lease_until"),
        Index("ix_join_verifications_status_deadline", "status", "deadline_at"),
        {"sqlite_autoincrement": True},
    )


class GroupMember(Base):
    """Known members per group, maintained from joins/leaves and messages.

    The Bot API cannot enumerate chat members, so the patrol scanner walks
    this roster instead. Rows are upserted on join and on every message
    (cheap: only when the visible profile changed) and marked left on leave;
    a stale row is harmless — the scanner skips users who are no longer in
    the group when Telegram rejects the restriction call.
    """

    __tablename__ = "group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(255), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    left: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last profile signature (incl. rules fingerprint) that passed the patrol
    # in THIS group; per-group so multi-group patrols cannot thrash the cache.
    patrol_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_group_member_group_user", "group_id", "user_id", unique=True),
    )


class UserProfileScreen(Base):
    """Last screened profile signature per group/user pair.

    The signature includes the group's moderation-rule fingerprint, so a
    process-wide user-only key lets two groups continually overwrite each
    other's cached verdict.  Keep the cache scoped to the policy owner.
    """

    __tablename__ = "user_profile_screens"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String(64), default="")
    checked_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WebhookInboxUpdate(Base):
    """Durable acceptance/dedup record for Telegram webhook updates."""

    __tablename__ = "webhook_inbox_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    auth_candidate: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_webhook_inbox_recovery",
            "priority",
            "auth_candidate",
            "completed_at",
            "dead_lettered_at",
            "next_attempt_at",
            "lease_until",
        ),
        Index(
            "ix_webhook_inbox_completed_retention",
            "completed_at",
        ),
        Index(
            "ix_webhook_inbox_dead_letter_retention",
            "dead_lettered_at",
        ),
    )


class TelegramDeleteJob(Base):
    """Durable delayed deletion for an outgoing Telegram message.

    A unique message key makes scheduling idempotent.  ``lease_until`` keeps a
    claimed row recoverable after a process crash without holding a database
    transaction across the Telegram API call.
    """

    __tablename__ = "telegram_delete_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_telegram_delete_jobs_message",
            "chat_id",
            "message_id",
            unique=True,
        ),
        Index(
            "ix_telegram_delete_jobs_recovery",
            "due_at",
            "lease_until",
        ),
    )


class PatrolRun(Base):
    """Per-group patrol bookkeeping: last completed run and its summary."""

    __tablename__ = "patrol_runs"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scanned: Mapped[int] = mapped_column(Integer, default=0)
    last_violations: Mapped[int] = mapped_column(Integer, default=0)
    running: Mapped[bool] = mapped_column(Boolean, default=False)


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
        Index("ix_message_vectors_group_row", "group_id", "id"),
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
