import enum
import secrets
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow

JsonType = JSON().with_variant(JSONB(), "postgresql")


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    # Stored as varchar + CHECK constraint, so adding a value is a plain migration.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


class Priority(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(enum.StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class MessageKind(enum.StrEnum):
    TASK_ASSIGNED = "task_assigned"
    TASK_UPDATED = "task_updated"
    TASK_UNASSIGNED = "task_unassigned"
    ANNOUNCEMENT = "announcement"


class TargetType(enum.StrEnum):
    DM = "dm"
    CHANNEL = "channel"


class MessageStatus(enum.StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"
    SKIPPED = "skipped"


class DeliveredVia(enum.StrEnum):
    DM = "dm"  # bot DM
    BOT_CHANNEL = "bot_channel"  # bot post in a channel
    WEBHOOK = "webhook"  # channel post under the sender's name and avatar


class RefType(enum.StrEnum):
    TASK = "task"
    ANNOUNCEMENT = "announcement"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    discord_user_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    discord_dm_channel_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    # Reserved for LLM summaries of long descriptions (not generated yet).
    summary: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    priority: Mapped[Priority] = mapped_column(_enum(Priority, "task_priority"), default=Priority.MEDIUM)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus, "task_status"), default=TaskStatus.TODO)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    creator: Mapped[User] = relationship(foreign_keys=[created_by])


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    # Reserved for LLM summaries of long bodies (not generated yet).
    summary: Mapped[str | None] = mapped_column(Text)
    channel_purpose: Mapped[str] = mapped_column(String(64), default="announcements")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    creator: Mapped[User] = relationship()


class DiscordChannel(Base):
    __tablename__ = "discord_channels"

    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32))
    channel_id: Mapped[str] = mapped_column(String(32), unique=True)
    # Created on first use by the worker. The token is a credential: anyone holding it can post here.
    webhook_id: Mapped[str | None] = mapped_column(String(32))
    webhook_token: Mapped[str | None] = mapped_column(String(128))


def new_nonce() -> str:
    # Discord caps nonces at 25 characters.
    return secrets.token_hex(12)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        Index("ix_outbound_messages_status_available_at", "status", "available_at"),
        Index("ix_outbound_messages_ref", "ref_type", "ref_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[MessageKind] = mapped_column(_enum(MessageKind, "message_kind"))
    ref_type: Mapped[RefType] = mapped_column(_enum(RefType, "message_ref_type"))
    ref_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The dashboard user who caused the message; shown as its author in Discord.
    sender_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    target_type: Mapped[TargetType] = mapped_column(_enum(TargetType, "message_target_type"))
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    channel_purpose: Mapped[str | None] = mapped_column(String(64))
    # {"embed": {...}, "sender": {"name": ..., "avatar_url": ...}}, built when the row is written.
    payload: Mapped[dict] = mapped_column(JsonType)

    status: Mapped[MessageStatus] = mapped_column(
        _enum(MessageStatus, "message_status"), default=MessageStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    nonce: Mapped[str] = mapped_column(String(25), default=new_nonce)

    discord_message_id: Mapped[str | None] = mapped_column(String(32))
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    delivered_via: Mapped[DeliveredVia | None] = mapped_column(_enum(DeliveredVia, "message_delivered_via"))
    # True when the recipient's DMs were blocked and the message went to the fallback channel instead.
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False)

    # Earliest time a worker may send it: now, a scheduled time, or the next retry.
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sender: Mapped[User | None] = relationship(foreign_keys=[sender_id])
    recipient: Mapped[User | None] = relationship(foreign_keys=[recipient_user_id])

    @property
    def marker(self) -> str:
        """Short tag put in the embed footer, used to find a webhook post after an ambiguous failure."""
        return f"#{self.id.hex[:8]}"


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
