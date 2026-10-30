# TTS Project — Imports Overview & Architecture Reference

This document explains every import used in the core application ([`backend/app.py`](file:///c:/Users/ASUS/Desktop/TTS/backend/app.py)) and the role of installed dependencies across the project.

---

## 1. Python Standard Library

| Module | What It Does in the Project |
| :--- | :--- |
| `os` | Environment variable reading (`YOUTUBE_API_KEY`, `DATABASE_URL`) and file path manipulation. |
| `re` | Regex operations for URL parsing (YouTube video IDs, playlist tokens, timestamps) and text normalization. |
| `io` | In-memory text/bytes streams (`io.StringIO`), used to dynamically generate CSV files for download without writing temp files to disk. |
| `csv` | Reading and writing CSV rows for batch import and export of audio records. |
| `json` | Serialization/deserialization of audit metadata, duplicate matching results, and API payloads. |
| `logging` | Structured application logging, error capturing, and trace outputs. |
| `traceback` | Stack trace formatting in exception handlers to diagnose runtime errors. |
| `asyncio` | Event loop integration and running asynchronous tasks concurrently. |
| `requests` | Synchronous HTTP client for fetching YouTube oEmbed data, metadata APIs, and web scraping fallbacks. |
| `datetime` | Generating timestamps (`created_at`, `updated_at`, `research_date`) for tracking record history. |
| `typing` (`Optional`, `List`, `Dict`, `Any`) | Type annotations for function signatures, endpoint definitions, and Pydantic schema validation. |

---

## 2. Third-Party Frameworks & Libraries

| Import / Package | Source | Role in the Project |
| :--- | :--- | :--- |
| `dotenv.load_dotenv` | `python-dotenv` | Loads environment configurations from `.env` and `backend/.env` into `os.environ`. |
| `FastAPI` | `fastapi` | Primary web framework; routes endpoints, manages lifecycle, and auto-generates OpenAPI docs. |
| `HTTPException` | `fastapi` | Standardized HTTP error responses with status codes and descriptive error messages. |
| `Query` | `fastapi` | Query parameter extraction, validation, and default value configuration in GET endpoints. |
| `status` | `fastapi` | HTTP status code constants (`status.HTTP_200_OK`, `status.HTTP_400_BAD_REQUEST`, etc.). |
| `BackgroundTasks` | `fastapi` | Executes non-blocking operations (e.g., background audio pipeline runs) after sending the response. |
| `CORSMiddleware` | `fastapi.middleware.cors` | Configures Cross-Origin Resource Sharing so browser frontends can communicate with the API. |
| `Response`, `FileResponse` | `fastapi.responses` | Custom response handlers: `Response` for custom MIME types (CSV text streams), `FileResponse` for serving downloaded audio files. |
| `StaticFiles` | `fastapi.staticfiles` | Mounts and serves frontend UI assets directly from the `frontend/` directory. |

---

## 3. Internal Project Modules

### [`backend.database`](file:///c:/Users/ASUS/Desktop/TTS/backend/database.py)
Database connection pool, schema initialization, and transactional queries for PostgreSQL/SQLite:
- `init_db`: Creates required tables (`audio_records`, `speaker_ledger`, `web_verifications`, etc.) on startup.
- `get_connection`: Context manager yielding active database connections with automatic commit/rollback.
- `format_duration`, `duration_to_seconds`: Bidirectional converters between `HH:MM:SS` duration strings and integer seconds.
- `upsert_web_verification`, `get_web_verifications`, `get_web_verification_by_id`, `get_all_stored_web_and_records`: Data access methods for the web verification cache and unified research tables.
- `flush_database`: Maintenance helper to wipe table data for development or reset runs.

### [`backend.duplicate`](file:///c:/Users/ASUS/Desktop/TTS/backend/duplicate.py)
Multi-layered duplicate detection engine for audio metadata and URLs:
- `canonicalize_url`: Normalizes platform links (standardizing YouTube URLs, stripping query tracking parameters, etc.).
- `extract_platform_and_item_id`: Extracts platform origin (`YouTube`, `Archive.org`, `Podcast`) and unique content identifier.
- `fast_prefilter`: Rapid early-exit checks to filter out non-duplicate candidates before heavy similarity calculations.
- `check_similarity`: Calculates lexical and fuzzy similarity (via RapidFuzz / TF-IDF) between titles, speaker names, and descriptions.

### [`backend.speaker_ledger`](file:///c:/Users/ASUS/Desktop/TTS/backend/speaker_ledger.py)
Enforces per-speaker quota constraints to ensure balanced TTS dataset representation:
- `MAX_SPEAKER_SECONDS`: Constant defining maximum permissible audio duration per speaker (e.g., 20 minutes = 1200 seconds).
- `validate_speaker_duration_cap`: Pre-check ensuring a candidate record's duration will not exceed the speaker limit.
- `record_accepted_duration_atomically`: Atomically credits approved audio seconds to the speaker's balance in the database.
- `remove_speaker_duration`: Decrements speaker balance if an approved record is rejected or removed during revalidation.
- `get_all_speaker_ledgers`: Returns list of all active speakers, accumulated duration, and quota status.
- `get_speaker_accepted_seconds`: Queries accumulated duration for a specific speaker.
- `format_seconds`: Converts raw integer seconds into formatted readable strings (`Xm Ys`).
- `sync_speaker_duration_ledger`: Re-computes and syncs ledger state against all currently `APPROVED` records.

### [`backend.fast_verifier`](file:///c:/Users/ASUS/Desktop/TTS/backend/fast_verifier.py)
Automated verification and metadata scraping:
- `execute_fast_verification`: Orchestrates URL scraping, title extraction, content validation, and licensing heuristics.
- `fetch_essential_content`: Scrapes minimal HTML/metadata from external source URLs without full-page overhead.

### [`backend.licensing`](file:///c:/Users/ASUS/Desktop/TTS/backend/licensing.py)
Copyright and license screening engine:
- `fetch_and_detect_youtube_licensing`: Queries YouTube Data API / page metadata to determine license type (Creative Commons vs. Standard).
- `analyze_licensing_evidence`: Analyzes description text, tags, and attribution markers against licensing policies.
- `ALL_CATEGORIES`, `SCREENING_POLICY`, `UNKNOWN`: Constants and policy maps classifying content eligibility for TTS dataset inclusion.

### [`backend.models`](file:///c:/Users/ASUS/Desktop/TTS/backend/models.py)
Pydantic schemas providing strict request/response data contracts:
- `DuplicateCheckRequest`, `DuplicateCheckResponse`: Payload structures for `/api/check-duplicate`.
- `RecordCreateRequest`, `RecordRevalidateRequest`, `RecordUpdateRequest`: Schemas for creating, updating, and reviewing research records.
- `CsvUploadRequest`: Schema for batch CSV ingestion.
- `YouTubeTestRequest`: Schema for ad-hoc YouTube licensing checks.
- `WebVerifyRequest`, `WebVerifyStoreRequest`: Schemas for URL web verification endpoints.

---

## 4. Other Key Dependencies in the Project

Dependencies declared in [`requirements.txt`](file:///c:/Users/ASUS/Desktop/TTS/requirements.txt) used across submodules:

- **`rapidfuzz`**: C++ accelerated fuzzy string matching used in [`backend/duplicate.py`](file:///c:/Users/ASUS/Desktop/TTS/backend/duplicate.py) for title and speaker similarity scoring.
- **`scikit-learn` & `numpy`**: Vector-based TF-IDF calculations for cosine similarity across long video descriptions and transcripts.
- **`beautifulsoup4`**: HTML parsing in [`backend/fast_verifier.py`](file:///c:/Users/ASUS/Desktop/TTS/backend/fast_verifier.py) to extract OpenGraph tags, title elements, and metadata.
- **`yt-dlp`**: Download engine in [`backend/audio_downloader.py`](file:///c:/Users/ASUS/Desktop/TTS/backend/audio_downloader.py) and [`backend/pipeline_service.py`](file:///c:/Users/ASUS/Desktop/TTS/backend/pipeline_service.py) for extracting and converting source audio into clean 16kHz WAV format.
- **`uvicorn`**: ASGI server executing the FastAPI application (`run.py`).
