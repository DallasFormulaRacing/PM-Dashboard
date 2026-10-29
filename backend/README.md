# RAG Backend

This directory contains the independently deployable RAG service. The current
implementation includes the PostgreSQL/pgvector persistence schema, Alembic
migrations, fixture-tested Notion record normalization and cleanup, and local
token-aware chunking. API, embedding, retrieval, and generation modules are
still placeholders.

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
as subtrees and restored at their original placeholders. Recovery rejects cycles
and missing placement markers instead of appending content under a different
heading. A page is not normalized if any subtree remains unresolved, which
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

### Checkpoint 2: cleanup and chunking

`app.ingestion.notion_markdown.clean_notion_markdown` groups enhanced Markdown
into heading sections and retains lists, code, callouts, toggles, and tables as
structural units. Presentation columns are flattened. Empty checklist items
are removed only when they have no meaningful children. Exact known task/project
template paragraphs and empty placeholder blocks are removed; code and quoted
text are preserved. To keep exact template phrases in a particular source, set
`remove_template_placeholders: false` in that source's policy. Cleanup counts and
its version are recorded under `metadata.notion_cleanup`.

`DocumentSection` now has optional `section_reference` and a `kind` distinguishing
body content from generated property evidence. `markdown:lines:start-end` refers
to the completed Markdown snapshot after subtree recovery, not a Notion URL
fragment. `notion:properties` identifies generated property evidence. No block
anchors are fabricated. Chunk references retain section indexes, heading paths,
page numbers, and these selectors; citations still use the canonical document URL.

`app.ingestion.chunking.DocumentChunker` emits validated `DocumentChunk` objects
with source identity/URL, ordered indexes, evidence text, source references,
token counts, hashes, tokenizer, and strategy version. Title, configured source
path, and available meeting/record date are included in the counted text.

Default `ChunkingConfig` values:

| Setting | Default |
| --- | --- |
| Prose/section packing target | 500 tokens |
| Maximum final chunk | 700 tokens |
| Whole short announcement | 400 tokens |
| Overlap within an oversized prose paragraph | Up to 50 tokens |
| Tokenizer | `cl100k_base` |

Short tasks and timeline records stay whole when they fit the maximum. Meeting
notes, projects, and technical documents retain heading boundaries. Long prose
is split at natural boundaries where possible, then at Unicode character
boundaries. Overlap never crosses sections or source pages. Long pipe tables
and native Notion XML tables are split between complete rows, repeating actual
headers. Lists, code, callouts, toggles, property records, and single table rows
that cannot fit raise `ChunkingError`; the caller receives no partial chunk list.
This conservative limit is an explicit review case, not silent truncation.

Hashes are deterministic for source identity, evidence, references, configuration,
and strategy version. They are preparation for later indexing, not a persistence
or embedding implementation. The generic chunker also retains page locations
from the existing extracted Box fixture; Box download/Docling remain separate work.

Install the updated dependencies with `python -m pip install -e '.[dev]'`.
Tokenization runs locally. On the first use, tiktoken downloads its public
vocabulary; no document text is sent. Prewarm it while network is available:

```bash
python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
```

Then inspect the synthetic examples from `backend/`:

```bash
python scripts/preview_notion_fixture.py --example task --chunks-only
python scripts/preview_notion_fixture.py --example meeting --chunks-only
python scripts/preview_notion_fixture.py --example technical --chunks-only
python scripts/preview_notion_fixture.py --status Draft --chunks-only
python -m pytest tests/unit tests/e2e
```

Omit `--chunks-only` to see the normalized sections and cleanup counts beside the
chunks. All examples use mock HTTP responses and synthetic content. Real media
attachments, unresolved unknown blocks, or unbalanced containers raise extraction
errors: attachment text/OCR and structured-block fallback are not implemented.
Live validation is checkpoint 3, following user review of this checkpoint.

Implementation references:

- <https://developers.notion.com/guides/data-apis/enhanced-markdown>
- <https://markdown-it-py.readthedocs.io/en/latest/using.html>
- <https://github.com/openai/tiktoken>

### Checkpoint 3: controlled live pilot

The single-page F1 Software meeting-note pilot passed on 2026-10-29 using the
backend's own integration token and official REST API version `2026-03-11`.
It produced 2 sections (one body, one properties) and 2 chunks containing 107
and 19 tokens. Page identity, canonical page URL, section references, final
token counts, and local chunk determinism passed. A second live read returned
the same source version and identical normalized documents/chunks. Notion
reported zero extraction warnings. No page text or credentials were saved to
fixtures, no embeddings/generation were requested, and no database writes ran.

