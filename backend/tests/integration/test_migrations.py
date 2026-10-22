"""Integration coverage for the complete database migration contract."""

from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
import pytest
from sqlalchemy.engine import URL, make_url

from app.config import get_settings


BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG = BACKEND_ROOT / "alembic.ini"


def _psycopg_parameters(url: URL, database: str) -> dict[str, str | int]:
    return {
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "dbname": database,
        "user": url.username or "",
    }


def _run_alembic(database_url: str, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_CONFIG), *arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def migrated_database_url() -> Iterator[str]:
    """Create, migrate, and remove a disposable PostgreSQL database."""

    base_url = make_url(get_settings().sqlalchemy_database_url)
    database_name = f"f1rag_test_{uuid4().hex[:12]}"
    test_url = base_url.set(database=database_name)
    admin_parameters = _psycopg_parameters(base_url, "postgres")
    previous_pgpassword = os.environ.get("PGPASSWORD")
    os.environ["PGPASSWORD"] = base_url.password or ""

    with psycopg.connect(**admin_parameters, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )

    try:
        rendered_url = test_url.render_as_string(hide_password=False)
        _run_alembic(rendered_url, "upgrade", "head")
        _run_alembic(rendered_url, "upgrade", "head")
        _run_alembic(rendered_url, "check")
        yield rendered_url
    finally:
        with psycopg.connect(**admin_parameters, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
            )
        if previous_pgpassword is None:
            os.environ.pop("PGPASSWORD", None)
        else:
            os.environ["PGPASSWORD"] = previous_pgpassword


def test_migration_creates_expected_schema(migrated_database_url: str) -> None:
    url = make_url(migrated_database_url)
    parameters = _psycopg_parameters(url, url.database or "")

    with psycopg.connect(**parameters) as connection:
        tables = connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'rag'
            ORDER BY table_name
            """
        ).fetchall()
        vector_version = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()

    assert [row[0] for row in tables] == [
        "chunks",
        "documents",
        "source_registry",
        "sync_runs",
    ]
    assert vector_version is not None


def test_constraints_search_and_cascade(migrated_database_url: str) -> None:
    url = make_url(migrated_database_url)
    parameters = _psycopg_parameters(url, url.database or "")
    first_embedding = "[" + ",".join(["1"] + ["0"] * 1535) + "]"
    second_embedding = "[" + ",".join(["0", "1"] + ["0"] * 1534) + "]"

    with psycopg.connect(**parameters, autocommit=True) as connection:
        source_id = connection.execute(
            """
            INSERT INTO rag.source_registry (source, external_id, source_type)
            VALUES ('notion', 'approved-page-1', 'page')
            RETURNING id
            """
        ).fetchone()[0]
        document_id = connection.execute(
            """
            INSERT INTO rag.documents (
                approved_via_source_id,
                source,
                external_id,
                title,
                content_hash
            )
            VALUES (%s, 'notion', 'approved-page-1', 'Benefits Guide', 'document-hash')
            RETURNING id
            """,
            (source_id,),
        ).fetchone()[0]

        with pytest.raises(UniqueViolation):
            connection.execute(
                """
                INSERT INTO rag.documents (
                    approved_via_source_id,
                    source,
                    external_id,
                    title,
                    content_hash
                )
                VALUES (%s, 'notion', 'approved-page-1', 'Duplicate', 'duplicate-hash')
                """,
                (source_id,),
            )

        connection.execute(
            """
            INSERT INTO rag.chunks (
                document_id,
                chunk_index,
                content,
                embedding,
                heading_path,
                token_count,
                content_hash,
                embedding_model,
                embedding_version
            )
            VALUES
                (%s, 0, 'Benefits enrollment closes Friday.', %s::vector,
                 ARRAY['Benefits'], 5, 'chunk-hash-1', 'text-embedding-3-small', 'v1'),
                (%s, 1, 'The office parking policy was updated.', %s::vector,
                 ARRAY['Facilities'], 7, 'chunk-hash-2', 'text-embedding-3-small', 'v1')
            """,
            (document_id, first_embedding, document_id, second_embedding),
        )

        full_text_matches = connection.execute(
            """
            SELECT chunk_index
            FROM rag.chunks
            WHERE text_search @@ plainto_tsquery('english', 'benefits enrollment')
            """
        ).fetchall()
        nearest_chunk = connection.execute(
            """
            SELECT chunk_index
            FROM rag.chunks
            ORDER BY embedding <=> %s::vector
            LIMIT 1
            """,
            (first_embedding,),
        ).fetchone()

        assert full_text_matches == [(0,)]
        assert nearest_chunk == (0,)

        connection.execute("DELETE FROM rag.documents WHERE id = %s", (document_id,))
        remaining_chunks = connection.execute(
            "SELECT count(*) FROM rag.chunks WHERE document_id = %s",
            (document_id,),
        ).fetchone()[0]

    assert remaining_chunks == 0
