# TTS Database — Implementation Status, End-to-End Workflow, and Async Roadmap

**Project:** `tts_database`  
**Database:** PostgreSQL (`tts_research`)  
**Document purpose:** Record what has been implemented so far, what remains, and the plan for introducing asynchronous database operations safely.  
**Status snapshot:** Based on the project details and test results reported up to 26 October 2026.

---

## 1. Scope and boundary

This project builds a standalone PostgreSQL data layer for the TTS Audio Research Hub. The existing TTS application is a reference for compatibility, not a target for modification.

- Keep the existing application's SQLite database and UI/backend unchanged.
- Do not migrate or overwrite its test/sample data.
- Reuse its existing duplicate-matching engine during future integration; do not create a second matching engine.
- Make schema changes through new migrations. Do not edit migrations that have already been applied.
- Integrate the PostgreSQL module into the application only when the owner chooses to do so.

## 2. What has been implemented

### 2.1 PostgreSQL foundation

- PostgreSQL database `tts_research` has been created.
- Python connection/configuration uses `psycopg`, `python-dotenv`, and `DATABASE_URL`.
- Schema migrations are tracked through `005_create_audit_logs.sql`.
- Initialization, verification, and restore scripts exist under `scripts/`.

### 2.2 Database tables

| Table | Purpose | Current status |
|---|---|---|
| `research_records` | Permanent records using the original 22 metadata fields | Implemented |
| `revalidation_queue` | Holds submitted candidates and their workflow/duplicate states | Implemented |
| `duplicate_cache` | Stores reusable duplicate-check results with expiry and rules version | Implemented |
| `audit_logs` | Records important actions and supporting JSON details | Implemented |

The permanent record table has internal `id`, `created_at`, and `updated_at` fields in addition to the metadata. The queue has queue status, duplicate status, evidence, approval reference, and timestamps. Cache expiry is independent from queue retention.

### 2.3 Permanent-record repository

`repositories/records.py` implements:

- `create_record`
- `get_record_by_id`
- `get_records`
- `update_record`
- `delete_record`
- Field validation through `METADATA_FIELDS` and `_validate_record_fields`

### 2.4 Revalidation queue

`repositories/revalidation_queue.py` implements:

- `submit_for_revalidation`
- `get_queue_record`
- `get_queue_records`
- `update_queue_status`
- `update_duplicate_status`
- `approve_queue_record`

Queue transitions are validated. Approval is not performed by directly setting the queue status: `approve_queue_record()` checks that the candidate is ready and its duplicate result is `unique`, then inserts the permanent record, updates the queue, and writes an audit event in one transaction.

Current queue statuses are `pending`, `ready_for_approval`, `approved`, and `rejected`. Duplicate outcomes are `not_checked`, `unique`, `duplicate`, `review_required`, and `check_failed`.

### 2.5 Duplicate-result cache and workflow wrapper

`repositories/duplicate_cache.py` implements:

- `save_cache_entry`
- `get_cache_entry`
- `delete_expired_cache_entries`

`repositories/duplicate_workflow.py` implements:

- `get_cached_duplicate_result`
- `record_duplicate_result`

The workflow records failures as `check_failed`; a failed check must never be treated as `unique`.

**Known follow-up items:**

1. `get_cached_duplicate_result()` should check that the cached `rules_version` matches the currently active version.
2. Queue-result update and cache save currently use separate transactions. Decide whether they must become atomic; if so, refactor through a single connection/transaction boundary.
3. Validate cache keys and rules-version inputs consistently at the service boundary.

### 2.6 CSV adapter

`csv_adapter.py` supports import/export for the original 22 CSV columns in their original order. It maps CSV headers to normalized database columns and converts durations:

- CSV `H:MM:SS` → integer seconds in PostgreSQL
- Integer seconds → CSV `H:MM:SS`
- `data_available` remains text, not a boolean

CSV import validates the file before inserting rows and uses a transaction, so invalid input or a failed insert should not leave a partial import.

### 2.7 Tests

The latest reported full-suite result was **54 passed**. This is a historical snapshot; run the full suite again after any code changes.

Test coverage includes connections, record CRUD, queue operations, audit logs, duplicate cache/workflow, CSV conversion, round-trip integration, invalid imports, and transaction rollback.

---

## 3. Original CSV fields supported

The database currently maps these 22 headers:

