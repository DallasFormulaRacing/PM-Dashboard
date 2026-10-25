# RAG Backend

This directory contains the independently deployable RAG service. The current
implementation includes the PostgreSQL/pgvector persistence schema, Alembic
migrations, and fixture-tested Notion record normalization. API, chunking,
embedding, retrieval, and generation modules are still placeholders.

## Local database

From the repository root, start PostgreSQL:

```bash
docker compose up -d
docker compose ps
```

Create a local `.env` from `../.env.example` and use local-only credentials.
The default host port is `5433` because port `5432` may already be occupied by
a host PostgreSQL installation.

## Install backend dependencies

The repository currently lives below a directory containing `:`, which Python
cannot use in a virtual-environment path on macOS/Linux. Create the environment
outside the repository (or rename that parent directory), then install the
project and test dependencies from `backend/`:

```bash
python3 -m venv ~/.virtualenvs/f1rag-backend
source ~/.virtualenvs/f1rag-backend/bin/activate
python -m pip install -e '.[dev]'
```

## Run migrations

From `backend/`:

```bash
python -m alembic upgrade head
python -m alembic current
```

The first migration enables pgvector, creates the `rag` schema, and creates:

- `rag.source_registry`
- `rag.documents`
- `rag.chunks`
- `rag.sync_runs`

It uses exact vector search initially; no HNSW or IVFFlat index is created.

## Normalized documents

`app.domain.documents.NormalizedDocument` is the shared boundary between source
connectors and the rest of the pipeline. Both Notion Markdown and extracted Box
files must produce this model before chunking, embedding, or persistence. The
contract rejects blank documents, unsupported sources, ambiguous timestamps,
invalid citation URLs, non-JSON metadata, and unknown fields.

Synthetic Notion and extracted-PDF fixtures live under `tests/fixtures/`; no
external source credentials or organizational content are required by the unit
tests.

## Notion connector

The asynchronous connector in `app.connectors.notion` reads only page IDs in
`NOTION_ALLOWED_PAGE_IDS` or pages discovered by querying IDs in
`NOTION_ALLOWED_DATA_SOURCE_IDS`. Both settings use JSON-array syntax:

```dotenv
NOTION_API_TOKEN=replace_with_a_read_only_integration_token
NOTION_API_VERSION=2026-03-11
NOTION_ALLOWED_PAGE_IDS=["11111111-1111-4111-8111-111111111111"]
NOTION_ALLOWED_DATA_SOURCE_IDS=[]
```

The connector intentionally does not use broad `/v1/search` discovery. It uses
the official page metadata, page Markdown, and data-source query endpoints. It
supports cursor pagination, bounded retries for transient/rate-limit responses,
`Retry-After`, title/category extraction, heading-aware normalization, and
explicit Markdown warnings.

When a Markdown response is truncated, returned unknown block IDs are fetched
as subtrees. A page is not normalized if any subtree remains unresolved, which
prevents partial content from being silently indexed. Unsupported-content
warnings are retained in `NormalizedDocument.metadata`.

The checked-in Notion responses are synthetic. Do not commit a real page dump
until it has been reviewed and sanitized for organizational or personal data.

Official references:

- <https://developers.notion.com/reference/retrieve-a-page>
- <https://developers.notion.com/reference/retrieve-page-markdown>
- <https://developers.notion.com/reference/query-a-data-source>
- <https://developers.notion.com/reference/request-limits>
- <https://developers.notion.com/reference/retrieve-a-page-property>

### Checkpoint 1: Notion records and source policies

`fetch_and_normalize_page(page_id)` now enforces lifecycle eligibility before
fetching Markdown. `fetch_and_normalize_data_source(data_source_id)` retrieves
the approved schema, paginates direct rows, re-fetches each row, and returns a
`NotionBatchResult` containing `documents` and `skipped` records with reasons.
Extraction, schema, access, and transport failures raise an exception instead of
returning a successful partial batch. This is in-memory ingestion; it does not
write documents or embeddings to PostgreSQL.

