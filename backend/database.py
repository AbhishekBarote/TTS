"""
PostgreSQL Database manager for Centralized Audio Research and Revalidation Workflow.
Uses PostgreSQL (psycopg2) and provides transparent
query compatibility, strict uniqueness constraints, per-speaker duration tracking ledger,
and full duplicate decision audit logs.
"""

import os
import re
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

try:
    from dotenv import load_dotenv
    load_dotenv()
    _backend_env = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(_backend_env):
        load_dotenv(_backend_env, override=True)
except ImportError:
    pass

import psycopg2
from psycopg2.extras import DictCursor, DictRow

from backend.duplicate import canonicalize_url, extract_platform_and_item_id

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tts_app:ttsdata@localhost:5432/tts_research")


def _convert_placeholders(sql: str) -> str:
    """Replaces ? with %s outside single-quoted string literals."""
    out = []
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif ch == '?' and not in_string:
            out.append('%s')
        else:
            out.append(ch)
    return "".join(out)


def _translate_sql(sql: str) -> tuple[str, bool]:
    """Translates legacy SQL idioms to PostgreSQL equivalents."""
    s = sql.strip()

    # 1. PRAGMA table_info(<table>) -> information_schema
    m = re.match(r"(?i)^PRAGMA\s+table_info\((\w+)\)", s)
    if m:
        tbl = m.group(1).lower()
        return (
            f"SELECT column_name AS name FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{tbl}'",
            False
        )

    # 2. AUTOINCREMENT -> SERIAL
    s = re.sub(r"(?i)INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", s)

    # 3. INSERT OR IGNORE INTO speaker_duration_ledger
    if re.search(r"(?i)INSERT\s+OR\s+IGNORE\s+INTO\s+speaker_duration_ledger", s):
        s = re.sub(r"(?i)INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s)
        s += " ON CONFLICT (speaker_id, video_id) DO NOTHING"

    # 4. INSERT OR REPLACE INTO speaker_duration_ledger
    elif re.search(r"(?i)INSERT\s+OR\s+REPLACE\s+INTO\s+speaker_duration_ledger", s):
        s = re.sub(r"(?i)INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s)
        s += (
            " ON CONFLICT (speaker_id, video_id) DO UPDATE SET "
            "duration_seconds = EXCLUDED.duration_seconds, "
            "accepted_status = EXCLUDED.accepted_status, "
            "accepted_at = EXCLUDED.accepted_at"
        )

    # 5. INSERT OR REPLACE INTO pipeline_jobs
    elif re.search(r"(?i)INSERT\s+OR\s+REPLACE\s+INTO\s+pipeline_jobs", s):
        s = re.sub(r"(?i)INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s)
        s += (
            " ON CONFLICT (record_id) DO UPDATE SET "
            "job_id = EXCLUDED.job_id, file_name = EXCLUDED.file_name, "
            "audio_link = EXCLUDED.audio_link, speaker_id = EXCLUDED.speaker_id, "
            "speaker_name = EXCLUDED.speaker_name, stage = EXCLUDED.stage, "
            "step_id = EXCLUDED.step_id, step_name = EXCLUDED.step_name, "
            "progress_pct = EXCLUDED.progress_pct, started_at = EXCLUDED.started_at, "
            "started_at_display = EXCLUDED.started_at_display, error = EXCLUDED.error, "
            "updated_at = EXCLUDED.updated_at"
        )

    # 6. Convert ? placeholders to %s
    s = _convert_placeholders(s)

    # 7. Convert LIKE to ILIKE for case-insensitive search parity
    s = re.sub(r"(?i)\s+LIKE\s+", " ILIKE ", s)

    # 8. Append RETURNING id for INSERT queries if not already present
    appended_returning = False
    if (
        re.match(r"(?i)^\s*INSERT\s+INTO\s+\w+", s)
        and "RETURNING" not in s.upper()
        and ";" not in s
    ):
        s += " RETURNING id"
        appended_returning = True

    return s, appended_returning


class PgCursorWrapper:
    """Wraps psycopg2 DictCursor to provide a dict-based cursor API."""

    def __init__(self, raw_cursor):
        self._cursor = raw_cursor
        self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def execute(self, query: str, params: Any = None):
        translated_query, appended_returning = _translate_sql(query)
        if params is not None:
            if isinstance(params, (list, tuple)):
                # Ensure all elements are suitable for psycopg2
                params = tuple(params)
            self._cursor.execute(translated_query, params)
        else:
            self._cursor.execute(translated_query)

        if appended_returning:
            try:
                res = self._cursor.fetchone()
                if res:
                    self._lastrowid = res[0]
                else:
                    self._lastrowid = None
            except Exception:
                self._lastrowid = None
        return self

    def executescript(self, script: str):
        translated, _ = _translate_sql(script)
        self._cursor.execute(translated)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size: int = 1):
        return self._cursor.fetchmany(size)

    def close(self):
        return self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class PgConnectionWrapper:
    """Wraps psycopg2 connection to provide a wrapped connection API."""

    def __init__(self, raw_conn):
        self._conn = raw_conn
        self.row_factory = None

    def cursor(self):
        raw_cur = self._conn.cursor(cursor_factory=DictCursor)
        return PgCursorWrapper(raw_cur)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()


def get_connection() -> PgConnectionWrapper:
    """Returns a PostgreSQL connection wrapped for call parity."""
    raw_conn = psycopg2.connect(DATABASE_URL)
    return PgConnectionWrapper(raw_conn)


def init_db():
    """Initializes and verifies the PostgreSQL database schema idempotently."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
    except Exception as exc:
        print(f"Notice: Database connection could not be established during init_db: {exc}")
        return

    # 1. Records table (Video records)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS records (
        id SERIAL PRIMARY KEY,
        research_date TEXT NOT NULL,
        source TEXT NOT NULL,
        audio_link TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        platform_item_id TEXT,
        title TEXT,
        channel_name TEXT,
        description TEXT NOT NULL,
        gender TEXT NOT NULL,
        before_processing_duration TEXT NOT NULL,
        duration_seconds INTEGER DEFAULT 0,
        accent TEXT DEFAULT 'Normal Accent',
        licensing TEXT DEFAULT 'Unknown / Not Specified',
        licensing_evidence TEXT,
        sanskrit_evidence TEXT DEFAULT '',
        speakers_id TEXT DEFAULT '',
        researched_by TEXT NOT NULL,
        revalidated_by TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING_REVALIDATION',
        rejection_reason TEXT,
        completion_date TEXT DEFAULT '',
        text_link TEXT DEFAULT 'ASR',
        dataset_name TEXT DEFAULT '',
        after_processing_duration TEXT DEFAULT '0:00:00',
        total_files TEXT DEFAULT '',
        assigned_to TEXT DEFAULT '',
        data_type_augmentation_status TEXT DEFAULT 'Original',
        data_available TEXT DEFAULT 'Lightining AV/HF',
        speaker_channel_name TEXT DEFAULT '',
        handover TEXT DEFAULT 'No',
        remark TEXT DEFAULT '',
        duplicate_status TEXT DEFAULT 'UNIQUE',
        matched_video_id TEXT,
        decision_reason TEXT,
        evidence_fields TEXT,
        content_hash TEXT,
        web_domain TEXT,
        detected_license TEXT,
        license_detection_source TEXT,
        license_verification_status TEXT DEFAULT 'NEEDS_VERIFICATION',
        license_reviewer_decision TEXT DEFAULT 'PENDING',
        license_conflict_detected INTEGER DEFAULT 0,
        license_screening_guidance TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)

    cursor.execute("ALTER TABLE records DROP CONSTRAINT IF EXISTS records_canonical_url_key;")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_canonical_url ON records (canonical_url);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_platform_item ON records (source, platform_item_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON records (status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_speaker ON records (speakers_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_content_hash ON records (content_hash);")



    # 3. Speaker Duration Ledger table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS speaker_duration_ledger (
        id SERIAL PRIMARY KEY,
        speaker_id TEXT NOT NULL,
        video_id TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        accepted_status TEXT NOT NULL DEFAULT 'ACCEPTED',
        accepted_at TEXT NOT NULL,
        UNIQUE(speaker_id, video_id)
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ledger_speaker ON speaker_duration_ledger (speaker_id);")



    # 5. Audit Log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        record_id BIGINT,
        action TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        actor TEXT,
        reason TEXT,
        comments TEXT,
        timestamp TEXT,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Ensure audit_logs columns match if table was pre-existing
    for col, col_type in [
        ("record_id", "BIGINT"),
        ("from_status", "TEXT"),
        ("to_status", "TEXT"),
        ("actor", "TEXT"),
        ("reason", "TEXT"),
        ("comments", "TEXT"),
        ("timestamp", "TEXT")
    ]:
        cursor.execute(f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {col} {col_type};")



    # 7. Pipeline Jobs table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pipeline_jobs (
        id SERIAL PRIMARY KEY,
        record_id INTEGER NOT NULL UNIQUE REFERENCES records(id) ON DELETE CASCADE,
        job_id TEXT DEFAULT '',
        file_name TEXT DEFAULT '',
        audio_link TEXT DEFAULT '',
        speaker_id TEXT DEFAULT '',
        speaker_name TEXT DEFAULT '',
        gender TEXT DEFAULT 'auto',
        denoise_model TEXT DEFAULT 'BS-Roformer',
        asr_model TEXT DEFAULT 'Whisper Large-v3',
        language TEXT DEFAULT 'English',
        stage TEXT DEFAULT 'QUEUED',
        step_id TEXT DEFAULT 'validate',
        step_index INTEGER DEFAULT 0,
        total_steps INTEGER DEFAULT 6,
        step_name TEXT DEFAULT 'Initializing',
        progress_pct INTEGER DEFAULT 0,
        started_at TEXT,
        started_at_display TEXT,
        finished_at TEXT,
        elapsed_seconds INTEGER DEFAULT 0,
        took_formatted TEXT DEFAULT '',
        segments_count INTEGER DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pj_record_id ON pipeline_jobs(record_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pj_stage ON pipeline_jobs(stage);")

    conn.commit()



    # Synchronize approved records into speaker_duration_ledger idempotently
    cursor.execute("""
        SELECT id, platform_item_id, speakers_id, duration_seconds, updated_at
        FROM records
        WHERE status = 'APPROVED'
    """)
    approved_rows = cursor.fetchall()
    for row in approved_rows:
        spk = row["speakers_id"]
        v_id = row["platform_item_id"] or str(row["id"])
        dur = row["duration_seconds"] or 0
        ts = row["updated_at"]
        if spk and dur > 0:
            cursor.execute("""
                INSERT INTO speaker_duration_ledger (
                    speaker_id, video_id, duration_seconds, accepted_status, accepted_at
                ) VALUES (?, ?, ?, 'ACCEPTED', ?)
                ON CONFLICT (speaker_id, video_id) DO NOTHING
            """, (spk.strip().upper(), v_id, dur, ts))

    conn.commit()
    conn.close()


def duration_to_seconds(dur_str: str) -> int:
    """Parses HH:MM:SS or MM:SS to total seconds."""
    if not dur_str:
        return 0
    parts = dur_str.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1:
            return int(parts[0])
    except ValueError:
        return 0
    return 0


def format_duration(dur_str: str) -> str:
    """Standardizes duration string to HH:MM:SS format."""
    total_sec = duration_to_seconds(dur_str)
    if total_sec == 0 and not dur_str.startswith("0"):
        return dur_str
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def seed_sample_data(conn):
    """Seeds initial illustrative records into PostgreSQL."""
    cursor = conn.cursor()
    sample_records = [
        {
            "research_date": "2026-10-15",
            "source": "YouTube",
            "audio_link": "https://youtube.com/watch?v=EXAMPLE001",
            "canonical_url": canonicalize_url("https://youtube.com/watch?v=EXAMPLE001"),
            "platform_item_id": "EXAMPLE001",
            "title": "Indian English lecture containing spoken Sanskrit terms",
            "channel_name": "swapnalok",
            "description": "Indian English lecture containing spoken Sanskrit terms\nchannel : swapnalok",
            "gender": "Male",
            "before_processing_duration": "00:18:35",
            "duration_seconds": duration_to_seconds("00:18:35"),
            "accent": "Indian English",
            "licensing": "CC BY",
            "licensing_evidence": "Video description clearly specifies Creative Commons Attribution 4.0 International license.",
            "sanskrit_evidence": "Timestamp 04:15 - Recites 'Dharmo rakshati rakshitah'; Timestamp 11:22 - Explains 'Satya and Ahimsa' with accurate pronunciation.",
            "speakers_id": "SPK_001",
            "researched_by": "Researcher_A",
            "revalidated_by": "Reviewer_B",
            "status": "APPROVED",
            "duplicate_status": "UNIQUE",
            "rejection_reason": None,
            "created_at": "2026-10-15T09:30:00",
            "updated_at": "2026-10-15T11:15:00"
        },
        {
            "research_date": "2026-10-15",
            "source": "Internet Archive",
            "audio_link": "https://archive.org/details/EXAMPLE002",
            "canonical_url": canonicalize_url("https://archive.org/details/EXAMPLE002"),
            "platform_item_id": "EXAMPLE002",
            "title": "Educational discussion with Sanskrit terminology",
            "channel_name": "Public Archives",
            "description": "Educational discussion with Sanskrit terminology\nchannel : Public Archives",
            "gender": "Female",
            "before_processing_duration": "00:24:10",
            "duration_seconds": duration_to_seconds("00:24:10"),
            "accent": "Indian English",
            "licensing": "Public Domain",
            "licensing_evidence": "Metadata specifies Public Domain Mark 1.0, uploaded from historical public educational radio broadcast.",
            "sanskrit_evidence": "Timestamp 03:40 - Recites Rigveda mantra excerpt; Timestamp 14:10 - Spoken discussion of 'Upanishad' and 'Brahman'.",
            "speakers_id": "SPK_002",
            "researched_by": "Researcher_B",
            "revalidated_by": "Reviewer_C",
            "status": "APPROVED",
            "duplicate_status": "UNIQUE",
            "rejection_reason": None,
            "created_at": "2026-10-15T10:00:00",
            "updated_at": "2026-10-15T13:45:00"
        },
        {
            "research_date": "2026-10-16",
            "source": "YouTube",
            "audio_link": "https://youtube.com/watch?v=EXAMPLE003",
            "canonical_url": canonicalize_url("https://youtube.com/watch?v=EXAMPLE003"),
            "platform_item_id": "EXAMPLE003",
            "title": "Interview discussing Indian philosophy",
            "channel_name": "Philosophy India",
            "description": "Interview discussing Indian philosophy\nchannel : Philosophy India",
            "gender": "Male",
            "before_processing_duration": "00:32:45",
            "duration_seconds": duration_to_seconds("00:32:45"),
            "accent": "Indian English",
            "licensing": "Permission Required",
            "licensing_evidence": "Email permission requested from production studio, awaiting formal sign-off.",
            "sanskrit_evidence": "Timestamp 08:12 - Speaker recites 'Vasudhaiva Kutumbakam'; Timestamp 21:05 - Detailed pronunciation of 'Advaita Vedanta' concepts.",
            "speakers_id": "SPK_003",
            "researched_by": "Researcher_C",
            "revalidated_by": None,
            "status": "PENDING_REVALIDATION",
            "duplicate_status": "UNIQUE",
            "rejection_reason": None,
            "created_at": "2026-10-16T08:15:00",
            "updated_at": "2026-10-16T08:15:00"
        }
    ]

    for rec in sample_records:
        cursor.execute("""
        INSERT INTO records (
            research_date, source, audio_link, canonical_url, platform_item_id,
            title, channel_name, description, gender, before_processing_duration,
            duration_seconds, accent, licensing, licensing_evidence, sanskrit_evidence,
            speakers_id, researched_by, revalidated_by, status, duplicate_status,
            rejection_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec["research_date"], rec["source"], rec["audio_link"], rec["canonical_url"],
            rec["platform_item_id"], rec["title"], rec["channel_name"], rec["description"],
            rec["gender"], rec["before_processing_duration"], rec["duration_seconds"],
            rec["accent"], rec["licensing"], rec["licensing_evidence"],
            rec["sanskrit_evidence"], rec["speakers_id"], rec["researched_by"],
            rec["revalidated_by"], rec["status"], rec["duplicate_status"],
            rec["rejection_reason"], rec["created_at"], rec["updated_at"]
        ))
        rec_id = cursor.lastrowid

        # Insert audit log
        cursor.execute("""
        INSERT INTO audit_logs (
            record_id, action, from_status, to_status, actor, reason, comments, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec_id, "CREATED", None, rec["status"], rec["researched_by"],
            None, "Initial candidate entered", rec["created_at"]
        ))

        # Insert into speaker duration ledger if approved
        if rec["status"] == "APPROVED":
            cursor.execute("""
                INSERT INTO speaker_duration_ledger (
                    speaker_id, video_id, duration_seconds, accepted_status, accepted_at
                ) VALUES (?, ?, ?, 'ACCEPTED', ?)
                ON CONFLICT (speaker_id, video_id) DO NOTHING
            """, (rec["speakers_id"], rec["platform_item_id"], rec["duration_seconds"], rec["updated_at"]))

    conn.commit()


def upsert_web_verification(data: Dict[str, Any]) -> Dict[str, Any]:
    """Stores or updates a web verification record idempotently."""
    conn = get_connection()
    cursor = conn.cursor()
    now_iso = datetime.now().isoformat()

    url = (data.get("original_url") or data.get("url") or "").strip()
    canonical_url = (data.get("canonical_url") or "").strip()
    final_url = (data.get("final_url") or canonical_url).strip()
    domain = (data.get("domain") or "").strip().lower()
    title = (data.get("title") or "").strip()
    headings = json.dumps(data.get("headings") or []) if isinstance(data.get("headings"), list) else str(data.get("headings") or "[]")
    author = (data.get("author") or "").strip()
    pub_date = (data.get("publication_date") or "").strip()
    desc = (data.get("description") or "").strip()
    raw_text = (data.get("raw_text") or "").strip()
    norm_text = (data.get("normalized_text") or "").strip().lower()
    content_hash = (data.get("content_hash") or "").strip()
    passages = json.dumps(data.get("passages") or []) if isinstance(data.get("passages"), list) else str(data.get("passages") or "[]")
    status = (data.get("status") or "UNIQUE").upper()
    matched_page_id = str(data.get("matched_page_id") or "") if data.get("matched_page_id") else None
    matched_url = (data.get("matched_url") or "").strip() or None
    prefilter_status = (data.get("prefilter_status") or "").strip()
    prefilter_score = float(data.get("prefilter_score") or 0.0)
    lexical_score = float(data.get("lexical_score") or 0.0)
    passage_score = float(data.get("passage_score") or 0.0)
    title_score = float(data.get("title_score") or 0.0)
    decision_reason = (data.get("decision_reason") or "").strip()
    evidence_fields = json.dumps(data.get("evidence_fields") or []) if isinstance(data.get("evidence_fields"), list) else str(data.get("evidence_fields") or "[]")
    llm_called = 1 if data.get("llm_called") else 0
    confidence = float(data.get("confidence") or 1.0)
    latency_ms = float(data.get("latencies_ms", {}).get("total_latency_ms") or data.get("latency_ms") or 0.0)
    researched_by = (data.get("researched_by") or "Web Researcher").strip()

    # Check if record already exists by canonical_url
    cursor.execute("SELECT id, created_at FROM web_verifications WHERE canonical_url = ?", (canonical_url,))
    existing = cursor.fetchone()

    if existing:
        record_id = existing["id"]
        created_at = existing["created_at"]
        cursor.execute("""
            UPDATE web_verifications SET
                url = ?,
                final_url = ?,
                domain = ?,
                title = ?,
                headings = ?,
                author = ?,
                publication_date = ?,
                description = ?,
                raw_text = ?,
                normalized_text = ?,
                content_hash = ?,
                passages = ?,
                status = ?,
                matched_page_id = ?,
                matched_url = ?,
                prefilter_status = ?,
                prefilter_score = ?,
                lexical_score = ?,
                passage_score = ?,
                title_score = ?,
                decision_reason = ?,
                evidence_fields = ?,
                llm_called = ?,
                confidence = ?,
                latency_ms = ?,
                researched_by = ?,
                updated_at = ?
            WHERE id = ?
        """, (
            url, final_url, domain, title, headings, author, pub_date, desc,
            raw_text, norm_text, content_hash, passages, status, matched_page_id,
            matched_url, prefilter_status, prefilter_score, lexical_score,
            passage_score, title_score, decision_reason, evidence_fields,
            llm_called, confidence, latency_ms, researched_by, now_iso, record_id
        ))
        action = "UPDATED_IDEMPOTENT"
    else:
        created_at = now_iso
        cursor.execute("""
            INSERT INTO web_verifications (
                url, canonical_url, final_url, domain, title, headings, author,
                publication_date, description, raw_text, normalized_text, content_hash,
                passages, status, matched_page_id, matched_url, prefilter_status,
                prefilter_score, lexical_score, passage_score, title_score,
                decision_reason, evidence_fields, llm_called, confidence,
                latency_ms, researched_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url, canonical_url, final_url, domain, title, headings, author,
            pub_date, desc, raw_text, norm_text, content_hash,
            passages, status, matched_page_id, matched_url, prefilter_status,
            prefilter_score, lexical_score, passage_score, title_score,
            decision_reason, evidence_fields, llm_called, confidence,
            latency_ms, researched_by, created_at, now_iso
        ))
        record_id = cursor.lastrowid
        action = "STORED_INITIAL"



    conn.commit()
    conn.close()

    return {
        "id": record_id,
        "action": action,
        "canonical_url": canonical_url,
        "status": status,
        "content_hash": content_hash,
        "title": title,
        "domain": domain,
        "created_at": created_at,
        "updated_at": now_iso
    }


def get_web_verifications(
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    domain_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Retrieves stored web verification records with optional search and filters."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM web_verifications WHERE 1=1"
    params = []

    if status_filter and status_filter.upper() != "ALL":
        query += " AND status = ?"
        params.append(status_filter.upper())

    if domain_filter:
        query += " AND domain LIKE ?"
        params.append(f"%{domain_filter.strip().lower()}%")

    if search:
        s = f"%{search.strip().lower()}%"
        query += " AND (title LIKE ? OR canonical_url LIKE ? OR author LIKE ? OR description LIKE ?)"
        params.extend([s, s, s, s])

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]

    for r in rows:
        if isinstance(r.get("headings"), str):
            try:
                r["headings"] = json.loads(r["headings"])
            except Exception:
                pass
        if isinstance(r.get("passages"), str):
            try:
                r["passages"] = json.loads(r["passages"])
            except Exception:
                pass
        if isinstance(r.get("evidence_fields"), str):
            try:
                r["evidence_fields"] = json.loads(r["evidence_fields"])
            except Exception:
                pass

    conn.close()
    return rows


def get_web_verification_by_id(record_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a single web verification record by primary key."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM web_verifications WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    if isinstance(r.get("headings"), str):
        try:
            r["headings"] = json.loads(r["headings"])
        except Exception:
            pass
    if isinstance(r.get("passages"), str):
        try:
            r["passages"] = json.loads(r["passages"])
        except Exception:
            pass
    if isinstance(r.get("evidence_fields"), str):
        try:
            r["evidence_fields"] = json.loads(r["evidence_fields"])
        except Exception:
            pass
    return r


def get_all_stored_web_and_records() -> List[Dict[str, Any]]:
    """Returns stored intake records for duplicate checking."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, platform_item_id, title, channel_name, description, source,
               audio_link, canonical_url, before_processing_duration, duration_seconds,
               speakers_id, status, researched_by, content_hash, web_domain
        FROM records
    """)
    video_records = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return video_records


def flush_database() -> Dict[str, int]:
    """Truncates all transactional and record data tables, resetting identity sequences."""
    tables_to_flush = [
        "pipeline_jobs",
        "records",
        "audit_logs",
        "speaker_duration_ledger"
    ]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    existing_tables = set(r[0] for r in cur.fetchall())
    valid_tables = [t for t in tables_to_flush if t in existing_tables]
    if valid_tables:
        cur.execute(f"TRUNCATE TABLE {', '.join(valid_tables)} RESTART IDENTITY CASCADE;")
        conn.commit()
    counts = {}
    for t in valid_tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        counts[t] = cur.fetchone()[0]
    conn.close()
    return counts

