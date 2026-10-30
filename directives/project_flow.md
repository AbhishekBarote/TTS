# TTS Project — Code Flow

## File Structure (Production)

```
TTS/
├── run.py                        # Entry point: uvicorn on 0.0.0.0:8088
├── .env                          # YOUTUBE_API_KEY, DATABASE_URL
├── requirements.txt              # Python dependencies
├── AGENTS.md                     # AI agent instructions
│
├── backend/                      # All server-side logic
│   ├── app.py                    # FastAPI routes (1798 lines)
│   ├── models.py                 # Pydantic request/response schemas
│   ├── database.py               # PostgreSQL connection, schema, queries
│   ├── duplicate.py              # URL canonicalization + similarity prefilter
│   ├── fast_verifier.py          # URL fetch + SHA-256 dedup + speaker detection
│   ├── licensing.py              # YouTube + web license detection engine
│   ├── speaker_ledger.py         # 3.5h per-speaker duration cap enforcement
│   ├── pipeline_service.py       # Audio download → ASR server → segment DB
│   └── audio_downloader.py       # yt-dlp wrapper + ffmpeg conversion
│
├── static/                       # Frontend (served at /)
│   ├── index.html                # Single-page app shell
│   ├── app.js                    # All UI logic (tabs, modals, API calls)
│   ├── style.css                 # Styling
│   └── media/segments/           # Pipeline output audio (gitignored)
│
└── directives/                   # Documentation
    └── README.md
```

## Module Dependency Graph

```
  run.py
    └── app.py
          ├── models.py
          ├── database.py ──────→ duplicate.py
          ├── duplicate.py
          ├── fast_verifier.py ──→ duplicate.py, database.py
          ├── licensing.py
          ├── speaker_ledger.py
          └── pipeline_service.py → database.py, audio_downloader.py
```

No circular dependencies. `duplicate.py` is the leaf — no internal imports.

## Startup Flow

```
python run.py
  └─ uvicorn loads backend.app:app
       ├─ .env loaded (dotenv)
       ├─ database.init_db()          → creates PostgreSQL tables if missing
       ├─ pipeline_service.ensure_audio_segments_table()
       ├─ static/ mounted at /static
       └─ GET / serves static/index.html
```

## Core Workflows

### 1. Research Tab — New Record Submission

```
Browser                        app.py                     backend modules
───────                        ──────                     ───────────────
User pastes URL ──────────→ POST /api/check-duplicate
                               ├─ fast_verifier.execute_fast_verification()
                               │    ├─ validate URL, canonicalize
                               │    ├─ fetch page content (SHA-256 hash)
                               │    ├─ check exact match in DB (hash + canonical URL)
                               │    └─ fast_prefilter (title/channel/duration similarity)
                               │
                               ├─ licensing.fetch_and_detect_youtube_licensing()
                               │    ├─ YouTube Data API v3 (if key configured)
                               │    ├─ oEmbed + watch page scrape (fallback)
                               │    └─ analyze_licensing_evidence() → CC/Standard/Unknown
                               │
                               └─ speaker_ledger.validate_speaker_duration_cap()
                                    └─ check speaker total < 12,600s (3.5h)

                            Returns: DuplicateCheckResponse
                              { status, metadata, licensing_info, speaker_duration_check }

User fills form, clicks
"Send for Review" ────────→ POST /api/records
                               ├─ Exact duplicate check (canonical_url in DB)
                               ├─ Duration cap validation
                               ├─ INSERT INTO records (status=PENDING_REVALIDATION)
                               ├─ INSERT INTO audit_logs
                               └─ sync_speaker_duration_ledger()
```

### 2. Revalidation Queue — Review & Approve/Reject

```
Browser                        app.py                     backend modules
───────                        ──────                     ───────────────
Reviewer opens queue ─────→ GET /api/records?status=PENDING_REVALIDATION

Reviewer inspects record,
edits fields, clicks
"Approve" ────────────────→ POST /api/records/{id}/revalidate
                               ├─ Load existing record
                               ├─ Resolve all editable fields (audio_link, speakers_id,
                               │   gender, accent, duration, licensing, text_link,
                               │   dataset_name, description)
                               ├─ If APPROVE:
                               │    ├─ validate_speaker_duration_cap() (3.5h limit)
                               │    ├─ record_accepted_duration_atomically()
                               │    └─ status → APPROVED
                               ├─ If REJECT:
                               │    ├─ remove_speaker_duration()
                               │    └─ status → REJECTED_AT_REVALIDATION
                               ├─ If SAVE_ONLY:
                               │    └─ saves edits, keeps current status
                               ├─ UPDATE records SET ...
                               ├─ INSERT INTO audit_logs
                               └─ sync_speaker_duration_ledger()
```

### 3. Audio Pipeline — Download + Process + Validate

