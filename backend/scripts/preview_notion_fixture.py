"""Print normalized synthetic records through the real connector, with no network.

Run from backend/: python scripts/preview_notion_fixture.py [--status Draft]
"""

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

import httpx

from app.connectors.notion import NotionConnector
from app.connectors.notion_properties import NotionSourcePolicy


FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "notion"


async def preview(status: str) -> dict:
    page = json.loads((FIXTURES / "task_page_response.json").read_text())
    schema = json.loads((FIXTURES / "task_data_source_response.json").read_text())
    markdown = json.loads((FIXTURES / "task_markdown_response.json").read_text())
    page["properties"]["Status"]["status"]["name"] = status
    source_id = schema["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/data_sources/{source_id}":
            payload = schema
        elif request.url.path == f"/v1/data_sources/{source_id}/query":
            # Return the record regardless of filter to demonstrate the local
            # enforcement boundary even if its status changed after the query.
            payload = {"object": "list", "results": [page], "has_more": False}
        elif request.url.path == f"/v1/pages/{page['id']}":
            payload = page
        elif request.url.path == f"/v1/pages/{page['id']}/markdown":
            payload = markdown
        else:
            raise AssertionError(f"Unexpected fixture request: {request.url.path}")
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotionConnector(
            token="synthetic-preview-token", allowed_page_ids=[],
            allowed_data_source_ids=[source_id], client=client,
            source_policies={source_id: NotionSourcePolicy(
                document_kind="task", ancestor_path=("Synthetic Engineering", "Tasks Tracker"),
            )},
        ) as connector:
            batch = await connector.fetch_and_normalize_data_source(source_id)
    return {
        "documents": [doc.model_dump(mode="json") for doc in batch.documents],
        "skipped": [asdict(record) for record in batch.skipped],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default="In Progress", help="Synthetic record status")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(preview(args.status)), indent=2))
