# Box Integration Plan

> **Audience:** engineers and AI coding agents implementing the Box connector.
> **Status (2026-11-12):** B1–B3 test track done. Connector, auth, lightweight extraction, and live check implemented; live run on a personal Box account passed (see §16). OAuth login is blocked by the console (redirect URI won't save on a non-developer account), so the live run used a developer token.
> **Written:** 2026-11-12. Companion to `AGENTS.md` (which wins on any conflict).

## 1. Goal and non-goals

**Goal:** ingest explicitly approved Box files (PDF, DOCX, TXT/Markdown first) into the existing `NormalizedDocument` contract, so they flow through the same chunk → embed → persist → retrieve → cite path as Notion content, with accurate citations and no content from unapproved files.

**Non-goals for the first slices:** Box AI (`/ai/ask`) or Box's own retrieval, OCR/scanned PDFs, PPTX/spreadsheets, per-user Box OAuth, RBAC, webhooks/real-time sync, enterprise-wide crawling.

**Why not Box AI:** it would replace the pgvector retrieval decision in AGENTS.md §2, consumes a metered AI-unit quota, and removes control over citations and evaluation. We use plain metadata + content download only.

## 2. Current state (verified in repo, 2026-11-05)

| Item | State |
|---|---|
| `backend/app/connectors/box.py` | one-line docstring stub |
| `backend/app/connectors/base.py` | one-line docstring stub |
| Box settings in `config.py` / `.env.example` | none |
| `docling`, Box SDK in `pyproject.toml` | absent (`httpx==0.28.1`, `tiktoken==0.11.0` present) |
| Box fixture | `tests/fixtures/box/extracted_pdf.json` (already-normalized JSON only; no real PDF/DOCX bytes) |
| Template to mirror | `NotionConnector`: `from_settings`, allowlist guard, typed errors, retrying `_request`, `fetch_and_normalize_*` |

## 3. Key design decisions (proposed; confirm in §12)

| # | Topic | Proposal | Rationale |
|---|---|---|---|
| D1 | Auth | **Pluggable `BoxAuth`**. Test track: developer token, then **OAuth 2.0 user login**. Production track: **CCG** Service Account once an admin-authorized team app exists | User is not a Box admin (UTD SSO account). OAuth lets us prove pull → chunk now; the connector stays unchanged when auth switches. Box apps cannot switch auth method after creation, so the test and production apps are separate apps |
| D2 | HTTP layer | Raw `httpx` (matches Notion), not `box-sdk-gen` | Full control of the 302 redirect, header stripping, streaming, and retry semantics; no new heavy dependency. Revisit if token handling gets complicated |
| D3 | Citation URL | `https://app.box.com/file/{id}` | Stable and canonical. Never a signed download URL. Only useful to people with Box access (see Q2) |
| D4 | Parsing → contract | **Changed 2026-11-12:** Docling cannot install on the Intel macOS dev host (no PyTorch builds for Python 3.13), so extraction is lightweight: `pypdf` (per-page sections), `python-docx` (headings/lists/tables/links), MD/TXT direct → **existing token-aware chunker**. Docling deferred to a Linux/Docker parser behind the same `extract_file` contract, compared on the eval set. Original plan: Docling output → `DocumentSection` (heading path + page number) → existing chunker | One normalization contract and chunk-reference format for both sources. Compare with Docling `HybridChunker` later on the eval set |
| D5 | Change detection | `file_version.id` as `source_version`; Box `sha1` as content hash | Unchanged files cost zero embedding calls |
| D6 | Scope order | Single file IDs → approved folders → sync | AGENTS.md §2 |

## 4. Box platform setup (requires you; see §12 for what I need)

### 4.0 Test track: OAuth 2.0 user login (do this first)

Context: the user signs in to Box through UTD SSO and is not an enterprise admin. Goal: prove "pull approved documents → normalize → chunk" before a team/admin-authorized app exists.

**Environment options, in recommended order:**

| Option | What it is | Pros | Cons |
|---|---|---|---|
| **T1. Free Box developer account** (sign up at developer.box.com with a non-UTD email) | Your own Box enterprise where you are admin | Cannot be blocked by UTD settings; no institutional-data questions; upload realistic sample PDFs/DOCX | Not the real corpus |
| **T2. UTD Box account via Developer Console** | Create a custom app in UTD's Box enterprise and log in as yourself | Real files you can already see | UTD may have **"Disable unpublished apps by default"** on; then authorization fails ("Cannot authorize with this service") until UTD's Primary Admin enables the app (Developer Console → app → **Enablement** tab → submit). UTD data policy may restrict moving university content into external tools |

Recommendation: build and validate against **T1** (it always works), and try **T2** in parallel. If UTD blocks the app, submit it for enablement or ask UTD OIT. Don't wait on UTD to keep building.

**Steps (same for T1 and T2):**
1. Box Developer Console → Create Platform App → **Custom App** → **User Authentication (OAuth 2.0)**.
2. Scopes: **Read all files and folders** only.
3. Configuration → OAuth 2.0 Redirect URI: `http://localhost:8765/callback` (must match exactly; confirm Box accepts it for your app).
4. Stage 0 (fastest smoke test): click **Generate Developer Token**. It is valid for 60 minutes and cannot be refreshed. Put it in `BOX_DEVELOPER_TOKEN` and run the diagnostic.
5. Stage 1: run `scripts/box_oauth_login.py`, which opens the browser, you log in (UTD SSO for T2), and it stores the tokens in a git-ignored file.

**OAuth mechanics (verified in Box docs):** access tokens last about 60 minutes. Refresh tokens last 60 days, are **single-use**, and rotate on every refresh (the new one must be persisted before use). The authorization code expires after **30 seconds**, so exchange it immediately.

**Security difference to note:** in OAuth mode the backend acts as **you**, so it can read everything you can see in Box. The Service Account collaboration layer (§4 production track) does not exist here. The **`BOX_ALLOWED_FILE_IDS` allowlist is the only guard**, so it is enforced before any network call and tested. Tokens live only in `backend/.local/` (git-ignored, file mode 0600) and are never printed.

### 4.1 Production track: CCG Service Account (later, needs an admin)

1. Create a Box Platform custom app in the Box developer console: **Server Authentication (Client Credentials Grant)**.
2. Enable only the read scope ("Read all files and folders stored in Box"). No write, no manage-users.
3. An **enterprise admin must authorize** the app (Admin Console → Integrations → Platform Apps).
4. Note the **Service Account email** (shown in the app's General Settings). A fresh Service Account sees nothing.
5. From a Box account that owns the pilot content, **invite the Service Account email as a collaborator with Viewer (or Previewer + download) role** on exactly the files/folders you approve. This is a second allowlist layer: even a bug in our allowlist cannot read files the Service Account was never given.
6. Collect: client ID, client secret, enterprise ID, and the pilot file IDs (the number at the end of `app.box.com/file/<ID>`).

Secrets go only in the git-ignored **repository-root `.env`** (that is where `Settings` loads from; there is no `backend/.env`); `.env.example` gets placeholders.

## 5. Configuration (new settings)

Follow the `notion_*` pattern in `config.py` (`SecretStr` for secrets, `validation_alias`, list parsing).

| Env var | Type | Purpose |
|---|---|---|
| `BOX_AUTH_MODE` | `developer_token` \| `oauth` \| `ccg` | selects the `BoxAuth` implementation |
| `BOX_DEVELOPER_TOKEN` | SecretStr | stage-0 smoke test only (60 min, not refreshable) |
| `BOX_CLIENT_ID` | str | app client ID (oauth, ccg) |
| `BOX_CLIENT_SECRET` | SecretStr | app client secret (oauth, ccg) |
| `BOX_OAUTH_REDIRECT_URI` | str | default `http://localhost:8765/callback` |
| `BOX_OAUTH_TOKEN_PATH` | path | default `backend/.local/box_oauth_tokens.json` (git-ignored, 0600) |
| `BOX_ENTERPRISE_ID` | str | CCG subject (`box_subject_type=enterprise`); production track only |
| `BOX_ACCOUNT_LABEL` | str | required; identifies the source account (e.g. `personal-dev`, `utd`), recorded on every document (UTD policy condition) |
| `BOX_ALLOWED_FILE_IDS` | list[str] | the allowlist for slice 1 |
| `BOX_ALLOWED_FOLDER_IDS` | list[str] | slice 2 only; empty by default |
| `BOX_MAX_FILE_BYTES` | int | download cap (propose 25 MiB) |
| `BOX_ALLOWED_EXTENSIONS` | list[str] | default `pdf,docx,txt,md` |
| `BOX_REQUEST_TIMEOUT_SECONDS` | float | per-request timeout |
| `BOX_API_BASE_URL` | str | default `https://api.box.com/2.0` (overridable for tests) |

Settings load must not fail when Box is unconfigured (Notion-only deployments must keep working).

## 6. Connector design (`app/connectors/box.py`)

### 6.1 Types
- Errors (mirror Notion): `BoxConnectorError` base; `BoxSourceNotAllowedError`; `BoxAPIError(status_code, code, message)`; `BoxTransportError`; `BoxResponseError`; `BoxAuthError`; `BoxFileExcludedError(file_id, reason)` (trashed, wrong extension, too large, unsupported content); `BoxIncompleteContentError` (extraction produced nothing or partial).
- `BoxFileMetadata` frozen dataclass: id, name, size, sha1, etag, version_id, modified_at, trashed_at, item_status, parent_id, extension.

### 6.2 `BoxConnector`
- `from_settings(settings)`, `__aenter__/__aexit__/aclose`, injectable `httpx.AsyncClient`/transport for tests.
- `_require_allowed_file(file_id)`: canonicalize (digits only), check allowlist **before any network call**. Raise `BoxSourceNotAllowedError` otherwise.
- Auth via an injected `BoxAuth` protocol (`async get_access_token() -> str`, `async invalidate()`), in `app/connectors/box_auth.py`:
  - `DeveloperTokenAuth`: returns the configured token; on 401 it raises `BoxAuthError("developer token expired")` with no refresh.
  - `OAuthUserAuth`: loads tokens from `BOX_OAUTH_TOKEN_PATH`; refreshes via `POST https://api.box.com/oauth2/token` (`grant_type=refresh_token`) when expired or on 401. It **atomically writes the rotated refresh token** (temp file + rename, mode 0600) before returning the new access token, and uses an `asyncio.Lock` so concurrent calls don't spend the single-use refresh token twice. If the refresh token is expired or invalid, it raises `BoxAuthError` telling the user to rerun the login script.
  - `CCGAuth` (production track): `grant_type=client_credentials`, `box_subject_type=enterprise`, `box_subject_id=<enterprise id>`; cache until expiry.
  - Connector rule: on 401, call `invalidate()` once and retry the request a single time. Never log tokens or secrets, and keep them out of exception messages.
- `scripts/box_oauth_login.py` (test track): builds the authorize URL (`https://account.box.com/api/oauth2/authorize?response_type=code&client_id=…&redirect_uri=…&state=<random>`), starts a one-shot `127.0.0.1:8765` listener, verifies `state`, exchanges the code immediately (30-second lifetime), writes the token file, and prints only "authorized as user id …".
- `_request()`: retry on 429 (honor `Retry-After`), 5xx, and transport errors with capped exponential backoff, same shape as Notion's `_request`.
- `fetch_box_metadata(file_id) -> BoxFileMetadata`: `GET /files/{id}?fields=name,size,sha1,etag,file_version,modified_at,trashed_at,item_status,parent,extension` (verify exact field list against the live reference).
- `download_box_file(file_id, max_bytes) -> AsyncIterator`/temp-file context manager:
  1. `GET /files/{id}/content` with auth, `follow_redirects=False`.
  2. `202` + `Retry-After` → wait and retry (bounded attempts).
  3. `302` → follow `Location` **without** the `Authorization` header; stream to a `tempfile` in a private directory; abort and delete when bytes exceed the cap (also check `size` from metadata up front).
  4. Verify downloaded SHA-1 equals metadata `sha1` (detects a version change mid-download). Mismatch → retry once, then error.
  5. Always delete the temp file in `finally`.
- `fetch_and_normalize_file(file_id) -> NormalizedDocument`: allowlist → metadata → exclusion checks (trashed/inactive, extension, size) → download → parse (§7) → build `NormalizedDocument`.

### 6.3 Metadata → `NormalizedDocument`

| Field | Value |
|---|---|
| `source` | `"box"` |
| `external_id` | Box file ID (string of digits) |
| `title` | file name |
| `source_url` | `https://app.box.com/file/{id}` |
| `source_version` | `file_version.id` |
| `modified_at` | Box `modified_at` (UTC) |
| `metadata` | `box_account` (from `BOX_ACCOUNT_LABEL`), `filename`, `mime_type`/extension, `size_bytes`, `sha1`, `etag`, `parent_folder_id`, `parser` + version, `warnings[]`, `lifecycle_status` |

The existing fixture's `source_version: "7"` is a placeholder; real values are version IDs, not small counters. Update the fixture note accordingly.

## 7. Parsing (`app/ingestion/box_extract.py`, new)

- **TXT/MD:** decode UTF-8 (fail explicitly on undecodable bytes), split by Markdown headings via the same logic Notion uses where reusable.
- **PDF/DOCX:** Docling `DocumentConverter` → iterate document items, group consecutive items under their nearest heading into `DocumentSection(text, heading_path, page_number)`. Tables serialized so the header row travels with the rows (AGENTS.md §7). Exact Docling API for headings/page provenance is **not yet verified**; confirm against the pinned version in checkpoint B2.
- Run Docling in a worker thread/process (CPU-bound, synchronous) with a timeout; do not block the event loop.
- OCR disabled. If a PDF yields no or negligible text (scan), raise `BoxIncompleteContentError`; do not ingest a partial or empty document silently. Embedded images/unsupported items produce an extraction warning or an explicit exclusion, mirroring Notion's attachment rule.
- Pin `docling` after testing; note the model-download side effect and image size impact in the README. Offline CI must not download models, so unit tests use a parser interface with a fake implementation, and one marked integration test exercises real Docling.

## 8. Chunking

Reuse the existing Notion chunker unchanged where possible. Needs: page number preserved as the section reference (citation shows "p. N"); title and heading context prepended for embedding text as Notion does; deterministic output; oversized structured units fail explicitly. If the chunker has Notion-specific assumptions, extract a source-neutral core rather than forking.

## 9. Lifecycle, errors, and revocation

| Condition | Behavior |
|---|---|
| File ID not in allowlist | `BoxSourceNotAllowedError`, zero network calls |
| 401 | refresh token once; then `BoxAuthError`; do not delete data |
| 403 | access lost: mark document inactive/excluded so retrieval stops serving it (conservative, per AGENTS.md §7); never retried as success |
| 404 | tombstone the document (deleted/unshared), only from a direct per-file read, never from sampling |
| Trashed / non-active | exclude, no content download |
| 429 / 5xx / network | retry with backoff; on exhaustion fail the run **without** touching healthy indexed data |
| Size over cap / extension not allowed | `BoxFileExcludedError`, recorded with reason |
| SHA-1 mismatch | retry once, then fail |

Retrieval-side lifecycle filtering (active + approved) is shared with Notion and is not Box-specific.

## 10. Later phases

### Folders (after single-file works)
- `GET /folders/{id}/items?usemarker=true&limit=1000&fields=…`, follow `next_marker` until null; never persist markers across runs.
- Traversal depth limit, extension allowlist, size cap, and **explicit per-folder inclusion policy**. Whether files added later are auto-included is a governance decision (Q4); default is **no auto-include** (new files require approval).
- Only files directly listed under an approved folder (or within the depth limit) are eligible; a folder approval does not extend to unrelated shared content.
- Sampling/partial listings never drive deletion; deletion reconciliation requires a complete, successful traversal.

### Sync (Phase F)
1. Manual reconcile: list, compare `sha1`/`file_version.id`, skip unchanged, atomic swap on change.
2. Scheduled polling.
3. Optional: V2 webhooks (folder-level `FILE.*` triggers, signature verification, dedupe by event id) with Events API + stored cursor as a backstop. Do not start before the manual sync works.

## 11. Testing and checkpoints

### Test matrix
| Layer | Cases |
|---|---|
| Connector unit (`httpx.MockTransport`) | metadata mapping; 302 download with auth header stripped; 202 retry; 401 refresh-once; 403; 404; 429 with `Retry-After`; 5xx; malformed JSON; size cap abort mid-stream; SHA-1 mismatch; temp file deleted on every path; allowlist rejects with zero requests; token never in logs/errors |
| Parser unit | tiny synthetic TXT/MD/DOCX/PDF checked into `tests/fixtures/box/`; headings and page numbers; table header retention; empty/scanned PDF → explicit failure; hostile text preserved as data |
| Offline end-to-end | mocked Box → normalized doc → chunks; deterministic across runs; matches fixture shape |
| Security | unapproved ID, trashed file, over-size file, prompt-injection text in a document |
| Live (opt-in, skipped without creds) | single approved file, repeated-read determinism, content-free diagnostic |

### Checkpoints (each ends with a pause for your review)

| CP | Deliverable | Exit criteria |
|---|---|---|
| **B0** | You: T1 developer account (and optionally a T2 UTD attempt), OAuth custom app, 2–3 sample files uploaded; client ID/secret in `.env` | app exists; developer token generated |
| **B1** | Settings + `BoxAuth` (developer token, OAuth) + connector core + metadata/download + typed errors, all mocked; `box_oauth_login.py` | connector unit matrix green; refresh-token rotation and lock tested; allowlist-before-network proven |
| **B2** | Parsers + Docling adapter + source-neutral chunker; synthetic fixtures | parser/chunk tests green offline; Docling provenance API confirmed |
| **B3 (test track)** | `scripts/check_box_live.py` + `export_box_chunks.py` (output to git-ignored `backend/.local/`) against T1 via OAuth | live file → N sections/chunks with correct canonical URL/version/sha1; identical output on repeated reads; refresh survives an expired access token; **this is the "can I pull and chunk Box docs" answer** |
| **B3b** | Same diagnostic against UTD (T2) if UTD enables the app | same criteria on 1–3 explicitly chosen real files |
| **B3c (production track)** | Team CCG app with Service Account, once an admin is available | same criteria; Service Account sees only collaborated files; docs note added to AGENTS.md progress section |
| **B4** | Folder traversal (approved folder, marker pagination, policy) | mocked pagination tests; bounded live sample; no deletion from partial listings |
| **B5** | Join the vertical slice: embed → atomic persist → retrieve → `/ask` with Box + Notion | Phase B/C exit checks in AGENTS.md §10 for a Box PDF |

Report format each time (per AGENTS.md §13): tests run, documents/chunks produced, limitations, next smallest step.

## 12. Open decisions and what I need from you

### Decided (2026-11-12)

| Question | Answer | Effect on plan |
|---|---|---|
| Box admin? | No. User signs in to UTD Box via SSO and is not an admin | Test track first: OAuth user login (§4.0); CCG deferred to production track |
| Users have Box access to cited files? | Yes | Citations use plain `https://app.box.com/file/{id}`; no shared links needed |
| New files in approved folders | Require explicit approval | No auto-include; folder listing only proposes candidates |
| External embedding/LLM provider | Approved | B5 may embed real Box content once the slice is reached |

Auth method for the production track: CCG by default (no answer needed until an admin is involved).

| Test environment | T1 free developer account first, then T2 (UTD) | Build and validate on T1; repeat B3 on UTD |
| UTD data policy | Allowed, provided the source account is identified; the user's own UTD documents are fine | Every Box document records which account it came from (`BOX_ACCOUNT_LABEL` → `metadata.box_account`) |
| Production home of team Box content | Deferred | Focus on the end-to-end slice |

### Pilot file set (chosen 2026-11-12)

Selection method: a read-only, content-free scan by the backend integration of ~2,000 visible F1 Notion pages, looking for real file attachments plus structurally rich pages. Only titles, sizes, heading/table counts, and attachment file names were inspected, not file contents, so text extractability is confirmed in B3.

Why attachments: the Notion connector deliberately rejects attachment content, so Box is the natural home for file-based knowledge. Notion handles pages and Box handles files.

| # | File | Where in Notion | Tests | Priority |
|---|---|---|---|---|
| 1 | `EV_Rules_Only.pdf` | "FSAE EV 2025 Ruleset" page (attachment) | long text PDF, numbered rule headings, page-number citations, multi-chunk splitting; dated 2025 ruleset → "outdated source" eval case | core |
| 2 | `2026_Business_Sponsorship_Proposal_Request_Scoring_Rubric.pdf` | "Business Case Competition IC/EV" page (attachment) | tables, header-row retention, short document | core |
| 3 | `IPCC Design.docx` | export "IPCC Design" page (6 headings, ~550 tokens) → convert to DOCX | DOCX path, heading paths | core |
| 4 | `CANFD Standard.md` | export "CANFD Standard" page (Markdown, 4 headings, table, one image) | TXT/MD path, Markdown tables, incomplete-content handling | extended |
| 5 | `Summer_25.pdf` | "2025 Scope Meeting Notes" (attachment) | **negative (verified)**: 1 page, 3.5 MB, **zero extractable text** (image-only/scanned) → must raise `BoxIncompleteContentError`; future OCR test case | edge |
| 6 | `2026BMS_HardwareDiagram.drawio.pdf` | "Stage 1 Hardware Block Diagram" (attachment) | edge: 1-page diagram with ~1,000 chars of disconnected label text → should extract with a low-structure warning, or be excluded by policy; never presented as prose | edge |
| 7 | `2026_Excellence_in_Technical_Innovation_Award_Submission.pptx` | "GM Grant" page (attachment) | **negative**: unsupported extension → `BoxFileExcludedError` before download | edge |

**Downloaded 2026-11-12** to git-ignored `backend/.local/box-pilot/` (raw Notion exports kept in `notion-raw/`). Verified with pypdf/pandoc: `EV_Rules_Only.pdf` has 22 pages and ~48k text chars; the rubric PDF has 1 page and ~1.8k chars; `IPCC Design.docx` has 7 headings and 32 paragraphs; `CANFD Standard.md` has 5 headings and 3 GFM tables.

Parser lesson for TXT/MD files: Notion's Markdown export separates blocks with single newlines (no blank line before headings) and uses Notion-specific `<table>`/`<empty-block/>` tags. The pilot `.md` files were converted to standard GFM. The Box MD parser should still tolerate headings that have no preceding blank line, because real uploaded files may look like this.

Deliberately excluded: *The C Programming Language* PDF (copyrighted book, not team knowledge), sponsorship letters/inquiries (contact details, low Q&A value), third-party theses, and duplicate attachments.

Duplication note: files 3–4 are Notion pages re-exported for format coverage. That is fine in the test database. In the real corpus each document must come from one source only, or retrieval will return duplicates.

How to prepare: download attachments from each Notion page (open the file block → Download). For 3–4: page `•••` → Export → Markdown; convert 3 to DOCX (`pandoc "IPCC Design.md" -o "IPCC Design.docx"`, or open in Word/Google Docs and save as .docx). Upload all to one folder (e.g., `F1 RAG Pilot`) in the free developer Box account, then copy each file ID from `app.box.com/file/<ID>`.

### Still open
1. **Production home of the team's Box content** (UTD enterprise vs separate team account): deferred by the user; decides who authorizes the CCG app.

### What I need from you (put values directly in the repository-root `.env`; never paste secrets in chat)
- Test track: `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET` from the OAuth custom app; the redirect URI set to `http://localhost:8765/callback`; optionally a fresh `BOX_DEVELOPER_TOKEN` for the stage-0 smoke test.
- Pilot file IDs → `BOX_ALLOWED_FILE_IDS`.
- Production track (later): CCG app client ID/secret, `BOX_ENTERPRISE_ID`, and confirmation that the Service Account email is a collaborator on the approved files.

## 13. Risks

| Risk | Mitigation |
|---|---|
| Admin authorization delays | do B1–B2 fully offline first; validate live on a free developer account (T1) |
| UTD blocks unpublished apps | submit via the Enablement tab or OIT; T1 keeps work unblocked |
| OAuth mode reads everything the user can see | allowlist checked before any network call, with tests; tokens only in `backend/.local/` at mode 0600 |
| Single-use refresh token lost (crash mid-rotation, concurrent refresh) | atomic write before use; asyncio lock; clear "rerun login" error |
| Docling weight (models, CPU, install size) | interface + fake in CI; worker process; pin version; measure |
| Docling output quality on real PDFs (tables, columns) | inspect real chunks locally in git-ignored `backend/.local/`; add eval questions |
| Scanned PDFs common in the real corpus | explicit exclusion now; OCR as later follow-up, gauged by how many files hit it |
| Box link citations unusable by end users | Q2; fallback is shared links with a governance review |
| Token/secret leakage via logs or exceptions | redact in error types; test that errors never contain secrets |
| Version changes mid-download | SHA-1 verification |

## 14. Unverified assumptions (confirm during implementation)

- Exact CCG token request parameters and token lifetime (check the live auth reference).
- Whether Box accepts the `http://localhost:8765/callback` redirect for your app (expected; test in the console).
- Whether UTD's enterprise has "Disable unpublished apps by default" on (only testable by trying, or by asking OIT).
- Exact `fields=` names for the file object, and whether `sha1` is always present for all file types.
- Docling API for heading paths and page provenance on the pinned version.
- Box rate limits for the app's service account (the "~1000 requests/min/user" figure came from a secondary page).
- Webhook details (root-folder restriction, retry/duplicate behavior) came from third-party sources.

## 16. Live results (2026-11-12, test track)

Account: personal Box (Enterprise ID 0), `BOX_ACCOUNT_LABEL` recorded on every document. Auth: developer token (OAuth redirect URI could not be saved in this console; fix by using a real Box developer account). Command: `BOX_AUTH_MODE=developer_token python scripts/check_box_live.py --export`.

| File | Outcome | Sections → chunks | Notes |
|---|---|---|---|
| `EV_Rules_Only.pdf` | processed | 22 pages → 26 chunks (44–661 tokens) | all 22 pages referenced; 275 distinct `EV.x.y` rule IDs preserved; no escape/hyphenation artifacts |
| Scoring Rubric PDF | processed | 1 → 1 (385 tokens) | table text flattened to lines (pypdf limitation) |
| `IPCC Design.docx` | processed | 6 → 6 | after hyperlink fix, word counts match the `.md` exactly per section |
| `IPCC Design.md` | processed | 6 → 6 | heading paths preserved |
| `2026BMS_HardwareDiagram.drawio.pdf` | processed | 1 → 1 (417 tokens) | label text only; low retrieval value, candidate for policy exclusion |
| `CANFD Standard.md` | **rejected** | — | contains an embedded image; conservative media rule (same as Notion) |
| `Summer_25.pdf` | **rejected** | — | image-only scan; OCR not enabled |
| `.pptx` award submission | **excluded** | — | extension not allowed; content never requested |

All processed files: canonical `app.box.com/file/{id}` URL, `source_version` = Box version ID, SHA-1 verified, identical chunk hashes on repeated reads, zero extraction warnings. 183 unit/e2e tests pass (46 new Box tests). Not run: embeddings, database writes, `/ask`.

**Next smallest steps:** (1) create a Box developer account so OAuth (refreshable) replaces the 60-minute token; (2) the vertical slice: embed → atomic persist → retrieve → `/ask` with Notion and Box documents; (3) later, Docling (Linux) for PDF table/heading structure, compared on the eval set.

## 15. References

- Authentication methods: https://developer.box.com/platform/authentication-methods/
- OAuth 2.0 setup: https://developer.box.com/guides/authentication/oauth2/oauth2-setup
- Refresh tokens: https://developer.box.com/guides/authentication/tokens/refresh
- Token and URL expiration: https://developer.box.com/guides/api-calls/permissions-and-errors/expiration
- Platform app approval / enablement: https://developer.box.com/guides/authorization/platform-app-approval
- "Cannot authorize with this service" troubleshooting: https://support.box.com/hc/en-us/articles/360044193313-API-Content-Unauthorized-Cannot-authorize-with-this-service
- Download file: https://developer.box.com/guides/downloads/file
- Get file information: https://developer.box.com/reference/get-files-id
- Marker-based pagination: https://developer.box.com/guides/api-calls/pagination/marker-based
- Rate limits: https://developer.box.com/guides/api-calls/permissions-and-errors/rate-limits
- Webhook triggers: https://developer.box.com/guides/webhooks/triggers.md
- Docling chunking: https://docling-project.github.io/docling/concepts/chunking/
