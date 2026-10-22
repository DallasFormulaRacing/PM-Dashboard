"""Create the initial RAG persistence schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create pgvector support and all initial RAG tables."""

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA rag")

    op.create_table(
        "source_registry",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("visibility", sa.Text(), server_default="shared", nullable=False),
        sa.Column(
            "inclusion_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('notion', 'box')", name="ck_source_registry_source_valid"),
        sa.CheckConstraint(
            "source_type IN ('page', 'data_source', 'file', 'folder')",
            name="ck_source_registry_source_type_valid",
        ),
        sa.CheckConstraint(
            "visibility IN ('shared', 'restricted')",
            name="ck_source_registry_visibility_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_registry"),
        sa.UniqueConstraint(
            "source",
            "external_id",
            name="uq_source_registry_source_external_id",
        ),
        schema="rag",
    )
    op.create_index(
        "ix_source_registry_enabled_visibility",
        "source_registry",
        ["is_enabled", "visibility"],
        schema="rag",
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("approved_via_source_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("source_version", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("visibility", sa.Text(), server_default="shared", nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('notion', 'box')", name="ck_documents_source_valid"),
        sa.CheckConstraint(
            "status IN ('active', 'stale', 'deleted')",
            name="ck_documents_status_valid",
        ),
        sa.CheckConstraint(
            "visibility IN ('shared', 'restricted')",
            name="ck_documents_visibility_valid",
        ),
        sa.ForeignKeyConstraint(
            ["approved_via_source_id"],
            ["rag.source_registry.id"],
            name="fk_documents_approved_via_source_id_source_registry",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "source",
            "external_id",
            name="uq_documents_source_external_id",
        ),
        schema="rag",
    )
    op.create_index(
        "ix_documents_status_visibility",
        "documents",
        ["status", "visibility"],
        schema="rag",
    )
    op.create_index(
        "ix_documents_source_status",
        "documents",
        ["source", "status"],
        schema="rag",
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(dim=1536), nullable=False),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section_reference", sa.Text(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_version", sa.Text(), nullable=False),
        sa.Column(
            "text_search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english'::regconfig, COALESCE(content, ''::text))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_chunks_chunk_index_nonnegative"),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number > 0",
            name="ck_chunks_page_number_positive",
        ),
        sa.CheckConstraint("token_count >= 0", name="ck_chunks_token_count_nonnegative"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["rag.documents.id"],
            name="fk_chunks_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunks"),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_chunks_document_chunk_index",
        ),
        schema="rag",
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"], schema="rag")
    op.create_index(
        "ix_chunks_text_search",
        "chunks",
        ["text_search"],
        schema="rag",
        postgresql_using="gin",
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("documents_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("documents_updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("documents_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("documents_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.CheckConstraint("source IN ('notion', 'box')", name="ck_sync_runs_source_valid"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="ck_sync_runs_status_valid",
        ),
        sa.CheckConstraint(
            "documents_seen >= 0",
            name="ck_sync_runs_documents_seen_nonnegative",
        ),
        sa.CheckConstraint(
            "documents_updated >= 0",
            name="ck_sync_runs_documents_updated_nonnegative",
        ),
        sa.CheckConstraint(
            "documents_skipped >= 0",
            name="ck_sync_runs_documents_skipped_nonnegative",
        ),
        sa.CheckConstraint(
            "documents_failed >= 0",
            name="ck_sync_runs_documents_failed_nonnegative",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_sync_runs_finished_after_started",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
        schema="rag",
    )
    op.create_index(
        "ix_sync_runs_source_started_at",
        "sync_runs",
        ["source", "started_at"],
        schema="rag",
    )


def downgrade() -> None:
    """Drop the RAG schema while leaving the shared vector extension installed."""

    op.drop_index("ix_sync_runs_source_started_at", table_name="sync_runs", schema="rag")
    op.drop_table("sync_runs", schema="rag")
    op.drop_index("ix_chunks_text_search", table_name="chunks", schema="rag")
    op.drop_index("ix_chunks_document_id", table_name="chunks", schema="rag")
    op.drop_table("chunks", schema="rag")
    op.drop_index("ix_documents_source_status", table_name="documents", schema="rag")
    op.drop_index("ix_documents_status_visibility", table_name="documents", schema="rag")
    op.drop_table("documents", schema="rag")
    op.drop_index(
        "ix_source_registry_enabled_visibility",
        table_name="source_registry",
        schema="rag",
    )
    op.drop_table("source_registry", schema="rag")
    op.execute("DROP SCHEMA rag")
