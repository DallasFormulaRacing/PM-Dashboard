"""SQLAlchemy models for the RAG persistence schema."""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint naming."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class SourceRegistry(Base):
    """Explicit allowlist of source objects approved for ingestion."""

    __tablename__ = "source_registry"
    __table_args__ = (
        CheckConstraint("source IN ('notion', 'box')", name="source_valid"),
        CheckConstraint(
            "source_type IN ('page', 'data_source', 'file', 'folder')",
            name="source_type_valid",
        ),
        CheckConstraint("visibility IN ('shared', 'restricted')", name="visibility_valid"),
        UniqueConstraint("source", "external_id", name="uq_source_registry_source_external_id"),
        Index("ix_source_registry_enabled_visibility", "is_enabled", "visibility"),
        {"schema": "rag"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="shared")
    inclusion_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="approved_via")


class Document(Base):
    """Current indexed lifecycle record for one source page or file."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("source IN ('notion', 'box')", name="source_valid"),
        CheckConstraint("status IN ('active', 'stale', 'deleted')", name="status_valid"),
        CheckConstraint("visibility IN ('shared', 'restricted')", name="visibility_valid"),
        UniqueConstraint("source", "external_id", name="uq_documents_source_external_id"),
        Index("ix_documents_status_visibility", "status", "visibility"),
        Index("ix_documents_source_status", "source", "status"),
        {"schema": "rag"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    approved_via_source_id: Mapped[int] = mapped_column(
        ForeignKey("rag.source_registry.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    source_version: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="shared")
    document_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    approved_via: Mapped[SourceRegistry] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Chunk(Base):
    """Searchable text segment and its embedding."""

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint("page_number IS NULL OR page_number > 0", name="page_number_positive"),
        CheckConstraint("token_count >= 0", name="token_count_nonnegative"),
        UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_text_search", "text_search", postgresql_using="gin"),
        {"schema": "rag"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("rag.documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    section_reference: Mapped[str | None] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_version: Mapped[str] = mapped_column(Text, nullable=False)
    text_search: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, COALESCE(content, ''::text))", persisted=True),
        nullable=False,
    )

    document: Mapped[Document] = relationship(back_populates="chunks")


class SyncRun(Base):
    """Audit record for one source synchronization attempt."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint("source IN ('notion', 'box')", name="source_valid"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("documents_seen >= 0", name="documents_seen_nonnegative"),
        CheckConstraint("documents_updated >= 0", name="documents_updated_nonnegative"),
        CheckConstraint("documents_skipped >= 0", name="documents_skipped_nonnegative"),
        CheckConstraint("documents_failed >= 0", name="documents_failed_nonnegative"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
        Index("ix_sync_runs_source_started_at", "source", "started_at"),
        {"schema": "rag"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    documents_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    documents_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    documents_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    documents_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_summary: Mapped[str | None] = mapped_column(Text)
