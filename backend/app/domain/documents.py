"""Provider-independent normalized document contracts."""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositivePageNumber = Annotated[int, Field(strict=True, ge=1)]
DocumentSource = Literal["notion", "box"]


class DocumentSection(BaseModel):
    """A coherent section of extracted document text with citation context."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    text: str
    heading_path: list[NonEmptyString] = Field(default_factory=list)
    page_number: PositivePageNumber | None = None

    @field_validator("text")
    @classmethod
    def text_must_contain_content(cls, value: str) -> str:
        """Reject extraction results that contain only whitespace."""

        if not value.strip():
            raise ValueError("section text must contain non-whitespace content")
        return value


class NormalizedDocument(BaseModel):
    """Common document shape emitted by every approved source connector."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source: DocumentSource
    external_id: NonEmptyString
    title: NonEmptyString
    source_url: NonEmptyString | None = None
    source_version: NonEmptyString | None = None
    modified_at: datetime | None = None
    sections: list[DocumentSection] = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("source_url")
    @classmethod
    def source_url_must_be_http(cls, value: str | None) -> str | None:
        """Accept only canonical HTTP(S) links suitable for citations."""

        if value is None:
            return None

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value

    @field_validator("modified_at")
    @classmethod
    def modified_at_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        """Prevent ambiguous source timestamps from entering the pipeline."""

        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("modified_at must include a timezone")
        return value