```
Browser                        app.py                     backend modules
───────                        ──────                     ───────────────
Click "Run Pipeline" ─────→ POST /api/pipeline/run/{id}
                               ├─ Seed pipeline_jobs row (DOWNLOADING)
                               ├─ Update record status → PIPELINE_PROCESSING
                               └─ BackgroundTask: run_full_pipeline()

                           pipeline_service.py (async background):
                               ├─ Step 1: audio_downloader.download_audio_yt_dlp()
                               │    ├─ yt-dlp download → raw audio file
                               │    └─ ffmpeg convert → 16kHz mono WAV
                               │
                               ├─ Step 2-3: POST WAV to processing server
                               │    └─ http://111.118.177.236:8008/run-pipeline/stream (SSE)
                               │
                               ├─ Step 4: Download + unzip results
                               │    └─ static/media/segments/{record_id}/
                               │
                               ├─ Step 5: Parse metadata CSV, seed audio_segments table
                               │
                               └─ Step 6: record status → VALIDATION_READY

Poll progress ────────────→ GET /api/pipeline/status/{id}
                               └─ Returns { stage, progress_pct, step_name, error }

View segments ─────────────→ GET /api/records/{id}/segments
Validate segment ──────────→ POST /api/segments/{id}/validate
Finalize ──────────────────→ POST /api/records/{id}/finalize-validation
                               └─ Counts accepted segments, sets total_files,
                                  after_processing_duration, status → COMPLETED
```

### 4. CSV Import / Export

```
Import:   POST /api/upload-csv
            └─ Every row → INSERT as APPROVED + speaker ledger update

Export:   GET /api/export/csv
            └─ 22-column CSV of all APPROVED records (RFC 4180, BOM-prefixed)

Preview:  GET /api/export/preview
            └─ Same data as JSON + CSV text
```

### 5. Supporting APIs

| Endpoint | Purpose |
|----------|---------|
| `GET /api/stats` | Dashboard metrics (counts, hours, recent submissions) |
| `GET /api/speakers/durations` | All speakers' accepted duration vs 3.5h cap |
| `GET /api/speakers/{id}/duration` | Single speaker detail + approved records list |
| `GET /api/next-speaker-id` | Next sequential SPK_NNN |
| `PUT /api/records/{id}` | Direct field edit (admin use) |
| `POST /api/admin/flush-database` | Wipe all data (dev/testing) |
| `GET/POST /api/youtube/test` | Verify YouTube API key |
| `GET/POST /api/detect-licensing` | Standalone license check |
| `GET /api/fetch-metadata` | Standalone metadata fetch |
| `POST /api/web-verify/check` | Web page duplicate check |
| `POST /api/web-verify/store` | Store web verification result |
| `GET /api/web-verify/records` | List stored web verifications |

## Database Tables

| Table | Purpose |
|-------|---------|
| `records` | All audio research records (35+ columns) |
| `audit_logs` | Every status change with actor, reason, timestamp |
| `speaker_duration_ledger` | Per-speaker accepted duration tracking |
| `web_verifications` | Stored web page duplicate checks |
| `pipeline_jobs` | Audio pipeline job state (persistent) |
| `audio_segments` | Individual segments from pipeline output |

## Record Status State Machine

```
                    ┌──────────────────┐
                    │ PENDING_RESEARCH │  (draft saved)
                    └────────┬─────────┘
                             │ SEND_FOR_REVIEW
                             ▼
              ┌──────────────────────────────┐
              │    PENDING_REVALIDATION      │
              └──────┬───────────┬───────────┘
                     │           │
              APPROVE│           │REJECT
                     ▼           ▼
              ┌──────────┐  ┌───────────────────────┐
              │ APPROVED │  │ REJECTED_AT_REVALIDATION│
              └────┬─────┘  └───────────────────────┘
                   │ Run Pipeline
                   ▼
          ┌─────────────────────┐
          │ PIPELINE_PROCESSING │
          └────┬────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
  ┌──────────────┐ ┌──────────────┐
  │PIPELINE_FAILED│ │VALIDATION_READY│
  └──────────────┘ └──────┬───────┘
                          │ Finalize
                          ▼
                   ┌───────────┐
                   │ COMPLETED │
                   └───────────┘
```

## Frontend (static/)

Single-page app with tab navigation:

| Tab | Key Functions in app.js |
|-----|------------------------|
| Dashboard | `loadStats()` → `GET /api/stats` |
| Research | `runDuplicateCheck()` → `POST /api/check-duplicate`, `submitRecord()` → `POST /api/records` |
| Revalidation Queue | `loadRevalidationQueue()` → `GET /api/records?status=PENDING_REVALIDATION`, `openInspectModal()`, `submitRevalidation()` → `POST /api/records/{id}/revalidate` |
| Records | `loadRecords()` → `GET /api/records` |
| Track Pipeline | `loadPipelineJobs()` → `GET /api/pipeline/jobs`, `triggerPipeline()` → `POST /api/pipeline/run/{id}` |
| Segment Validation | `loadSegments()` → `GET /api/records/{id}/segments` |
| CSV Import/Export | `uploadCsv()` → `POST /api/upload-csv`, `exportCsv()` → `GET /api/export/csv` |
