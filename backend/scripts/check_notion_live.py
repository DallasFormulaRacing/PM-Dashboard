"""Read-only page/data-source smoke tests with content-free JSON output.

Run from backend/: python scripts/check_notion_live.py --page-id UUID [--preflight]
Credentials and approvals come only from Settings; this command never writes to
Notion, PostgreSQL, an embedding provider, or a generation provider.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
import json
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.connectors.notion import (
    NotionAPIError,
    NotionConnector,
    NotionConnectorError,
    NotionIncompleteContentError,
    NotionIncompleteQueryError,
    NotionRecordExcludedError,
    NotionResponseError,
    NotionTransportError,
)
from app.connectors.notion_properties import (
    PROPERTY_ALIASES,
    DocumentKind,
    NotionSourcePolicy,
    NotionPropertyError,
    find_property,
    lifecycle_reason,
    lifecycle_query_filter,
    normalize_properties,
    property_summary,
)
from app.domain.documents import NormalizedDocument
from app.ingestion.chunking import ChunkingError, DocumentChunker


class SmokeValidationError(ValueError):
    """A failed invariant, expressed as a fixed code without document content."""


def preflight(settings: Settings, page_id: str, *, data_source: bool = False) -> dict:
    page_id = str(UUID(page_id))
    missing = []
    token = settings.notion_api_token.get_secret_value() if settings.notion_api_token else ""
    if not token.strip() or token.lower().startswith(("replace", "placeholder", "synthetic")):
        missing.append("NOTION_API_TOKEN")
    values = (
        settings.notion_allowed_data_source_ids if data_source else settings.notion_allowed_page_ids
    )
    allowed = {str(UUID(value)) for value in values}
    if page_id not in allowed:
        missing.append(
            "source_in_NOTION_ALLOWED_DATA_SOURCE_IDS"
            if data_source
            else "pilot_in_NOTION_ALLOWED_PAGE_IDS"
        )
    return {
        "status": "blocked_configuration" if missing else "ready",
        "data_source_id" if data_source else "page_id": page_id,
        "api_version": settings.notion_api_version,
        "missing_requirements": missing,
    }


def failure_report(error: Exception) -> dict:
    """Never return provider messages or validation inputs containing content."""

    report = {"status": "failed"}
    if isinstance(error, NotionRecordExcludedError):
        return {"status": "excluded", "reason": error.reason}
    if isinstance(error, NotionAPIError):
        report.update(reason="notion_api_error", http_status=error.status_code)
    elif isinstance(error, NotionIncompleteContentError):
        report.update(
            reason="incomplete_markdown", unresolved_block_count=len(error.unresolved_block_ids)
        )
    elif isinstance(error, NotionIncompleteQueryError):
        report["reason"] = "incomplete_data_source_query"
    elif isinstance(error, NotionPropertyError):
        report["reason"] = "invalid_property_mapping"
    elif isinstance(error, NotionResponseError):
        report["reason"] = (
            "attachment_extraction_required"
            if str(error) == "Attachment/media content requires a separate approved extraction step"
            else "invalid_or_unsupported_notion_content"
        )
    elif isinstance(error, NotionTransportError):
        report["reason"] = "notion_transport_error"
    elif isinstance(error, NotionConnectorError):
        report["reason"] = "notion_connector_error"
    elif isinstance(error, ChunkingError):
        report["reason"] = "chunking_requires_review"
    elif isinstance(error, SmokeValidationError):
        report["reason"] = str(error)
    else:
        report["reason"] = "unexpected_error"
    return report


def probe_metadata(page: dict, policy: NotionSourcePolicy) -> dict:
    """Check sampled property shapes/values without emitting their contents.

    This describes the query snapshot, independently of later body extraction.
    People/text pagination is still performed by the full normalization path.
    """

    properties = normalize_properties(page, policy)
    people = {field: properties.get(field, []) for field in ("assignees", "attendees")}
    for entries in people.values():
        for person in entries:
            try:
                UUID(person["id"])
            except ValueError as error:
                raise SmokeValidationError("invalid_person_uuid") from error
    dates = [
        field for field in ("date", "start_date", "due_date", "end_date") if field in properties
    ]
    for field in dates:
        for key in ("start", "end"):
            if properties[field].get(key):
                try:
                    datetime.fromisoformat(properties[field][key].replace("Z", "+00:00"))
                except ValueError as error:
                    raise SmokeValidationError("invalid_date_property") from error
    return {
        "status": "passed",
        "scope": "query_metadata_snapshot",
        "lifecycle_eligible": lifecycle_reason(page, policy) is None,
        "populated_property_fields": sorted(properties),
        "people_counts_observed": {field: len(entries) for field, entries in people.items()},
        "people_names_available": {
            field: sum(person["name"] is not None for person in entries)
            for field, entries in people.items()
        },
        "people_pagination_may_be_needed": any(len(entries) >= 25 for entries in people.values()),
        "date_fields_present": dates,
    }


def validate_document(document: NormalizedDocument, page_id: str, chunker: DocumentChunker) -> dict:
    if document.source != "notion" or document.external_id != page_id:
        raise SmokeValidationError("source_identity_mismatch")
    url = urlsplit(document.source_url or "")
    if (
        url.scheme != "https"
        or not url.netloc
        or page_id.replace("-", "") not in url.path.replace("-", "")
    ):
        raise SmokeValidationError("canonical_page_url_mismatch")
    chunks = chunker.chunk_document(document)
    if not chunks:
        raise SmokeValidationError("no_searchable_chunks")
    for index, chunk in enumerate(chunks):
        if (
            chunk.source != document.source
            or chunk.external_id != page_id
            or chunk.source_url != document.source_url
            or chunk.chunk_index != index
        ):
            raise SmokeValidationError("chunk_source_mismatch")
        if (
            chunk.token_count != chunker.count_tokens(chunk.content)
            or chunk.token_count > chunker.config.max_tokens
        ):
            raise SmokeValidationError("chunk_token_budget_mismatch")
        for reference in chunk.references:
            if not 0 <= reference.section_index < len(document.sections):
                raise SmokeValidationError("invalid_section_index")
            section = document.sections[reference.section_index]
            if (
                not reference.section_reference
                or reference.section_reference != section.section_reference
                or reference.heading_path != section.heading_path
                or reference.page_number != section.page_number
            ):
                raise SmokeValidationError("invalid_section_reference")
    # Local determinism check does not make another request.
    if chunks != chunker.chunk_document(document):
        raise SmokeValidationError("non_deterministic_chunking")
    return {
        "document_kind": document.metadata.get("document_kind"),
        "lifecycle_policy": document.metadata.get("lifecycle_policy"),
        "section_count": len(document.sections),
        "body_section_count": sum(section.kind == "content" for section in document.sections),
        "property_section_count": sum(
            section.kind == "properties" for section in document.sections
        ),
        "chunk_count": len(chunks),
        "token_counts": [chunk.token_count for chunk in chunks],
        "source_identity_verified": True,
        "canonical_page_url_verified": True,
        "section_references_verified": True,
        "chunking_deterministic": True,
        "cleanup": document.metadata.get("notion_cleanup"),
        "extraction_warning_count": len(
            document.metadata.get("notion_markdown", {}).get("warnings", [])
        ),
    }


async def run_live_check(
    settings: Settings,
    page_id: str,
    *,
    repeat: bool = False,
    document_kind: DocumentKind | None = None,
) -> dict:
    report = preflight(settings, page_id)
    if report["status"] != "ready":
        return report
    page_id = report["page_id"]
    # Do not let an unrelated source policy or data-source allowlist expand this
    # smoke test. Existing per-page context/mappings are retained when configured.
    policies = {
        str(UUID(key)): value
        for key, value in settings.notion_source_policies.items()
        if str(UUID(key)) == page_id
    }
    if document_kind is not None:
        policy = policies.get(page_id, NotionSourcePolicy())
        policies[page_id] = NotionSourcePolicy.model_validate(
            {
                **policy.model_dump(),
                "document_kind": document_kind,
            }
        )
    report["document_kind_origin"] = (
        "cli_override"
        if document_kind is not None
        else "source_policy"
        if page_id in policies
        else "schema_inference"
    )
    try:
        async with NotionConnector(
            token=settings.notion_api_token.get_secret_value(),
            allowed_page_ids=[page_id],
            source_policies=policies,
            api_version=settings.notion_api_version,
        ) as connector:
            document = await connector.fetch_and_normalize_page(page_id)
            chunker = DocumentChunker()
            report.update(validate_document(document, page_id, chunker))
            if repeat:
                second = await connector.fetch_and_normalize_page(page_id)
                validate_document(second, page_id, chunker)
                if document.source_version != second.source_version:
                    report["status"] = "inconclusive_source_changed"
                    return report
                if document != second or chunker.chunk_document(document) != chunker.chunk_document(
                    second
                ):
                    raise SmokeValidationError("unchanged_source_produced_different_results")
                report["unchanged_on_repeat"] = True
        report["status"] = "passed"
    except Exception as error:
        report.update(failure_report(error))
    return report


async def run_data_source_check(
    settings: Settings,
    data_source_id: str,
    *,
    limit: int = 5,
    document_kind: DocumentKind | None = None,
    repeat: bool = False,
    eligible_only: bool = False,
) -> dict:
    report = preflight(settings, data_source_id, data_source=True)
    if report["status"] != "ready":
        return report
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Sample limit must be an integer between 1 and 5")
    source_id = report["data_source_id"]
    policies = {str(UUID(key)): value for key, value in settings.notion_source_policies.items()}
    policy = policies.get(source_id, NotionSourcePolicy())
    if document_kind is not None:
        policy = NotionSourcePolicy.model_validate(
            {**policy.model_dump(), "document_kind": document_kind}
        )
    report.update(
        sample_limit=limit,
        query_mode="unfiltered_bounded_sample",
        document_kind_origin="cli_override"
        if document_kind is not None
        else "source_policy"
        if source_id in policies
        else "schema_inference",
    )
    records = []
    markdown_reads: dict[str, int] = {}

    async def track_request(request: httpx.Request) -> None:
        parts = request.url.path.strip("/").split("/")
        if (
            request.method == "GET"
            and len(parts) == 4
            and parts[:2] == ["v1", "pages"]
            and parts[3] == "markdown"
        ):
            markdown_reads[parts[2]] = markdown_reads.get(parts[2], 0) + 1

    try:
        async with httpx.AsyncClient(event_hooks={"request": [track_request]}) as client:
            async with NotionConnector(
                token=settings.notion_api_token.get_secret_value(),
                allowed_page_ids=[],
                allowed_data_source_ids=[source_id],
                source_policies={source_id: policy},
                api_version=settings.notion_api_version,
                client=client,
            ) as connector:
                schema = await connector.fetch_notion_data_source(source_id)
                report["mapped_property_types"] = {
                    field: found[1]["type"]
                    for field in PROPERTY_ALIASES
                    if (found := find_property(schema["properties"], field, policy)) is not None
                }
                query_filter = lifecycle_query_filter(schema, policy) if eligible_only else None
                report["server_status_filter_applied"] = query_filter is not None
                if query_filter is not None:
                    report["query_mode"] = "eligible_filtered_bounded_sample"
                sample = await connector.sample_notion_pages(
                    source_id,
                    limit=limit,
                    query_filter=query_filter,
                )
                report.update(
                    sampled_records=len(sample.pages),
                    more_available=sample.has_more,
                    complete_source_query=not sample.has_more and query_filter is None,
                    complete_sample_query=not sample.has_more,
                    query_pages_read=sample.query_requests,
                )
                chunker = DocumentChunker() if sample.pages else None
                for row in sample.pages:
                    page_id = str(UUID(row["id"]))
                    record = {"page_id": page_id}
                    try:
                        record["metadata_check"] = probe_metadata(row, policy)
                        document = await connector.fetch_and_normalize_page(page_id)
                        if document.metadata.get("data_source_id") != source_id:
                            raise SmokeValidationError("data_source_membership_mismatch")
                        record.update(validate_document(document, page_id, chunker))
                        properties = document.metadata["properties"]
                        summary = property_summary(properties)
                        chunks = chunker.chunk_document(document)
                        if summary and not any(summary in chunk.content for chunk in chunks):
                            raise SmokeValidationError("property_evidence_not_preserved")
                        record.update(
                            populated_property_fields=sorted(properties),
                            people_counts={
                                field: len(properties.get(field, []))
                                for field in ("assignees", "attendees")
                            },
                            date_fields_present=[
                                field
                                for field in ("date", "start_date", "due_date", "end_date")
                                if field in properties
                            ],
                            property_evidence_verified=True,
                            data_source_membership_verified=True,
                            status="passed",
                        )
                        if repeat:
                            second = await connector.fetch_and_normalize_page(page_id)
                            validate_document(second, page_id, chunker)
                            if document.source_version != second.source_version:
                                record["status"] = "inconclusive_source_changed"
                            elif document != second or chunks != chunker.chunk_document(second):
                                raise SmokeValidationError(
                                    "unchanged_source_produced_different_results"
                                )
                            else:
                                record["unchanged_on_repeat"] = True
                    except Exception as error:
                        record.update(failure_report(error))
                    record["markdown_requests"] = markdown_reads.get(page_id, 0)
                    if record["status"] == "excluded":
                        record["excluded_before_body_verified"] = record["markdown_requests"] == 0
                        if not record["excluded_before_body_verified"]:
                            record.update(status="failed", reason="excluded_body_was_fetched")
                    records.append(record)
        report["records"] = records
        report["passed_records"] = sum(row["status"] == "passed" for row in records)
        report["excluded_records"] = sum(row["status"] == "excluded" for row in records)
        report["failed_records"] = sum(
            row["status"] not in {"passed", "excluded"} for row in records
        )
        report["inactive_exclusion_exercised"] = report["excluded_records"] > 0
        report["status"] = (
            "needs_review"
            if report["failed_records"]
            else "passed"
            if report["passed_records"]
            else "inconclusive_no_eligible_records"
            if records
            else "inconclusive_no_records"
        )
    except Exception as error:
        report.update(failure_report(error))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--page-id", help="Explicitly allowlisted pilot page UUID")
    target.add_argument("--data-source-id", help="Explicitly allowlisted Notion data-source UUID")
    parser.add_argument(
        "--limit", type=int, choices=range(1, 6), default=5, help="Maximum sampled rows (1-5)"
    )
    parser.add_argument("--preflight", action="store_true", help="Check configuration without I/O")
    parser.add_argument(
        "--eligible-only",
        action="store_true",
        help="Use the production status filter for a data-source sample",
    )
    parser.add_argument(
        "--repeat", action="store_true", help="Repeat live read to check stable output"
    )
    parser.add_argument(
        "--document-kind",
        choices=[
            "announcement",
            "meeting_note",
            "task",
            "project",
            "technical_doc",
            "timeline_item",
        ],
        help="Explicit kind for this test only; leaves .env unchanged",
    )
    args = parser.parse_args(argv)
    if args.eligible_only and not args.data_source_id:
        parser.error("--eligible-only requires --data-source-id")
    try:
        settings = Settings()
        report = preflight(
            settings,
            args.data_source_id or args.page_id,
            data_source=bool(args.data_source_id),
        )
        if not args.preflight and report["status"] == "ready":
            if args.data_source_id:
                report = asyncio.run(
                    run_data_source_check(
                        settings,
                        args.data_source_id,
                        limit=args.limit,
                        eligible_only=args.eligible_only,
                        repeat=args.repeat,
                        document_kind=args.document_kind,
                    )
                )
            else:
                report = asyncio.run(
                    run_live_check(
                        settings,
                        args.page_id,
                        repeat=args.repeat,
                        document_kind=args.document_kind,
                    )
                )
    except (ValueError, ValidationError):
        report = {"status": "blocked_configuration", "reason": "invalid_settings_or_page_uuid"}
    print(json.dumps(report, indent=2))
    return 0 if report["status"] in {"ready", "passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
