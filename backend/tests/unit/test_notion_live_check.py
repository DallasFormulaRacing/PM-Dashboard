"""The live diagnostic must enforce approval and avoid printing source content."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.connectors.notion import NotionConnector


BACKEND = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "notion_live_check", BACKEND / "scripts/check_notion_live.py"
)
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)
PAGE_ID = "11111111-1111-4111-8111-111111111111"


def settings(**overrides):
    return Settings(
        _env_file=None,
        NOTION_API_TOKEN=overrides.pop("token", "unit-test-token-never-use-live"),
        NOTION_ALLOWED_PAGE_IDS=overrides.pop("allowed", [PAGE_ID]),
        NOTION_ALLOWED_DATA_SOURCE_IDS=overrides.pop("sources", []),
        NOTION_SOURCE_POLICIES=overrides.pop("policies", {}),
        **overrides,
    )


@pytest.mark.parametrize(
    ("token", "allowed", "expected"),
    [
        (None, [], ["NOTION_API_TOKEN", "pilot_in_NOTION_ALLOWED_PAGE_IDS"]),
        ("unit-test-token", [], ["pilot_in_NOTION_ALLOWED_PAGE_IDS"]),
        ("replace_with_real_token", [PAGE_ID], ["NOTION_API_TOKEN"]),
    ],
)
def test_preflight_does_not_connect_when_configuration_is_missing(token, allowed, expected):
    report = live.preflight(settings(token=token, allowed=allowed), PAGE_ID)
    assert report["status"] == "blocked_configuration"
    assert report["missing_requirements"] == expected
    assert token is None or token not in json.dumps(report)


def install_transport(monkeypatch, *, api_error=False, changed=False, incorrect_url=False):
    calls = []
    page_reads = 0
    fixtures = BACKEND / "tests/fixtures/notion"

    def handler(request):
        nonlocal page_reads
        calls.append(request.url.path)
        if api_error:
            return httpx.Response(
                403,
                json={
                    "code": "restricted_resource",
                    "message": "PRIVATE SOURCE TEXT MUST NOT APPEAR",
                },
            )
        if request.url.path.endswith("/markdown"):
            return httpx.Response(
                200, json=json.loads((fixtures / "markdown_response.json").read_text())
            )
        page_reads += 1
        page = json.loads((fixtures / "page_response.json").read_text())
        if changed and page_reads > 1:
            page["last_edited_time"] = "2026-10-29T12:00:00Z"
        if incorrect_url:
            page["url"] = "https://www.notion.so/another-page"
        return httpx.Response(200, json=page)

    def connector(**kwargs):
        connector = NotionConnector(
            **kwargs,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        connector._owns_client = True
        return connector

    monkeypatch.setattr(live, "NotionConnector", connector)
    return calls


def test_smoke_check_reports_counts_and_checks_without_content_or_credentials(monkeypatch):
    calls = install_transport(monkeypatch)
    report = asyncio.run(live.run_live_check(settings(), PAGE_ID, repeat=True))
    assert report["status"] == "passed"
    assert report["unchanged_on_repeat"] is True
    assert report["chunking_deterministic"] is True
    assert report["section_references_verified"] is True
    assert report["section_count"] == 2
    assert report["chunk_count"] == 2
    assert len(calls) == 4
    assert all(value <= 700 for value in report["token_counts"])
    serialized = json.dumps(report)
    for forbidden in ("Synthetic Program", "mentoring", "Programs", "unit-test-token"):
        assert forbidden not in serialized


def test_api_error_details_are_not_printed(monkeypatch):
    install_transport(monkeypatch, api_error=True)
    report = asyncio.run(live.run_live_check(settings(), PAGE_ID))
    assert report["status"] == "failed"
    assert report["http_status"] == 403
    assert "PRIVATE SOURCE TEXT" not in json.dumps(report)


def test_explicit_meeting_kind_applies_without_changing_settings(monkeypatch):
    install_transport(monkeypatch)
    original = settings()
    report = asyncio.run(live.run_live_check(original, PAGE_ID, document_kind="meeting_note"))
    assert report["status"] == "passed"
    assert report["document_kind"] == "meeting_note"
    assert report["document_kind_origin"] == "cli_override"
    assert original.notion_source_policies == {}


def test_changed_source_is_inconclusive_instead_of_a_determinism_failure(monkeypatch):
    install_transport(monkeypatch, changed=True)
    report = asyncio.run(live.run_live_check(settings(), PAGE_ID, repeat=True))
    assert report["status"] == "inconclusive_source_changed"


def test_citation_url_must_point_at_the_requested_page(monkeypatch):
    install_transport(monkeypatch, incorrect_url=True)
    report = asyncio.run(live.run_live_check(settings(), PAGE_ID))
    assert report["status"] == "failed"
    assert report["reason"] == "canonical_page_url_mismatch"


def test_cli_bad_configuration_never_prints_validation_input(monkeypatch, capsys):
    def invalid_settings():
        raise ValueError("PRIVATE TOKEN VALUE")

    monkeypatch.setattr(live, "Settings", invalid_settings)
    assert live.main(["--page-id", PAGE_ID, "--preflight"]) == 2
    assert "PRIVATE TOKEN VALUE" not in capsys.readouterr().out


SOURCE_ID = "33333333-3333-4333-8333-333333333333"
ROW_ID = "44444444-4444-4444-8444-444444444444"
INACTIVE_ID = "77777777-7777-4777-8777-777777777777"


def install_source_transport(monkeypatch, *, incomplete=False, extra=False, attachment=False):
    from copy import deepcopy

    fixtures = BACKEND / "tests/fixtures/notion"
    active = json.loads((fixtures / "task_page_response.json").read_text())
    inactive = deepcopy(active)
    inactive["id"] = INACTIVE_ID
    inactive["properties"]["Status"]["status"]["name"] = "Completed"
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == f"/v1/data_sources/{SOURCE_ID}":
            return httpx.Response(
                200, json=json.loads((fixtures / "task_data_source_response.json").read_text())
            )
        if request.url.path.endswith("/query"):
            limit = json.loads(request.content)["page_size"]
            response = {
                "results": [active, inactive][:limit],
                "has_more": extra,
                "next_cursor": "next" if extra else None,
            }
            if incomplete:
                response["request_status"] = {"type": "incomplete"}
            return httpx.Response(200, json=response)
        if request.url.path.endswith("/markdown"):
            assert INACTIVE_ID not in request.url.path
            body = json.loads((fixtures / "task_markdown_response.json").read_text())
            if attachment:
                body["markdown"] = '<pdf src="https://example.invalid/private.pdf" />'
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=inactive if INACTIVE_ID in request.url.path else active)

    def connector(**kwargs):
        supplied = kwargs.pop("client", None)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks=supplied.event_hooks if supplied else None,
        )
        result = NotionConnector(**kwargs, client=client)
        result._owns_client = True
        return result

    monkeypatch.setattr(live, "NotionConnector", connector)
    return calls


def test_source_preflight_requires_source_approval_even_when_page_is_allowed():
    report = live.preflight(settings(), SOURCE_ID, data_source=True)
    assert report["status"] == "blocked_configuration"
    assert report["missing_requirements"] == ["source_in_NOTION_ALLOWED_DATA_SOURCE_IDS"]


def test_bounded_source_smoke_covers_people_dates_and_inactive_exclusion(monkeypatch):
    calls = install_source_transport(monkeypatch)
    report = asyncio.run(
        live.run_data_source_check(
            settings(sources=[SOURCE_ID]),
            SOURCE_ID,
            document_kind="task",
            repeat=True,
        )
    )
    assert report["status"] == "passed"
    assert report["sampled_records"] == 2
    assert report["passed_records"] == report["excluded_records"] == 1
    assert report["inactive_exclusion_exercised"] is True
    assert report["mapped_property_types"]["assignees"] == "people"
    active, inactive = report["records"]
    assert active["people_counts"]["assignees"] == 1
    assert active["date_fields_present"] == ["due_date"]
    assert active["property_evidence_verified"] is True
    assert active["unchanged_on_repeat"] is True
    assert active["markdown_requests"] == 2
    assert inactive["markdown_requests"] == 0
    assert inactive["excluded_before_body_verified"] is True
    serialized = json.dumps(report)
    for forbidden in ("Synthetic Engineer", "TEST-42", "synthetic@example", "unit-test-token"):
        assert forbidden not in serialized
    assert len([call for call in calls if call.url.path.endswith("/query")]) == 1


def test_capped_source_sample_does_not_claim_complete_query(monkeypatch):
    calls = install_source_transport(monkeypatch, extra=True)
    report = asyncio.run(
        live.run_data_source_check(
            settings(sources=[SOURCE_ID]),
            SOURCE_ID,
            limit=1,
            document_kind="task",
        )
    )
    assert report["status"] == "passed"
    assert report["sampled_records"] == 1
    assert report["more_available"] is True
    assert report["complete_source_query"] is False
    assert report["inactive_exclusion_exercised"] is False
    assert len([call for call in calls if call.url.path.endswith("/query")]) == 1


def test_failed_record_is_reported_without_weakening_attachment_checks(monkeypatch):
    install_source_transport(monkeypatch, attachment=True)
    report = asyncio.run(live.run_data_source_check(settings(sources=[SOURCE_ID]), SOURCE_ID))
    assert report["status"] == "needs_review"
    assert report["failed_records"] == report["excluded_records"] == 1
    assert report["records"][0]["reason"] == "attachment_extraction_required"
    assert "private.pdf" not in json.dumps(report)


def test_incomplete_source_query_does_not_fetch_any_page_bodies(monkeypatch):
    calls = install_source_transport(monkeypatch, incomplete=True)
    report = asyncio.run(live.run_data_source_check(settings(sources=[SOURCE_ID]), SOURCE_ID))
    assert report["status"] == "failed"
    assert not any("/pages/" in call.url.path for call in calls)


def test_eligible_sample_uses_shared_status_filter_and_rechecks_each_row(monkeypatch):
    calls = install_source_transport(monkeypatch)
    report = asyncio.run(
        live.run_data_source_check(
            settings(sources=[SOURCE_ID]),
            SOURCE_ID,
            eligible_only=True,
        )
    )
    assert report["server_status_filter_applied"] is True
    assert report["query_mode"] == "eligible_filtered_bounded_sample"
    assert report["complete_sample_query"] is True
    assert report["complete_source_query"] is False
    query = next(call for call in calls if call.url.path.endswith("/query"))
    assert json.loads(query.content)["filter"] == {
        "or": [
            {"property": "state", "status": {"equals": "Not Started"}},
            {"property": "state", "status": {"equals": "In Progress"}},
            {"property": "state", "status": {"equals": "Waiting"}},
        ]
    }
    # The mock intentionally returns a completed row despite the server filter.
    assert report["excluded_records"] == 1
    assert report["records"][1]["excluded_before_body_verified"] is True


def test_source_kind_comes_from_persisted_policy_when_no_cli_override(monkeypatch):
    install_source_transport(monkeypatch)
    configured = settings(
        sources=[SOURCE_ID],
        policies={SOURCE_ID: {"document_kind": "meeting_note"}},
    )
    report = asyncio.run(live.run_data_source_check(configured, SOURCE_ID))
    assert report["status"] == "passed"
    assert report["document_kind_origin"] == "source_policy"
    assert report["records"][0]["document_kind"] == "meeting_note"
