"""In-memory chunks and citation locations, before embedding or persistence."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.domain.documents import DocumentSource, NonEmptyString, PositivePageNumber


class ChunkReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_index: Annotated[int, Field(strict=True, ge=0)]
    heading_path: list[NonEmptyString] = Field(default_factory=list)
    page_number: PositivePageNumber | None = None
    section_reference: NonEmptyString | None = None


class DocumentChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: DocumentSource
    external_id: NonEmptyString
    title: NonEmptyString
    source_url: str | None
    chunk_index: Annotated[int, Field(strict=True, ge=0)]
    content: NonEmptyString
    token_count: Annotated[int, Field(strict=True, ge=1)]
    content_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    references: list[ChunkReference] = Field(min_length=1)
    tokenizer: NonEmptyString
    chunking_version: NonEmptyString