The pilot has a generic title property and no status property, so ordinary
schema inference returns `technical_doc` and `implicit_active`. A second test
with explicit `meeting_note` classification also passed. The CLI override below
affects only that test; set `document_kind: "meeting_note"` in the page's
`NOTION_SOURCE_POLICIES` entry for the same classification during regular ingestion.

To reproduce, set `NOTION_API_TOKEN` in the ignored repository-root `.env`,
include the pilot UUID in `NOTION_ALLOWED_PAGE_IDS`, and share the page with
that read-only integration. Keep the token out of source control and chat.
From `backend/`, replacing `APPROVED_PAGE_UUID` with the explicitly approved ID:

```bash
python scripts/check_notion_live.py --page-id APPROVED_PAGE_UUID --preflight
python scripts/check_notion_live.py --page-id APPROVED_PAGE_UUID --document-kind meeting_note --repeat
```

Preflight performs no network I/O. The live command reads only the selected page
and any unknown subtrees returned by its Markdown response. It reports counts,
fixed validation results, cleanup statistics, and sanitized error categories;
it does not print the page title/body, people, raw properties, or API error
messages. Exit code 0 means ready/passed; 2 means blocked, excluded, failed, or
inconclusive because the source changed between reads. Mock tests cover the
diagnostic itself; `python -m pytest tests/unit tests/e2e` never invokes live Notion.

That initial pilot validated one page. The bounded database tests below extend
the live coverage; they do not validate the entire workspace.

### Bounded Notion database tests

The diagnostic also accepts `--data-source-id`. It retrieves the approved schema
and samples at most five direct records, using at most five query pages (with
the connector's existing bounded retries). It never follows other data sources.
An unfiltered sample includes inactive metadata so the test can verify that
fresh lifecycle checks exclude records before any Markdown request.

```bash
python scripts/check_notion_live.py --data-source-id APPROVED_DATA_SOURCE_UUID --preflight
python scripts/check_notion_live.py --data-source-id APPROVED_DATA_SOURCE_UUID --limit 5 --repeat
python scripts/check_notion_live.py --data-source-id APPROVED_DATA_SOURCE_UUID --limit 5 --eligible-only --repeat
```

The ID must be in `NOTION_ALLOWED_DATA_SOURCE_IDS` and shared with the backend
integration. Source policies in `.env` supply document kind and property mappings;
`--document-kind` remains an optional test-only override. `--eligible-only` uses
the same schema-backed status filter as production batch normalization. Local
fresh-page lifecycle and source-membership checks still run. The report says
whether that server filter was applied, whether more records are available,
and whether the selected query finished. A limited sample is never a full sync
or a basis for deleting indexed records. `sample_notion_pages` is deliberately
separate from the complete-query `list_notion_pages` API.

Results on 2026-10-29: **16 distinct records across four sources; 9 processed
into 12 chunks, 5 excluded before body access, and 2 blocked by unsupported
content**. Repeat reads of successful records produced identical output. The
published attachment-based meeting note and the task with two unresolved blocks
were not accepted as healthy documents. Metadata checks are reported separately
from extraction success, so property coverage cannot hide a failed body parse.

See [the validation report](docs/notion-live-validation-2026-10-29.md) for sample
breakdown, property-only records, and remaining coverage gaps. The local ignored
`.env` now contains the four test source IDs and explicit meeting/task kinds;
the integration token was left unchanged. Tests did not create a database source
registry, persist organizational documents, or call embeddings/generation.

### Reviewing actual chunks locally

The live diagnostic intentionally prints no chunk text. To explicitly export
approved real content for human review, use a Git-ignored local directory:

```bash
python scripts/export_notion_chunks.py --data-source-id APPROVED_DATA_SOURCE_UUID --output-dir .local/notion-review
```

Repeat `--data-source-id` to select additional approved sources (maximum four).
The exporter samples at most five eligible records per source, uses configured
source policies, and produces `chunks.md` plus `chunks.json`. Markdown shows the
exact chunk text, source URLs, references, and token counts; JSON also includes
normalized document sections. Optional repeated `--expected-page-id` arguments
require the sample to match a known record set before any review files are written.

These are refreshed live snapshots, not archived copies of earlier in-memory
tests. Files have private permissions and existing directories are never
overwritten. `/backend/.local/` is ignored by Git because exports contain actual
organizational text and people metadata. No embedding or database indexing runs.

## Run tests

With the Docker database healthy, run from `backend/`:

```bash
python -m pytest
```

The integration test creates a disposable database, applies the migrations
twice, verifies constraints/vector/full-text behavior and cascade deletion, and
then removes the disposable database. It does not modify `f1rag_local`.