1. `Date` → `research_date`
2. `Completion_date` → `completion_date`
3. `Source` → `source`
4. `Audio_Link` → `audio_link`
5. `Text_Link` → `text_link`
6. `Description` → `description`
7. `Dataset Name` → `dataset_name`
8. `Gender` → `gender`
9. `Before_Processing_Duration` → `before_processing_duration`
10. `After_Processing_Duration` → `after_processing_duration`
11. `Total_files` → `total_files`
12. `Researched_By` → `researched_by`
13. `Assigned_to` → `assigned_to`
14. `Status` → `status`
15. `Data_type (Augmentation_Status)` → `data_type_augmentation_status`
16. `Data_available` → `data_available`
17. `Speakers_ID` → `speakers_id`
18. `Speaker/Channel_Name` → `speaker_channel_name`
19. `Accent` → `accent`
20. `Licensing` → `licensing`
21. `Handover` → `handover`
22. `Remark` → `remark`

Durations and `total_files` have non-negative constraints in PostgreSQL.

---

## 4. End-to-end target workflow

1. **Intake:** receive candidate metadata and validate field types and required values.
2. **Normalize identity:** canonicalize URLs and extract platform IDs using the existing application's logic. Preserve the original link as well.
3. **Queue:** create a durable `revalidation_queue` entry before processing the duplicate result.
4. **Check cache:** reuse only an unexpired result created under the active duplicate-rules version.
5. **Duplicate verification:** call the existing matching engine. Use exact identifiers first, then candidate matching; use LLM/web checks only when the existing policy requires them.
6. **Persist result:** store `unique`, `duplicate`, `review_required`, or `check_failed`, along with evidence. `not_checked` is only an initial state.
7. **Revalidation:** reviewer examines evidence, licensing, metadata, and the duplicate result. Duplicate status and approval status remain separate.
8. **Speaker-cap validation:** calculate accepted duration for the speaker and ensure the proposed approval stays within the existing 3.5-hour / 12,600-second limit.
9. **Atomic approval:** in one transaction, verify eligibility, insert the permanent record, update speaker ledger, update queue state/reference, and write audit events.
10. **Export:** export approved/permanent records to CSV with the exact original headers and duration formatting.
11. **Maintenance:** expire cache entries, back up PostgreSQL, test restores, review audit history, and monitor failed checks.

A shared speaker or shared topic alone does not prove a duplicate. A failed metadata fetch or unavailable model endpoint must not become a `unique` result.

---

## 5. What remains to be implemented

### Priority 1 — Compatibility with the existing application

The existing app contains fields and workflows beyond the 22-column CSV. Before integration, confirm the exact field contract from the source code and add the required fields through new migrations. Candidate fields identified during source review include canonical URL/platform ID, title/channel, duration in seconds, duplicate decision evidence, matched item IDs, and verification-related metadata. Do not add fields solely because they sound useful; confirm their actual callers and persistence requirements.

Also define an explicit mapping between the existing application's status values and the PostgreSQL queue states. Never silently conflate duplicate outcomes, licensing decisions, and human approval.

### Priority 2 — Speaker-duration ledger

Implement a PostgreSQL speaker ledger equivalent to the existing app's ledger. It must:

- Count only accepted/approved duration.
- Avoid counting the same record twice.
- Support the 12,600-second cap.
- Enforce the cap safely when two approvals happen concurrently.
- Update atomically with approval and audit history.

### Priority 3 — Verification and decision history

Compare existing `decision_audit`, `web_verifications`, and `audio_segments` requirements with the PostgreSQL schema. Add only the tables needed to preserve required behavior. Generic `audit_logs` may not capture every specialized field.

### Priority 4 — Transaction and cache hardening

- Check cache `rules_version` before reusing results.
- Decide whether queue result and cache writes should be atomic.
- Add tests for concurrent submissions and approvals, cache expiry, and failures at each transaction step.

### Priority 5 — Async database API

Add async entry points while retaining the synchronous API during migration. Convert a complete call chain at a time and test parity with the existing synchronous functions.

### Priority 6 — Operational readiness

- Connection pool lifecycle and limits.
- Backup/restore verification.
- Migration verification.
- Logging and error handling.
- Permission separation between research, review, approval, and maintenance operations.
- Performance tests for expected concurrent requests.

### Priority 7 — Future application integration

Only after the contract and tests are stable should the PostgreSQL module be connected to the TTS application. The integration should be an explicit adapter/configuration change, not an unreviewed replacement of the existing database.

---

## 6. Async functions: implementation approach

### 6.1 Important distinction

The current repository functions use synchronous `psycopg` connections. Adding an `async def` wrapper around a synchronous database call does **not** make the database I/O asynchronous; it still blocks the event loop.

