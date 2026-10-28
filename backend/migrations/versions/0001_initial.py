"""initial schema: users, tasks, announcements, discord channels, outbox

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("discord_user_id", sa.String(32), unique=True),
        sa.Column("discord_dm_channel_id", sa.String(32)),
        _ts("created_at", nullable=False),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("assignee_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("priority", _enum("task_priority", "low", "medium", "high"), nullable=False),
        sa.Column("status", _enum("task_status", "todo", "in_progress", "done"), nullable=False),
        _ts("due_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
    )

    op.create_table(
        "announcements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("channel_purpose", sa.String(64), nullable=False),
        _ts("scheduled_for"),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        _ts("created_at", nullable=False),
    )

    op.create_table(
        "discord_channels",
        sa.Column("purpose", sa.String(64), primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(32), nullable=False, unique=True),
        sa.Column("webhook_id", sa.String(32)),
        sa.Column("webhook_token", sa.String(128)),
    )

    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", _enum("message_kind", "task_assigned", "task_updated", "task_unassigned",
                                "announcement"), nullable=False),
        sa.Column("ref_type", _enum("message_ref_type", "task", "announcement"), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        sa.Column("sender_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("target_type", _enum("message_target_type", "dm", "channel"), nullable=False),
        sa.Column("recipient_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("channel_purpose", sa.String(64)),
        sa.Column("payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("status", _enum("message_status", "pending", "sending", "sent", "failed", "dead", "skipped"),
                  nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("nonce", sa.String(25), nullable=False),
        sa.Column("discord_message_id", sa.String(32)),
        sa.Column("discord_channel_id", sa.String(32)),
        sa.Column("delivered_via", _enum("message_delivered_via", "dm", "bot_channel", "webhook")),
        sa.Column("used_fallback", sa.Boolean(), nullable=False),
        _ts("available_at", nullable=False),
        _ts("enqueued_at"),
        _ts("locked_at"),
        _ts("created_at", nullable=False),
        _ts("sent_at"),
    )
    op.create_index("ix_outbound_messages_status_available_at", "outbound_messages", ["status", "available_at"])
    op.create_index("ix_outbound_messages_ref", "outbound_messages", ["ref_type", "ref_id"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.String(64), primary_key=True),
        _ts("beat_at", nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_outbound_messages_ref", table_name="outbound_messages")
    op.drop_index("ix_outbound_messages_status_available_at", table_name="outbound_messages")
    op.drop_table("outbound_messages")
    op.drop_table("discord_channels")
    op.drop_table("announcements")
    op.drop_table("tasks")
    op.drop_table("users")
