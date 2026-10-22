# RAG Backend

This directory contains the independently deployable RAG service. The current
implementation includes the PostgreSQL/pgvector persistence schema and Alembic
migration infrastructure. API, ingestion, retrieval, and generation modules are
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

## Run tests

With the Docker database healthy, run from `backend/`:

```bash
python -m pytest
```

The integration test creates a disposable database, applies the migrations
twice, verifies constraints/vector/full-text behavior and cascade deletion, and
then removes the disposable database. It does not modify `f1rag_local`.
