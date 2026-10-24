"""
Database manager for the Centralized Audio Research and Revalidation Workflow.
Uses SQLite for persistent storage with strict uniqueness constraints,
per-speaker duration tracking ledger, and full duplicate decision audit logs.
Adheres to Section 11 of final_implementation_architecture.md.
"""

import sqlite3
import os
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from backend.duplicate import canonicalize_url, extract_platform_and_item_id

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "audio_workflow.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Records table (Video records)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        research_date TEXT NOT NULL,
        source TEXT NOT NULL,
        audio_link TEXT NOT NULL,
        canonical_url TEXT NOT NULL UNIQUE,
        platform_item_id TEXT,
        description TEXT NOT NULL,
        gender TEXT NOT NULL,
        before_processing_duration TEXT NOT NULL,
        duration_seconds INTEGER DEFAULT 0,
        accent TEXT NOT NULL DEFAULT 'Indian English',
        licensing TEXT NOT NULL,
        licensing_evidence TEXT,
        sanskrit_evidence TEXT NOT NULL,
        speakers_id TEXT NOT NULL,
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
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)

    # Index on canonical_url and platform_item_id
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_canonical_url ON records (canonical_url);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_platform_item ON records (source, platform_item_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON records (status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_speaker ON records (speakers_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_content_hash ON records (content_hash);")

    # 2. Schema Migrations: Add new fields if not already present
    cursor.execute("PRAGMA table_info(records)")
    existing_cols = {col["name"] for col in cursor.fetchall()}

    columns_to_add = [
        ("title", "TEXT"),
        ("channel_name", "TEXT"),
        ("duplicate_status", "TEXT DEFAULT 'UNIQUE'"),
        ("matched_video_id", "TEXT"),
        ("decision_reason", "TEXT"),
        ("evidence_fields", "TEXT"),
        ("completion_date", "TEXT DEFAULT ''"),
        ("text_link", "TEXT DEFAULT 'ASR'"),
        ("dataset_name", "TEXT DEFAULT ''"),
        ("after_processing_duration", "TEXT DEFAULT '0:00:00'"),
        ("total_files", "TEXT DEFAULT ''"),
        ("assigned_to", "TEXT DEFAULT ''"),
        ("data_type_augmentation_status", "TEXT DEFAULT 'Original'"),
        ("data_available", "TEXT DEFAULT 'Lightining AV/HF'"),
        ("speaker_channel_name", "TEXT DEFAULT ''"),
        ("handover", "TEXT DEFAULT 'No'"),
        ("remark", "TEXT DEFAULT ''"),
        ("content_hash", "TEXT"),
        ("web_domain", "TEXT"),
        ("detected_license", "TEXT"),
        ("license_detection_source", "TEXT"),
        ("license_verification_status", "TEXT DEFAULT 'NEEDS_VERIFICATION'"),
        ("license_reviewer_decision", "TEXT DEFAULT 'PENDING'"),
        ("license_conflict_detected", "INTEGER DEFAULT 0"),
        ("license_screening_guidance", "TEXT")
    ]
    for col_name, col_type in columns_to_add:
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE records ADD COLUMN {col_name} {col_type};")

    # 3. Web Verifications table (8-Stage Web Search Deduplication Engine)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS web_verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        canonical_url TEXT NOT NULL UNIQUE,
        final_url TEXT,
        domain TEXT,
        title TEXT,
        headings TEXT,
        author TEXT,
        publication_date TEXT,
        description TEXT,
        raw_text TEXT,
        normalized_text TEXT,
        content_hash TEXT NOT NULL,
        passages TEXT,
        status TEXT NOT NULL DEFAULT 'UNIQUE',
        matched_page_id TEXT,
        matched_url TEXT,
        prefilter_status TEXT,
        prefilter_score REAL DEFAULT 0.0,
        lexical_score REAL DEFAULT 0.0,
        passage_score REAL DEFAULT 0.0,
        title_score REAL DEFAULT 0.0,
        decision_reason TEXT,
        evidence_fields TEXT,
        llm_called INTEGER DEFAULT 0,
        confidence REAL DEFAULT 1.0,
        latency_ms REAL DEFAULT 0.0,
        researched_by TEXT DEFAULT 'Web Researcher',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_canonical_url ON web_verifications (canonical_url);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_content_hash ON web_verifications (content_hash);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_domain ON web_verifications (domain);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_status ON web_verifications (status);")

    # 3. Speaker Duration Ledger table (Section 9 & 11)
    # Tracks cumulative accepted duration per speaker with unique constraint
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS speaker_duration_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        speaker_id TEXT NOT NULL,
        video_id TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        accepted_status TEXT NOT NULL DEFAULT 'ACCEPTED',
        accepted_at TEXT NOT NULL,
        UNIQUE(speaker_id, video_id)
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ledger_speaker ON speaker_duration_ledger (speaker_id);")

    # 4. Decision Audit table (Section 11)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS decision_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_video_id TEXT,
        status TEXT NOT NULL,
        matched_video_id TEXT,
        reason TEXT,
        evidence_fields TEXT,
        pre_filter_scores TEXT,
        endpoint_version TEXT,
        request_latency_ms REAL,
        decision_timestamp TEXT NOT NULL,
        error_state TEXT
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_candidate ON decision_audit (candidate_video_id);")

    # 5. Audit Log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT,
        comments TEXT,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(record_id) REFERENCES records(id) ON DELETE CASCADE
    );
    """)

    # 6. Audio Segments table (Section 3 of implementation.md)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audio_segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id INTEGER NOT NULL,
        segment_index INTEGER NOT NULL,
        file_name TEXT NOT NULL,
        audio_path TEXT NOT NULL,
        relative_url TEXT NOT NULL,
        transcription TEXT NOT NULL,
        original_transcription TEXT NOT NULL,
        duration_seconds REAL DEFAULT 0.0,
        language TEXT DEFAULT 'english',
        gender TEXT DEFAULT 'auto',
        validation_status TEXT DEFAULT 'PENDING',
        rejection_reason TEXT,
        validated_by TEXT,
        validated_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_segments_record_id ON audio_segments(record_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_segments_status ON audio_segments(validation_status);")

    conn.commit()

    # Seed illustrative examples if database is brand new
    cursor.execute("SELECT COUNT(*) as count FROM records")
    count = cursor.fetchone()["count"]
    if count == 0:
        seed_sample_data(conn)

    # 6. Synchronize approved records into speaker_duration_ledger idempotently
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
                INSERT OR IGNORE INTO speaker_duration_ledger (
                    speaker_id, video_id, duration_seconds, accepted_status, accepted_at
                ) VALUES (?, ?, ?, 'ACCEPTED', ?)
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
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()

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
                INSERT OR IGNORE INTO speaker_duration_ledger (
                    speaker_id, video_id, duration_seconds, accepted_status, accepted_at
                ) VALUES (?, ?, ?, 'ACCEPTED', ?)
            """, (rec["speakers_id"], rec["platform_item_id"], rec["duration_seconds"], rec["updated_at"]))

    conn.commit()


def upsert_web_verification(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stage 8: Store the decision idempotently.
    Saves the result, evidence, and matched record.
    If the canonical_url already exists, updates the existing row with new evaluation data,
    guaranteeing that repeated processing does not create duplicate rows.
    """
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

    # Also record in decision_audit
    cursor.execute("""
        INSERT INTO decision_audit (
            candidate_video_id, status, matched_video_id, reason,
            evidence_fields, pre_filter_scores, endpoint_version,
            request_latency_ms, decision_timestamp, error_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        canonical_url, status, matched_page_id, decision_reason,
        evidence_fields, str(prefilter_score), data.get("endpoint_version", "web-verifier"),
        latency_ms, now_iso, None
    ))

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
    """
    Combines records from both `records` and `web_verifications` tables
    to provide comprehensive cross-table duplicate checking.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Video / Intake records
    cursor.execute("""
        SELECT id, platform_item_id, title, channel_name, description, source,
               audio_link, canonical_url, before_processing_duration, duration_seconds,
               speakers_id, status, researched_by, content_hash, web_domain
        FROM records
    """)
    video_records = [dict(r) for r in cursor.fetchall()]

    # 2. Web verification records
    cursor.execute("""
        SELECT id, canonical_url, title, domain as channel_name, description,
               'Web' as source, url as audio_link, canonical_url,
               author, publication_date, headings, normalized_text, passages,
               content_hash, status, researched_by, '00:00:00' as before_processing_duration,
               0 as duration_seconds
        FROM web_verifications
    """)
    web_records = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return video_records + web_records
