# Controlled Notion data-source validation — 2026-10-29

The backend integration, using REST API `2026-03-11`, queried four explicitly
allowlisted sources. Each run sampled no more than five direct records. A second
Middleware run applied the production status filter to avoid a sample dominated
by completed records. These two Middleware samples overlap by one record.

## Results

| Source / sample | Sampled | Processed | Excluded | Blocked |
| --- | ---: | ---: | ---: | ---: |
| IC Meeting Notes, unfiltered | 2 | 0 | 1 | 1 |
| Powertrain Tasks Tracker, unfiltered | 1 | 0 | 0 | 1 |
| Software Web Dashboard Meeting Notes, unfiltered | 4 | 4 | 0 | 0 |
| Software Middleware tasks, unfiltered | 5 | 1 | 4 | 0 |
| Software Middleware tasks, eligibility-filtered | 5 | 5 | 0 | 0 |
| **Distinct records, deduplicated** | **16** | **9** | **5** | **2** |

The 9 processed records produced 12 in-memory chunks, with a largest observed
chunk of 190 tokens (configured maximum: 700). They passed source UUID,
canonical page URL, source membership, section references, exact token counts,
and deterministic chunking checks. Repeated live reads returned the same source
versions and identical normalized documents/chunks.

All five excluded records had **zero Markdown requests**. This includes one
draft meeting note and four inactive task records. Filtering happened before
body retrieval. The eligible Middleware query successfully applied the same
schema-derived status filter used by batch ingestion, while retaining local
checks on fresh page metadata.

Middleware returned `has_more: true`; the diagnostic stopped at the requested
limit. Other sampled sources returned no further records. No full Middleware
crawl or complete-corpus reconciliation was performed.

## Property and content findings

- The real schemas mapped status, priority, descriptions, summaries, due dates,
  meeting dates, assignees, and attendees to the expected semantic fields.
- Nonempty assignee IDs/display names were preserved in one successful active
  task's property evidence. The reports contain counts, not names or addresses.
- Dates passed structural/ISO checks in query metadata from excluded records
  and from the blocked Powertrain task. No successfully chunked task in these
  samples had a nonempty due date. Live date-to-chunk preservation is therefore
  still unverified; synthetic tests cover it.
- Attendee fields were present in a real schema but empty in the sample. This
  does not validate nonempty attendee preservation or large people lists live.
- Six processed records had no extractable body and produced property-only
  evidence. Three meeting notes contained body content. Decide whether sparse
  status/priority-only records are useful when curating the first indexed corpus.
- Explicit source kinds were used for generic-title databases. Those tested
  meeting/task kinds are now saved in the ignored local source policies.

## Blocked content

1. The published IC meeting note requires attachment/media extraction. The
   current text-only normalization path rejected it instead of treating its
   short overview and properties as a complete document.
2. The active Powertrain task returned two unresolved Markdown blocks. The
   connector rejected the record. No chunk was produced from its partial body.

Both records passed their sampled property-shape checks, but those checks are
separate from document extraction. No unsupported block was silently skipped,
and failure handling was not weakened to make the test pass. Attachment parsing
and controlled structured-block fallback remain separate implementation work.

## Reproduction and limits

From `backend/`, with the backend environment activated and the selected source
configured in `NOTION_ALLOWED_DATA_SOURCE_IDS`:

```bash
python scripts/check_notion_live.py --data-source-id APPROVED_DATA_SOURCE_UUID --limit 5 --repeat
python scripts/check_notion_live.py --data-source-id APPROVED_DATA_SOURCE_UUID --limit 5 --eligible-only --repeat
python -m pytest tests/unit tests/e2e
```

The diagnostic returns `passed` only when at least one sampled record processed
and none failed. A source with blocked records returns `needs_review` (exit 2),
even if other records were correctly excluded. Empty/all-excluded samples are
inconclusive. Per-record diagnostics do not change the production batch API's
failure semantics.

This test did not validate live long-prose/table splitting, following multiple
cursor pages, nonempty attendee lists, attachments, embeddings, persistence,
retrieval quality, answer faithfulness, or source revocation. These are not
claimed as passing live coverage.

Only counts, UUIDs, fixed validation results, and error categories were printed.
Real page text, titles, people names, email addresses, attachments, and API error
messages were not copied into reports or fixtures. No Notion writes, PostgreSQL
writes, embedding requests, or generation requests were made.

Local verification after the diagnostic changes: **134 unit/offline end-to-end
tests passed** on Python 3.12; targeted Ruff checks and `git diff --check` passed.