Real async database access requires `psycopg.AsyncConnection` (or an async pool) and `await` for database operations.

Async improves throughput for concurrent I/O-bound requests; it does not automatically speed up individual SQL queries or CPU-heavy matching/audio processing.

### 6.2 Proposed additions

Keep the synchronous modules working initially and add separate modules:

```text
async_connection.py
repositories/async_records.py
repositories/async_revalidation_queue.py
repositories/async_duplicate_cache.py
repositories/async_audit_logs.py
tests/test_async_connection.py
tests/test_async_records.py
tests/test_async_revalidation_queue.py
tests/test_async_duplicate_cache.py
```

After callers and tests have migrated, decide whether to keep both APIs or make async the primary interface.

### 6.3 Minimal async connection example

```python
# async_connection.py
import os

from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.rows import dict_row

load_dotenv()


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    return url


async def get_async_connection() -> AsyncConnection:
    return await AsyncConnection.connect(
        get_database_url(),
        row_factory=dict_row,
    )
```

Example health check:

```python
from async_connection import get_async_connection


async def async_check_database() -> bool:
    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 AS ok")
            row = await cur.fetchone()
            return row["ok"] == 1
```

For production request traffic, use a bounded async connection pool with explicit startup and shutdown lifecycle. The exact setup depends on the installed `psycopg_pool` version.

### 6.4 Async repository rules

- Use `async def` for async repository methods.
- Await every database I/O operation (`execute`, `fetchone`, `fetchall`, etc.).
- Use `async with` for connection/cursor scopes.
- Keep SQL values parameterized.
- Only interpolate column names that have been validated against `METADATA_FIELDS`.
- Preserve transaction boundaries and rollback behavior.
- Do not call synchronous repository functions from an async request handler; that would block the event loop.
- Do not mix synchronous and asynchronous connections in one transaction.
- Keep async tests isolated and clean up inserted test rows.
- Test behavior parity against the synchronous API before switching callers.

### 6.5 Async approval must be a single transaction

The future async approval service should perform all approval-critical work in one transaction:

1. Lock/claim the queue candidate.
2. Validate queue and duplicate statuses.
3. Validate required licensing and metadata checks.
4. Lock the relevant speaker ledger row or use a documented advisory-lock strategy.
5. Recalculate accepted speaker duration under that lock.
6. Insert the permanent record.
7. Insert/update the speaker ledger.
8. Mark the queue record approved and set its permanent record ID.
9. Insert the audit event.
10. Commit.

Any failure must roll back all approval-critical writes.

### 6.6 Async acceptance criteria

Async implementation is ready when:

- Async connection and CRUD tests pass.
- Queue transitions match synchronous behavior.
- Failed operations roll back cleanly.
- Async approval is race-safe once the speaker ledger exists.
- No event-loop path calls blocking database functions.
- The original synchronous test suite still passes during migration.
- Tests run against a dedicated test database, not production records.

---

## 7. Recommended implementation order

| Order | Work item | Deliverable |
|---:|---|---|
| 1 | Verify existing app's persistence contract | Field/status mapping document |
| 2 | Add confirmed compatibility fields via new migration | Migration + schema tests |
| 3 | Implement speaker ledger and cap enforcement | Repository + concurrency tests |
| 4 | Add required verification history | Minimal tables + audit tests |
| 5 | Fix cache rules-version validation | Code + regression tests |
| 6 | Decide and implement atomic queue/cache result write | Transaction tests |
| 7 | Add async connection layer and async record CRUD | Async modules + tests |
| 8 | Add async queue/cache/audit operations | Async parity tests |
| 9 | Add atomic async approval service | Approval + concurrency tests |
| 10 | Re-run full suite and backup/restore checks | Verified test and operational checklist |
| 11 | Integrate only when explicitly approved | Adapter and integration tests |

## 8. Definition of done

The database module is ready for future integration when:

- Existing required fields and statuses have an explicit compatibility mapping.
- All migrations apply cleanly on a fresh database.
- Record, queue, cache, and audit operations have reliable tests.
- Speaker duration is enforced transactionally without double counting.
- Duplicate failures cannot be misreported as unique.
- Cache reuse respects expiry and rules version.
- CSV round-trip preserves all 22 headers, order, and duration values.
- Sync and async APIs have parity, or the project has deliberately selected one API.
- Backups can be restored successfully.
- The existing TTS application and its SQLite data remain untouched until the owner chooses to integrate them.