Eligible status values are `Published`, `Not started`, `In progress`, and
`Waiting` (case-insensitive). Drafts, reviews, completed/outdated records,
unknown/empty statuses, and archived/trashed pages are excluded. Statusless
sources use `implicit_active`, meaning non-archived/non-trashed; modification
dates alone do not establish that information is current. Status/select schemas
also produce server-side query filters, with eligibility checked again on the
fresh page response. A schema-declared property missing from a row is an error,
not an implicit statusless source.

Selected facts are stored under `metadata.properties` and rendered into a
`Record properties` section, so property-only records can be searched later.
Supported fields are status, categories/tags, priority, date/start/due/end date
ranges, summary/TLDR, description, assignees, and attendees. People retain IDs
and available display names; missing names remain null. User-directory lookup
is not performed. Raw person emails/avatars, file properties, relations, and
unmapped properties are not copied. This does not redact emails written into
approved page body text or description fields.

Metadata also preserves document kind, canonical page ID/URL, parent type/ID,
parent data-source ID/name (name when its schema was fetched), timestamps,
configured ancestor path, lifecycle policy, and extraction warnings. The REST
API does not return the Notion plugin's full ancestor-path envelope. Paths are
explicit source context, marked `source_policy` or `unavailable`; the connector
does not crawl ancestors to reconstruct them. Name-based property aliases are
case-insensitive. Ambiguous aliases require an explicit override. Property IDs
are preferred for overrides because they survive renames.

Add optional policies to the repository-root `.env` (the IDs below are synthetic):

```dotenv
NOTION_SOURCE_POLICIES={"33333333-3333-4333-8333-333333333333":{"document_kind":"task","ancestor_path":["Synthetic Team","Tasks Tracker"],"property_overrides":{"status":"state","assignees":"owners"}}}
```

Each policy key must independently appear in `NOTION_ALLOWED_PAGE_IDS` or
`NOTION_ALLOWED_DATA_SOURCE_IDS`. Policies do not grant access. A page-specific
policy takes precedence over a data-source policy. Mark navigation containers
as `document_kind: "hub"` to exclude them. Other supported kinds are
`announcement`, `meeting_note`, `task`, `project`, `technical_doc`, and
`timeline_item`. Without a policy, familiar title-property names determine kind;
otherwise it defaults to `technical_doc`. Use explicit kinds for sources named
`Name` or `title` that cannot be classified reliably from their schema.

Discovery never follows linked pages, attachments, nested databases, or ancestor
pages. A discovered page must still belong to its approved data source when
normalized. Failed/incomplete queries grant no new page access; rerunning a
query replaces that source's prior in-memory discovery grants. The low-level
`list_notion_pages` and compatibility alias `list_notion_announcements` return
raw row metadata, not eligibility-filtered documents. Applications should use
the normalization methods. This checkpoint does not enforce database retrieval
or revoke already-persisted documents; those serving/sync paths are future work.

To inspect this checkpoint without credentials, run from `backend/` using the
installed backend environment:

```bash
python -m pytest tests/unit
python scripts/preview_notion_fixture.py
python scripts/preview_notion_fixture.py --status Draft
python scripts/preview_notion_fixture.py --status Completed
```

The preview uses only synthetic JSON responses through `httpx.MockTransport`.
The default produces one normalized task with body and property evidence; the
other commands produce zero documents and a skip reason. These fixtures mirror
observed workspace shapes but contain no copied organizational records.

Template cleanup, chunk-size/token limits, section/block references, attachment
extraction, and live validation are subsequent checkpoints. The current Markdown
completeness checks still reject unresolved unknown blocks; an unsupported embed
can therefore prevent an otherwise eligible record from normalizing.

## Run tests

With the Docker database healthy, run from `backend/`:

```bash
python -m pytest
```

The integration test creates a disposable database, applies the migrations
twice, verifies constraints/vector/full-text behavior and cascade deletion, and
then removes the disposable database. It does not modify `f1rag_local`.
