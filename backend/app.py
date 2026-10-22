"""
FastAPI Application for Centralized Audio Research and Revalidation Workflow.
Implements the Low-Latency Metadata Duplicate Detection and Per-Speaker Duration Tracking
defined in final_implementation_architecture.md.
"""

import os
import io
import csv
import json
import time
import requests
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

from fastapi import FastAPI, HTTPException, Query, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles

from backend.database import (
    init_db, get_connection, format_duration, duration_to_seconds,
    upsert_web_verification, get_web_verifications, get_web_verification_by_id,
    get_all_stored_web_and_records
)
from backend.duplicate import (
    canonicalize_url, extract_platform_and_item_id, fast_prefilter, check_similarity
)
from backend.llm_adapter import (
    llm_config, LLMAdapter, LLMDecision
)
from backend.speaker_ledger import (
    MAX_SPEAKER_SECONDS, validate_speaker_duration_cap,
    record_accepted_duration_atomically, remove_speaker_duration,
    get_all_speaker_ledgers, get_speaker_accepted_seconds, format_seconds,
    sync_speaker_duration_ledger
)
from backend.fast_verifier import execute_fast_verification, fetch_essential_content
from backend.models import (
    DuplicateCheckRequest, DuplicateCheckResponse,
    RecordCreateRequest, RecordRevalidateRequest, RecordUpdateRequest,
    CsvUploadRequest, LLMConfigUpdateRequest, LLMTestRequest, YouTubeTestRequest,
    WebVerifyRequest, WebVerifyStoreRequest
)

app = FastAPI(
    title="Centralized Audio Research and Revalidation System",
    description="Low-Latency Duplicate Detection & Speaker Duration Ledger for Indian English Audio with Spoken Sanskrit Terms",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def format_to_ddmmyy(date_str: str) -> str:
    """Formats date to DDMMYY (e.g. 2026-10-15 -> 151026)."""
    if not date_str:
        return ""
    date_clean = str(date_str).strip()
    if len(date_clean) == 6 and date_clean.isdigit():
        return date_clean
    parts = date_clean.split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        return f"{parts[2].zfill(2)}{parts[1].zfill(2)}{parts[0][-2:]}"
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[0].zfill(2)}{parts[1].zfill(2)}{parts[2][-2:]}"
    parts_slash = date_clean.split("/")
    if len(parts_slash) == 3:
        y = parts_slash[2][-2:]
        return f"{parts_slash[0].zfill(2)}{parts_slash[1].zfill(2)}{y}"
    return date_clean

def format_to_slash_date(date_str: Optional[str]) -> str:
    """Formats date strictly as DD/MM/YYYY matching production spreadsheet."""
    if not date_str:
        return ""
    date_clean = str(date_str).strip()
    if "/" in date_clean:
        parts = date_clean.split("/")
        if len(parts) == 3:
            if len(parts[0]) == 4:  # YYYY/MM/DD
                return f"{int(parts[2]):02d}/{int(parts[1]):02d}/{parts[0]}"
            elif len(parts[2]) == 4:  # DD/MM/YYYY
                return f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"
            elif len(parts[2]) == 2:  # DD/MM/YY
                return f"{int(parts[0]):02d}/{int(parts[1]):02d}/20{parts[2]}"
    if "-" in date_clean:
        parts = date_clean.split("-")
        if len(parts) == 3:
            if len(parts[0]) == 4:  # YYYY-MM-DD
                return f"{int(parts[2]):02d}/{int(parts[1]):02d}/{parts[0]}"
            elif len(parts[2]) == 4:  # DD-MM-YYYY
                return f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"
            elif len(parts[2]) == 2:  # DD-MM-YY
                return f"{int(parts[0]):02d}/{int(parts[1]):02d}/20{parts[2]}"
    if len(date_clean) == 6 and date_clean.isdigit():
        return f"{date_clean[:2]}/{date_clean[2:4]}/20{date_clean[4:]}"
    if len(date_clean) == 8 and date_clean.isdigit():
        return f"{date_clean[6:]}/{date_clean[4:6]}/{date_clean[:4]}"
    return date_clean

# Initialize database tables and migrations
init_db()

# Mount static directory
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def read_root():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "Audio Research Workflow API is online. Please load the UI."}


# ==============================================================================
# DUPLICATE DETECTION API (SECTIONS 2, 3, 5, 6, 7 OF ARCHITECTURE)
# ==============================================================================

@app.post("/api/check-duplicate", response_model=DuplicateCheckResponse)
def check_duplicate_api(req: DuplicateCheckRequest):
    """
    Fast, Low-Latency URL Validation and Webpage-Fetching Pipeline:
    1. Validates HTTP/HTTPS URLs safely.
    2. Lightweight HTTP requests with connection pooling, strict timeouts, response-size limits, safe redirects.
    3. Strictly enforces TLS verification (verify=True).
    4. Extracts essential fields: canonical URL, title, metadata, main text.
    5. Returns FETCH_FAILED if page inaccessible; never marks inaccessible pages as unique.
    6. Exact content-hash (SHA-256) and canonical URL match first against indexed DB.
    7. Fast indexed pre-filter; invokes self-hosted LLM only for plausible candidates.
    8. Validates per-speaker duration cap.
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL cannot be empty")

    res = execute_fast_verification(
        url=url,
        speakers_id=req.speakers_id,
        candidate_description=req.description,
        candidate_duration=req.before_processing_duration
    )

    meta = res.get("metadata") or {}
    raw_duration = req.before_processing_duration or meta.get("duration") or "00:00:00"
    formatted_dur = format_duration(raw_duration)
    cand_dur_seconds = duration_to_seconds(formatted_dur)

    conn = get_connection()
    spk_check = validate_speaker_duration_cap(conn, req.speakers_id, cand_dur_seconds)

    # Audit the decision
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO decision_audit (
            candidate_video_id, status, matched_video_id, reason,
            evidence_fields, pre_filter_scores, endpoint_version,
            request_latency_ms, decision_timestamp, error_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        res.get("platform_item_id") or res.get("canonical_url"),
        res.get("status"),
        res.get("matched_video_id"),
        res.get("reason"),
        json.dumps(res.get("evidence_fields", [])),
        "0.0",
        res.get("endpoint_version", "fast-pipeline"),
        res.get("latency_ms", 0.0),
        datetime.now().isoformat(),
        res.get("reason") if res.get("status") == "FETCH_FAILED" else None
    ))
    conn.commit()
    conn.close()

    return DuplicateCheckResponse(
        is_duplicate=res.get("is_duplicate", False),
        status=res.get("status", "UNIQUE"),
        matched_video_id=res.get("matched_video_id"),
        reason=res.get("reason", ""),
        evidence_fields=res.get("evidence_fields", []),
        confidence=res.get("confidence", 0.0),
        prefilter_triggered=res.get("endpoint_version") not in ("fast-exact-index", "fast-content-fetcher", "fast-url-validator"),
        llm_called=res.get("endpoint_version") not in ("fast-exact-index", "fast-prefilter", "fast-content-fetcher", "fast-url-validator"),
        request_latency_ms=res.get("latency_ms", 0.0),
        endpoint_version=res.get("endpoint_version"),
        canonical_url=res.get("canonical_url", url),
        platform=res.get("platform", "Web"),
        platform_item_id=res.get("platform_item_id"),
        existing_record=res.get("existing_record"),
        similar_records=res.get("similar_records", []),
        metadata=meta,
        speaker_duration_check=spk_check
    )


@app.get("/api/fetch-metadata")
def fetch_metadata_api(url: str = Query(...)):
    """Explicitly fetch metadata for a given URL using fast fetcher."""
    url = url.strip()
    from backend.fast_verifier import validate_and_normalize_url, fetch_essential_content
    is_valid, canon, platform, item_id, _ = validate_and_normalize_url(url)
    return fetch_essential_content(canon or url, platform, item_id)


@app.post("/api/web-verify/check")
def web_verify_check_api(req: WebVerifyRequest):
    """Low-latency verification check."""
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    return execute_fast_verification(url=url)


@app.post("/api/web-verify/store")
def web_verify_store_api(req: WebVerifyStoreRequest):
    """
    Stage 8: Store the decision idempotently.
    Saves the result, evidence, and matched record.
    Repeated processing updates the existing record rather than creating duplicate entries.
    """
    try:
        data = req.model_dump()
        stored_record = upsert_web_verification(data)
        return {"success": True, "record": stored_record}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store web verification: {str(e)}")


@app.get("/api/web-verify/records")
def get_web_verifications_api(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query("ALL"),
    domain: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    """Retrieves saved web verification records with optional search and filters."""
    records = get_web_verifications(
        search=search,
        status_filter=status,
        domain_filter=domain,
        limit=limit,
        offset=offset
    )
    return {"total": len(records), "records": records}


@app.get("/api/web-verify/records/{record_id}")
def get_web_verification_record_api(record_id: int):
    """Retrieves full details of a single web verification record."""
    rec = get_web_verification_by_id(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Web verification record not found")
    return rec


# ==============================================================================
# RECORD INTAKE & SPEAKER DURATION CAP ENFORCEMENT (SECTION 9)
# ==============================================================================

@app.post("/api/records", status_code=status.HTTP_201_CREATED)
def create_record(req: RecordCreateRequest):
    """
    Submits a new candidate record:
    1. Validates duplicate status:
       - Confirmed DUPLICATE is rejected/blocked.
       - POTENTIAL_DUPLICATE routes to review queue.
    2. Validates per-speaker duration cap (12,600s = 3.5h):
       - If current_accepted + candidate_duration > 12600, rejects with 400.
    3. Atomically updates records table and speaker_duration_ledger.
    """
    url = (req.audio_link or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Audio link is required")

    canonical_url = canonicalize_url(url)
    platform, platform_item_id = extract_platform_and_item_id(url)
    source_name = req.source.strip() or platform

    # Format duration
    formatted_dur = format_duration(req.before_processing_duration)
    dur_seconds = duration_to_seconds(formatted_dur)

    conn = get_connection()
    cursor = conn.cursor()

    # Exact duplicate check against database
    cursor.execute("SELECT * FROM records WHERE canonical_url = ?", (canonical_url,))
    duplicate = cursor.fetchone()
    if not duplicate and platform_item_id and source_name in ("YouTube", "Internet Archive", "Vimeo"):
        cursor.execute("SELECT * FROM records WHERE source = ? AND platform_item_id = ?", (source_name, platform_item_id))
        duplicate = cursor.fetchone()

    if not duplicate:
        cursor.execute("SELECT * FROM web_verifications WHERE canonical_url = ?", (canonical_url,))
        web_dup = cursor.fetchone()
        if web_dup:
            duplicate = web_dup

    if duplicate:
        conn.close()
        dup_dict = dict(duplicate)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Exact duplicate recording already exists in the database.",
                "existing_record": {
                    "id": dup_dict.get("id"),
                    "audio_link": dup_dict.get("audio_link") or dup_dict.get("canonical_url") or dup_dict.get("url"),
                    "speakers_id": dup_dict.get("speakers_id") or "—",
                    "researched_by": dup_dict.get("researched_by") or "Researcher",
                    "status": dup_dict.get("status") or "DUPLICATE",
                    "rejection_reason": dup_dict.get("rejection_reason") or dup_dict.get("decision_reason")
                }
            }
        )

    # Check if candidate was classified as DUPLICATE by LLM / prefilter
    if req.duplicate_status == "DUPLICATE" and req.action.upper() == "SEND_FOR_REVIEW":
        conn.close()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot submit duplicate recording for review. Duplicate candidates must be excluded."
        )

    # Strictly block FETCH_FAILED URLs from being submitted into dataset
    if req.duplicate_status == "FETCH_FAILED":
        conn.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot submit record: URL content could not be verified or is inaccessible. Inaccessible pages cannot be accepted into the dataset."
        )

    action = req.action.upper()
    if action == "SEND_FOR_REVIEW":
        # Section 9 Rule 6: Validate Speaker Duration Cap
        cap_check = validate_speaker_duration_cap(conn, req.speakers_id, dur_seconds)
        if not cap_check["is_valid"]:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"Rejection: {cap_check['message']}"
            )
        new_status = "PENDING_REVALIDATION"
    elif action == "REJECT":
        new_status = "REJECTED_BY_RESEARCHER"
        if not (req.rejection_reason or "").strip():
            conn.close()
            raise HTTPException(
                status_code=400,
                detail="Rejection reason is required when rejecting a candidate."
            )
    elif action == "SAVE_DRAFT":
        new_status = "PENDING_RESEARCH"
    else:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Invalid action '{req.action}'. Must be SEND_FOR_REVIEW, REJECT, or SAVE_DRAFT.")

    now_iso = datetime.now().isoformat()
    sanskrit_val = (req.sanskrit_evidence or "").strip()
    evidence_json = json.dumps(req.evidence_fields) if req.evidence_fields else None

    # Derive title and channel
    desc_clean = req.description.strip()
    derived_title = desc_clean.split("\n")[0] if desc_clean else "Audio recording"
    channel_name = req.speaker_channel_name or ""
    if not channel_name:
        for line in desc_clean.splitlines():
            if "channel" in line.lower() and ":" in line:
                channel_name = line.split(":", 1)[1].strip()
                break
    if not channel_name:
        channel_name = req.speakers_id.strip()

    dataset_name = req.dataset_name or derived_title.replace(" ", "_")[:60]
    assigned_to = req.assigned_to or req.researched_by.strip()
    completion_date = req.completion_date or ""
    text_link = req.text_link or "ASR"
    after_dur = req.after_processing_duration or "0:00:00"
    total_files = req.total_files or ""
    data_type = req.data_type_augmentation_status or "Original"
    data_avail = req.data_available or "Lightining AV/HF"
    handover = req.handover or "No"
    remark = req.remark or ""

    try:
        cursor.execute("""
            INSERT INTO records (
                research_date, source, audio_link, canonical_url, platform_item_id,
                title, channel_name, description, gender, before_processing_duration,
                duration_seconds, accent, licensing, licensing_evidence, sanskrit_evidence,
                speakers_id, researched_by, revalidated_by, status, duplicate_status,
                matched_video_id, decision_reason, evidence_fields, rejection_reason,
                completion_date, text_link, dataset_name, after_processing_duration,
                total_files, assigned_to, data_type_augmentation_status, data_available,
                speaker_channel_name, handover, remark,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            req.research_date or datetime.now().strftime("%Y-%m-%d"),
            source_name,
            req.audio_link.strip(),
            canonical_url,
            platform_item_id,
            derived_title,
            channel_name,
            desc_clean,
            req.gender.strip(),
            formatted_dur,
            dur_seconds,
            req.accent.strip() or "Normal Accent",
            req.licensing.strip(),
            req.licensing_evidence.strip() if req.licensing_evidence else "",
            sanskrit_val,
            req.speakers_id.strip(),
            req.researched_by.strip(),
            None,
            new_status,
            req.duplicate_status or "UNIQUE",
            req.matched_video_id,
            req.decision_reason,
            evidence_json,
            req.rejection_reason.strip() if req.rejection_reason else None,
            completion_date,
            text_link,
            dataset_name,
            after_dur,
            total_files,
            assigned_to,
            data_type,
            data_avail,
            channel_name,
            handover,
            remark,
            now_iso,
            now_iso
        ))
        record_id = cursor.lastrowid

        audit_action = "SUBMITTED_FOR_REVIEW" if new_status == "PENDING_REVALIDATION" else ("REJECTED_BY_RESEARCHER" if new_status == "REJECTED_BY_RESEARCHER" else "DRAFT_SAVED")
        cursor.execute("""
            INSERT INTO audit_logs (
                record_id, action, from_status, to_status, actor, reason, comments, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id,
            audit_action,
            None,
            new_status,
            req.researched_by.strip(),
            req.rejection_reason,
            "Researcher submission with duplicate verification",
            now_iso
        ))

        sync_speaker_duration_ledger(conn)
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    cursor.execute("SELECT * FROM records WHERE id = ?", (record_id,))
    created_rec = dict(cursor.fetchone())
    conn.close()
    return created_rec


# ==============================================================================
# REVALIDATION & ATOMIC DURATION LEDGER UPDATES (SECTION 9)
# ==============================================================================

@app.post("/api/records/{record_id}/revalidate")
def revalidate_record(record_id: int, req: RecordRevalidateRequest):
    """
    Reviewer action on a candidate record:
    - Allows the reviewer/revalidator to make changes in the content sent for review:
      (Audio Link, Speaker Name, Speaker ID, Duration, Gender, Accent, Source, Licensing, Sanskrit Evidence, Description).
    - Actions:
      * APPROVE: Saves content changes, validates 3.5h speaker duration cap, moves to APPROVED, updates ledger.
      * SAVE_ONLY: Saves content changes without altering status (allows reviewer to stage changes).
      * REJECT: Saves content changes, sets REJECTED_AT_REVALIDATION with reason, removes duration from ledger.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Record not found")

    record = dict(row)
    old_status = record["status"]
    action = req.action.upper()
    reviewer = (req.reviewer or "").strip()

    if not reviewer:
        conn.close()
        raise HTTPException(status_code=400, detail="Reviewer identity (Revalidated_By) is required")

    now_iso = datetime.now().isoformat()
    modified_fields = []

    # 1. Resolve editable content fields from reviewer request (defaulting to existing record values)
    new_url = req.audio_link.strip() if req.audio_link and req.audio_link.strip() else record["audio_link"]
    canonical_url = record["canonical_url"]
    platform_item_id = record["platform_item_id"]
    if new_url != record["audio_link"]:
        canonical_url = canonicalize_url(new_url)
        _, platform_item_id = extract_platform_and_item_id(new_url)
        cursor.execute("SELECT id FROM records WHERE canonical_url = ? AND id != ?", (canonical_url, record_id))
        dup = cursor.fetchone()
        if dup:
            conn.close()
            raise HTTPException(status_code=409, detail=f"Another record (ID #{dup['id']}) already has this canonical URL.")
        modified_fields.append("audio_link")

    raw_dur = req.before_processing_duration if req.before_processing_duration is not None else record["before_processing_duration"]
    formatted_dur = format_duration(raw_dur) if raw_dur else record["before_processing_duration"]
    dur_seconds = duration_to_seconds(formatted_dur) if formatted_dur else record.get("duration_seconds", 0)
    if formatted_dur != record["before_processing_duration"]:
        modified_fields.append("duration")

    spk_id = req.speakers_id.strip() if req.speakers_id is not None else (record.get("speakers_id") or "")
    spk_chan = req.speaker_channel_name.strip() if req.speaker_channel_name is not None else (record.get("speaker_channel_name") or "")
    if spk_id != (record.get("speakers_id") or ""):
        modified_fields.append("speakers_id")
    if spk_chan != (record.get("speaker_channel_name") or ""):
        modified_fields.append("speaker_channel_name")

    gender = req.gender if req.gender is not None else record.get("gender", "Unspecified")
    if gender != record.get("gender"):
        modified_fields.append("gender")

    accent = req.accent if req.accent is not None else record.get("accent", "Indian English")
    if accent != record.get("accent"):
        modified_fields.append("accent")

    source = req.source if req.source is not None else record.get("source", "YouTube")
    if source != record.get("source"):
        modified_fields.append("source")

    licensing = req.licensing if req.licensing is not None else record.get("licensing", "CC")
    if licensing != record.get("licensing"):
        modified_fields.append("licensing")

    sanskrit_evidence = req.sanskrit_evidence if req.sanskrit_evidence is not None else record.get("sanskrit_evidence", "")
    if sanskrit_evidence != record.get("sanskrit_evidence"):
        modified_fields.append("sanskrit_evidence")

    description = req.description if req.description is not None else record.get("description", "")
    if description != record.get("description"):
        modified_fields.append("description")

    # Determine speaker identity for ledger & cap check
    speaker_check_id = spk_chan if (spk_chan and (not spk_id or spk_id.upper().startswith("SPK_"))) else (spk_id or spk_chan)
    video_item_id = f"REC_{record_id}"

    if action == "APPROVE":
        # Section 9: Validate 3.5h duration cap before approval
        cap_check = validate_speaker_duration_cap(conn, speaker_check_id, dur_seconds, exclude_video_id=video_item_id)
        if not cap_check["is_valid"]:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve: {cap_check['message']}"
            )

        new_status = "APPROVED"
        rejection_reason = None
        audit_action = "APPROVED"

        # Atomically record into speaker_duration_ledger
        record_accepted_duration_atomically(
            conn,
            speaker_check_id,
            video_item_id,
            dur_seconds,
            accepted_status="ACCEPTED"
        )

    elif action == "SAVE_ONLY":
        new_status = old_status
        rejection_reason = record.get("rejection_reason")
        audit_action = "MODIFIED_BY_REVIEWER"

    elif action == "REJECT":
        new_status = "REJECTED_AT_REVALIDATION"
        rejection_reason = (req.rejection_reason or "").strip()
        if not rejection_reason:
            conn.close()
            raise HTTPException(status_code=400, detail="Rejection reason is required when rejecting a record")
        audit_action = "REJECTED_AT_REVALIDATION"

        # Remove from speaker_duration_ledger if present
        remove_speaker_duration(conn, speaker_check_id, video_item_id)
        if platform_item_id:
            remove_speaker_duration(conn, speaker_check_id, platform_item_id)
    else:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Invalid revalidation action '{req.action}'. Must be APPROVE, REJECT, or SAVE_ONLY.")

    comp_date = record.get("completion_date") or (datetime.now().strftime("%d/%m/%Y") if new_status == "APPROVED" else "")
    assigned_val = req.assigned_to or record.get("assigned_to") or reviewer
    dataset_val = req.dataset_name or record.get("dataset_name") or record.get("title", "").replace(" ", "_")[:60]
    handover_val = req.handover or record.get("handover") or "No"
    data_avail_val = req.data_available or record.get("data_available") or "Lightining AV/HF"
    remark_val = req.notes or rejection_reason or record.get("remark") or ""

    cursor.execute("""
        UPDATE records
        SET status = ?, revalidated_by = ?, rejection_reason = ?, updated_at = ?,
            completion_date = ?, assigned_to = ?, dataset_name = ?, handover = ?,
            data_available = ?, remark = ?,
            audio_link = ?, canonical_url = ?, platform_item_id = ?,
            before_processing_duration = ?, duration_seconds = ?,
            speakers_id = ?, speaker_channel_name = ?,
            gender = ?, accent = ?, source = ?, licensing = ?,
            sanskrit_evidence = ?, description = ?
        WHERE id = ?
    """, (
        new_status, reviewer, rejection_reason, now_iso,
        comp_date, assigned_val, dataset_val, handover_val,
        data_avail_val, remark_val,
        new_url, canonical_url, platform_item_id,
        formatted_dur, dur_seconds,
        spk_id, spk_chan,
        gender, accent, source, licensing,
        sanskrit_evidence, description,
        record_id
    ))

    audit_comment = req.notes or ""
    if modified_fields:
        prefix = f"[Content edited by reviewer: {', '.join(modified_fields)}]"
        audit_comment = f"{prefix} {audit_comment}".strip()
    if not audit_comment:
        if new_status == "APPROVED":
            audit_comment = "Record approved for final dataset"
        elif action == "SAVE_ONLY":
            audit_comment = "Record content updated by reviewer"
        else:
            audit_comment = "Record rejected at revalidation"

    cursor.execute("""
        INSERT INTO audit_logs (
            record_id, action, from_status, to_status, actor, reason, comments, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record_id,
        audit_action,
        old_status,
        new_status,
        reviewer,
        rejection_reason,
        audit_comment,
        now_iso
    ))

    sync_speaker_duration_ledger(conn)
    conn.commit()
    cursor.execute("SELECT * FROM records WHERE id = ?", (record_id,))
    updated_rec = dict(cursor.fetchone())
    conn.close()
    return updated_rec


# ==============================================================================
# SPEAKER DURATION LEDGER APIS (SECTION 9)
# ==============================================================================

@app.get("/api/speakers/durations")
def list_speaker_durations():
    """Returns accepted duration vs 12,600s cap for all speakers."""
    conn = get_connection()
    durations = get_all_speaker_ledgers(conn)
    conn.close()
    return {
        "max_seconds_per_speaker": MAX_SPEAKER_SECONDS,
        "formatted_max_limit": "3h 30m 0s",
        "speakers": durations
    }


@app.get("/api/speakers/{speaker_id}/duration")
def get_single_speaker_duration(speaker_id: str):
    """Returns accepted duration details and remaining allowance for a speaker."""
    conn = get_connection()
    status_info = validate_speaker_duration_cap(conn, speaker_id, candidate_seconds=0)

    # Get approved videos list for this speaker
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, platform_item_id, title, duration_seconds, before_processing_duration, audio_link, updated_at
        FROM records
        WHERE UPPER(speakers_id) = ? AND status = 'APPROVED'
        ORDER BY updated_at DESC
    """, (speaker_id.strip().upper(),))
    accepted_vids = [dict(r) for r in cursor.fetchall()]
    conn.close()

    status_info["approved_records"] = accepted_vids
    return status_info


# ==============================================================================
# LLM ENDPOINT CONFIGURATION & TEST APIS (SECTION 7)
# ==============================================================================

@app.get("/api/llm/config")
def get_llm_config_api():
    """Returns active self-hosted LLM endpoint configuration."""
    return llm_config.to_dict()


@app.post("/api/llm/config")
def update_llm_config_api(req: LLMConfigUpdateRequest):
    """Updates self-hosted LLM endpoint parameters dynamically."""
    llm_config.update(req.model_dump(exclude_unset=True))
    return {
        "message": "LLM endpoint configuration updated successfully.",
        "config": llm_config.to_dict()
    }


@app.post("/api/llm/test")
def test_llm_endpoint_api(req: LLMTestRequest):
    """Tests the self-hosted LLM endpoint connection with live parameter testing."""
    override = {}
    if req.base_url is not None:
        override["base_url"] = req.base_url
    if req.endpoint_path is not None:
        override["endpoint_path"] = req.endpoint_path
    if req.api_key is not None:
        override["api_key"] = req.api_key
    if req.model is not None:
        override["model"] = req.model
    override["timeout_seconds"] = min(int(req.timeout_seconds), 4) if req.timeout_seconds is not None else 4

    active_base_url = (override.get("base_url") if "base_url" in override else llm_config.base_url).strip().rstrip("/")
    active_path = (override.get("endpoint_path") if "endpoint_path" in override else llm_config.endpoint_path).strip()
    active_model = (override.get("model") if "model" in override else llm_config.model).strip()

    sample_candidate = {
        "title": req.test_title or "Ancient Indian Vedic Chanting and Grammar",
        "channel": req.test_channel or "swapnalok",
        "description": f"{req.test_title}\nchannel : {req.test_channel}",
        "duration_seconds": duration_to_seconds(req.test_duration or "00:18:35")
    }
    sample_match = [{
        "id": 9999,
        "platform_item_id": "TEST_REF_01",
        "title": "Ancient Indian Vedic Chanting Lecture",
        "channel": "swapnalok",
        "description": "Ancient Indian Vedic Chanting Lecture\nchannel : swapnalok",
        "duration_seconds": duration_to_seconds(req.test_duration or "00:18:35"),
        "prefilter_score": 85.0
    }]
    decision = LLMAdapter.evaluate(sample_candidate, sample_match, override_config=override if override else None)
    
    is_remote = bool(active_base_url)
    endpoint_display = f"{active_base_url}{active_path}" if is_remote else "Standalone Semantic Engine"
    success = not bool(decision.error_state) if is_remote else True

    return {
        "success": success,
        "is_remote": is_remote,
        "endpoint_url": endpoint_display,
        "model": active_model if is_remote else "Built-in RapidFuzz & Semantic Matcher",
        "decision": decision.model_dump(),
        "error": decision.error_state,
        "message": (
            "Connected successfully to remote LLM endpoint!" if (is_remote and success)
            else f"Remote LLM connection failed: {decision.error_state}" if (is_remote and not success)
            else "Standalone Semantic Engine is fully operational. Zero external API calls required."
        )
    }


@app.get("/api/youtube/status")
@app.post("/api/youtube/test")
def test_youtube_api_endpoint(req: Optional[YouTubeTestRequest] = None):
    """
    Verifies connection to the official Google YouTube Data API v3.
    Tests with standard video ID dQw4w9WgXcQ.
    """
    key = (req.api_key if req and req.api_key else os.getenv("YOUTUBE_API_KEY", "")).strip()
    video_id = (req.sample_video_id if req and req.sample_video_id else "dQw4w9WgXcQ").strip()

    if not key:
        return {
            "connected": False,
            "has_key": False,
            "status": "NOT_CONFIGURED",
            "message": "No YOUTUBE_API_KEY configured in .env. YouTube metadata is operating via public oEmbed fallback.",
            "source": "oEmbed / Web Scraper (Operational)"
        }

    try:
        api_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id={video_id}&key={key}"
        res = requests.get(api_url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            items = data.get("items", [])
            if items:
                snippet = items[0].get("snippet", {})
                content_details = items[0].get("contentDetails", {})
                return {
                    "connected": True,
                    "has_key": True,
                    "status": "CONNECTED",
                    "api_service": "Google YouTube Data API v3",
                    "message": "YouTube Data API v3 key is verified and operational!",
                    "sample_title": snippet.get("title", ""),
                    "sample_channel": snippet.get("channelTitle", ""),
                    "sample_duration": content_details.get("duration", "")
                }
            else:
                return {
                    "connected": True,
                    "has_key": True,
                    "status": "CONNECTED_EMPTY",
                    "api_service": "Google YouTube Data API v3",
                    "message": "API key is valid, but test video was not found."
                }
        else:
            try:
                err_data = res.json()
                err_msg = err_data.get("error", {}).get("message", res.text[:200])
            except Exception:
                err_msg = res.text[:200]
            return {
                "connected": False,
                "has_key": True,
                "status": f"HTTP_{res.status_code}",
                "api_service": "Google YouTube Data API v3",
                "error": f"HTTP {res.status_code}: {err_msg}",
                "message": f"YouTube API rejected key: {err_msg}"
            }
    except Exception as e:
        return {
            "connected": False,
            "has_key": True,
            "status": "NETWORK_ERROR",
            "error": str(e),
            "message": f"Network error testing YouTube API: {str(e)}"
        }


@app.get("/api/audit/decisions")
def get_decision_audit_logs(limit: int = 50):
    """Retrieves recent duplicate detection decisions for compliance and analysis."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM decision_audit ORDER BY id DESC LIMIT ?
    """, (limit,))
    logs = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return logs


# ==============================================================================
# RECORD BROWSING & DETAIL
# ==============================================================================

@app.get("/api/records")
def list_records(
    status_filter: Optional[str] = Query(None, alias="status"),
    search: Optional[str] = None,
    source: Optional[str] = None
):
    """List records with optional status, source, or text filtering."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM records WHERE 1=1"
    params = []

    if status_filter and status_filter.upper() != "ALL":
        query += " AND status = ?"
        params.append(status_filter.upper())

    if source and source.upper() != "ALL":
        query += " AND source LIKE ?"
        params.append(f"%{source}%")

    if search:
        search_like = f"%{search.strip()}%"
        query += """ AND (
            speakers_id LIKE ? OR
            description LIKE ? OR
            audio_link LIKE ? OR
            researched_by LIKE ? OR
            revalidated_by LIKE ? OR
            licensing LIKE ?
        )"""
        params.extend([search_like] * 6)

    query += " ORDER BY research_date DESC, id DESC"
    cursor.execute(query, params)
    records = []
    for r in cursor.fetchall():
        d = dict(r)
        d["research_date_ddmmyy"] = format_to_ddmmyy(d.get("research_date"))
        records.append(d)
    conn.close()
    return records


@app.get("/api/records/{record_id}")
def get_record_detail(record_id: int):
    """Get single record with its full audit log trail."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Record not found")

    record = dict(row)

    cursor.execute("""
        SELECT * FROM audit_logs WHERE record_id = ? ORDER BY timestamp ASC
    """, (record_id,))
    logs = [dict(l) for l in cursor.fetchall()]
    record["audit_history"] = logs
    conn.close()
    return record


@app.put("/api/records/{record_id}")
def update_record(record_id: int, req: RecordUpdateRequest):
    """Update fields of an existing record and log the change."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Record not found")

    current = dict(row)
    now_iso = datetime.now().isoformat()

    new_url = req.audio_link.strip() if req.audio_link else current["audio_link"]
    canonical_url = current["canonical_url"]
    platform_item_id = current["platform_item_id"]
    if new_url != current["audio_link"]:
        canonical_url = canonicalize_url(new_url)
        _, platform_item_id = extract_platform_and_item_id(new_url)
        cursor.execute("SELECT id FROM records WHERE canonical_url = ? AND id != ?", (canonical_url, record_id))
        dup = cursor.fetchone()
        if dup:
            conn.close()
            raise HTTPException(status_code=409, detail="Another record already has this canonical URL.")

    formatted_dur = format_duration(req.before_processing_duration) if req.before_processing_duration else current["before_processing_duration"]
    dur_sec = duration_to_seconds(formatted_dur)

    new_status = req.status or current["status"]

    cursor.execute("""
        UPDATE records SET
            research_date = ?, source = ?, audio_link = ?, canonical_url = ?, platform_item_id = ?,
            description = ?, gender = ?, before_processing_duration = ?, duration_seconds = ?,
            accent = ?, licensing = ?, licensing_evidence = ?, sanskrit_evidence = ?,
            speakers_id = ?, researched_by = ?, status = ?, rejection_reason = ?, updated_at = ?
        WHERE id = ?
    """, (
        req.research_date or current["research_date"],
        req.source or current["source"],
        new_url,
        canonical_url,
        platform_item_id,
        req.description if req.description is not None else current["description"],
        req.gender or current["gender"],
        formatted_dur,
        dur_sec,
        req.accent or current["accent"],
        req.licensing or current["licensing"],
        req.licensing_evidence if req.licensing_evidence is not None else current["licensing_evidence"],
        req.sanskrit_evidence if req.sanskrit_evidence is not None else current["sanskrit_evidence"],
        req.speakers_id or current["speakers_id"],
        req.researched_by or current["researched_by"],
        new_status,
        req.rejection_reason if req.rejection_reason is not None else current["rejection_reason"],
        now_iso,
        record_id
    ))

    cursor.execute("""
        INSERT INTO audit_logs (
            record_id, action, from_status, to_status, actor, reason, comments, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record_id,
        "EDITED",
        current["status"],
        new_status,
        req.actor or "Editor",
        req.rejection_reason,
        "Record updated",
        now_iso
    ))

    conn.commit()
    cursor.execute("SELECT * FROM records WHERE id = ?", (record_id,))
    updated_rec = dict(cursor.fetchone())
    conn.close()
    return updated_rec


# ==============================================================================
# CSV IMPORT FEATURE (WITH HISTORICAL DE-DUPLICATION & 22-COLUMN SCHEMA)
# ==============================================================================

CSV_EXPORT_COLUMNS = [
    "Date",
    "Completion_date",
    "Source",
    "Audio_Link",
    "Text_Link",
    "Description",
    "Dataset Name",
    "Gender",
    "Before_Processing_Duration",
    "After_Processing_Duration",
    "Total_files",
    "Researched_By",
    "Assigned_to",
    "Status",
    "Data_type (Augmentation_Status)",
    "Data_available",
    "Speakers_ID",
    "Speaker/Channel_Name",
    "Accent",
    "Licensing",
    "Handover",
    "Remark"
]

def find_row_val(row: Dict[str, str], aliases: List[str], default: str = "") -> str:
    """Helper to find value in row with flexible column name aliases."""
    row_normalized = {
        k.strip().lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_"): v 
        for k, v in row.items() if k
    }
    for alias in aliases:
        clean = alias.strip().lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")
        if clean in row_normalized and row_normalized[clean] is not None and str(row_normalized[clean]).strip():
            return str(row_normalized[clean]).strip()
    return default


@app.post("/api/upload-csv")
def upload_csv_data(req: CsvUploadRequest):
    """
    Imports historical records with strict de-duplication:
    1. Exact video ID & canonical URL matching skips duplicate rows.
    2. Enforces speaker duration limit for approved historical records.
    3. Atomically updates speaker_duration_ledger.
    4. Supports full 22-column production schema and legacy 11-column formats.
    """
    csv_text = (req.csv_text or "").strip()
    if not csv_text:
        raise HTTPException(status_code=400, detail="CSV content cannot be empty.")

    if csv_text.startswith("\ufeff"):
        csv_text = csv_text[1:]

    target_status = req.target_status or "APPROVED"

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse CSV: {str(e)}")

    conn = get_connection()
    cursor = conn.cursor()

    total_rows = 0
    imported_count = 0
    skipped_duplicates = 0
    duplicate_items = []
    now_iso = datetime.now().isoformat()

    for row in reader:
        total_rows += 1
        audio_link = find_row_val(row, [
            "audio_link", "audiolink", "audio/video link", "audio_video_link",
            "audio/video_link", "video_link", "videolink", "url", "link",
            "audio_url", "recording_url"
        ])
        if not audio_link:
            continue

        canonical_url = canonicalize_url(audio_link)
        platform, platform_item_id = extract_platform_and_item_id(audio_link)

        source_val = find_row_val(row, ["source", "platform", "website"], default=platform or "Youtube")
        desc_val = find_row_val(row, ["description", "title", "content"], default="Historical entry")
        gender_val = find_row_val(row, ["gender", "sex"], default="Unspecified")
        raw_dur = find_row_val(row, [
            "before_processing_duration", "before-processing duration",
            "before_processing duration", "duration", "duration_seconds",
            "duration (seconds)", "duration seconds", "length"
        ], default="00:15:00")
        formatted_dur = format_duration(raw_dur)
        dur_seconds = duration_to_seconds(formatted_dur)
        accent_val = find_row_val(row, ["accent", "language_accent"], default="Normal Accent")
        licensing_val = find_row_val(row, ["licensing", "license", "rights"], default="CC")
        spk_channel_val = find_row_val(row, [
            "speaker/channel_name", "speaker / channel_name", "speaker_channel_name",
            "speaker_name", "speaker name", "channel_name", "channel"
        ], default="").strip()
        spk_val = find_row_val(row, [
            "speakers_id", "speaker_id", "speakerid", "speakers id", "speaker id",
            "speaker_name", "speaker name", "speaker"
        ], default="").strip()

        # If speakers_id was not explicitly provided, use speaker_channel_name
        if not spk_val:
            spk_val = spk_channel_val or f"SPK_HIST_{total_rows:03d}"
        if not spk_channel_val:
            spk_channel_val = spk_val

        researched_by_val = find_row_val(row, ["researched_by", "researchedby", "researcher", "submitted_by"], default="Historical_Data")
        revalidated_by_val = find_row_val(row, ["revalidated_by", "revalidatedby", "reviewer", "verified_by"], default="Historical_Reviewer")
        research_date_val = find_row_val(row, ["date", "research_date", "researchdate"], default=datetime.now().strftime("%Y-%m-%d"))

        completion_date_val = find_row_val(row, ["completion_date", "completiondate"], default="")
        text_link_val = find_row_val(row, ["text_link", "textlink"], default="ASR")
        dataset_name_val = find_row_val(row, ["dataset_name", "dataset name", "dataset"], default="")
        after_dur_val = find_row_val(row, ["after_processing_duration", "after_processing duration"], default="0:00:00")
        total_files_val = find_row_val(row, ["total_files", "totalfiles"], default="")
        assigned_to_val = find_row_val(row, ["assigned_to", "assignedto"], default=researched_by_val)
        status_val = find_row_val(row, ["status"], default=target_status).strip().upper()
        data_type_val = find_row_val(row, ["data_type_augmentation_status", "data_type (augmentation_status)", "data_type", "augmentation_status"], default="Original")
        data_available_val = find_row_val(row, ["data_available", "dataavailable"], default="Lightining AV/HF")
        handover_val = find_row_val(row, ["handover"], default="No")
        remark_val = find_row_val(row, ["remark", "remarks", "notes", "comment", "comments"], default="")

        # Exact duplicate check
        cursor.execute("SELECT id, speakers_id, audio_link, status FROM records WHERE canonical_url = ?", (canonical_url,))
        existing = cursor.fetchone()

        if not existing and platform_item_id and platform in ("YouTube", "Internet Archive", "Vimeo"):
            cursor.execute("SELECT id, speakers_id, audio_link, status FROM records WHERE source = ? AND platform_item_id = ?", (source_val, platform_item_id))
            existing = cursor.fetchone()

        if existing:
            skipped_duplicates += 1
            duplicate_items.append({
                "url": audio_link,
                "existing_id": existing["id"],
                "speakers_id": existing["speakers_id"],
                "status": existing["status"]
            })
            continue

        # Check speaker duration cap if importing as approved
        actual_status = status_val if status_val in ("APPROVED", "PENDING_REVALIDATION", "REJECTED_BY_RESEARCHER", "REJECTED_AT_REVALIDATION") else target_status
        rej_reason = None
        if actual_status == "APPROVED":
            cap_check = validate_speaker_duration_cap(conn, spk_val, dur_seconds)
            if not cap_check["is_valid"]:
                actual_status = "PENDING_REVALIDATION"
                rej_reason = "Flagged: would exceed 3.5h speaker cap during batch import."

        derived_title = desc_val.split("\n")[0] if desc_val else "Historical Audio"
        if not dataset_name_val:
            dataset_name_val = derived_title.replace(" ", "_")[:60]
        if not spk_channel_val:
            spk_channel_val = spk_val

        # Insert record with all 22 columns
        cursor.execute("""
            INSERT INTO records (
                research_date, source, audio_link, canonical_url, platform_item_id,
                title, channel_name, description, gender, before_processing_duration, duration_seconds,
                accent, licensing, licensing_evidence, sanskrit_evidence,
                speakers_id, researched_by, revalidated_by, status, duplicate_status, rejection_reason,
                completion_date, text_link, dataset_name, after_processing_duration, total_files,
                assigned_to, data_type_augmentation_status, data_available, speaker_channel_name,
                handover, remark, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            research_date_val, source_val, audio_link, canonical_url, platform_item_id,
            derived_title, spk_channel_val, desc_val, gender_val, formatted_dur, dur_seconds,
            accent_val, licensing_val, "Imported from CSV dataset", "",
            spk_val, researched_by_val, revalidated_by_val if actual_status == "APPROVED" else None,
            actual_status, "UNIQUE", rej_reason,
            completion_date_val, text_link_val, dataset_name_val, after_dur_val, total_files_val,
            assigned_to_val, data_type_val, data_available_val, spk_channel_val,
            handover_val, remark_val, now_iso, now_iso
        ))
        record_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO audit_logs (
                record_id, action, from_status, to_status, actor, reason, comments, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id, "IMPORTED_FROM_CSV", None, actual_status,
            researched_by_val, rej_reason, "Imported from dataset", now_iso
        ))

        # Update ledger if approved
        if actual_status == "APPROVED":
            record_accepted_duration_atomically(
                conn, spk_val, platform_item_id or str(record_id), dur_seconds
            )

        imported_count += 1

    sync_speaker_duration_ledger(conn)
    conn.commit()
    conn.close()

    return {
        "total_rows": total_rows,
        "imported": imported_count,
        "skipped_duplicates": skipped_duplicates,
        "duplicates": duplicate_items[:10]
    }


# ==============================================================================
# STRICT 22-COLUMN RFC 4180 CSV EXPORT & PREVIEW
# ==============================================================================

@app.get("/api/export/csv")
def export_approved_csv():
    """
    Exports approved records as CSV adhering strictly to the 22-column production schema:
    Date,Completion_date,Source,Audio_Link,Text_Link,Description,Dataset Name,Gender,
    Before_Processing_Duration,After_Processing_Duration,Total_files,Researched_By,
    Assigned_to,Status,Data_type (Augmentation_Status),Data_available,Speakers_ID,
    Speaker/Channel_Name,Accent,Licensing,Handover,Remark
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM records
        WHERE status = 'APPROVED'
        ORDER BY research_date DESC, id DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    seen_urls = set()
    deduped_rows = []
    for r in rows:
        canon = r.get("canonical_url")
        if canon in seen_urls:
            continue
        seen_urls.add(canon)
        deduped_rows.append(r)

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL, lineterminator='\r\n')
    writer.writerow(CSV_EXPORT_COLUMNS)

    for r in deduped_rows:
        date_val = format_to_slash_date(r.get("research_date"))
        completion_val = format_to_slash_date(r.get("completion_date")) or ""
        source_val = r.get("source") or "Youtube"
        audio_link_val = r.get("audio_link") or ""
        text_link_val = r.get("text_link") or "ASR"
        desc_val = r.get("description") or ""
        dataset_name_val = r.get("dataset_name") or r.get("title") or "Indian_English_TTS"
        gender_val = r.get("gender") or ""
        before_dur = r.get("before_processing_duration") or "0:00:00"
        after_dur = r.get("after_processing_duration") or "0:00:00"
        total_files = r.get("total_files") or ""
        researched_by = r.get("researched_by") or ""
        assigned_to = r.get("assigned_to") or r.get("revalidated_by") or researched_by
        status_val = r.get("status") or "Approved"
        data_type = r.get("data_type_augmentation_status") or "Original"
        data_available = r.get("data_available") or "Lightining AV/HF"
        speakers_id = r.get("speakers_id") or ""
        spk_channel = r.get("speaker_channel_name") or r.get("channel_name") or speakers_id
        accent = r.get("accent") or "Normal Accent"
        licensing = r.get("licensing") or "CC"
        handover = r.get("handover") or "No"
        remark = r.get("remark") or r.get("rejection_reason") or ""

        writer.writerow([
            date_val,
            completion_val,
            source_val,
            audio_link_val,
            text_link_val,
            desc_val,
            dataset_name_val,
            gender_val,
            before_dur,
            after_dur,
            total_files,
            researched_by,
            assigned_to,
            status_val,
            data_type,
            data_available,
            speakers_id,
            spk_channel,
            accent,
            licensing,
            handover,
            remark
        ])

    csv_data = output.getvalue()
    csv_bytes = "\ufeff" + csv_data
    filename = f"approved_dataset_{datetime.now().strftime('%Y%m%d')}.csv"

    return Response(
        content=csv_bytes.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


@app.get("/api/export/preview")
def preview_approved_csv():
    """Returns approved records in CSV format matching the 22-column production schema."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM records WHERE status = 'APPROVED' ORDER BY research_date DESC, id DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    seen_urls = set()
    deduped = []
    for r in rows:
        if r["canonical_url"] not in seen_urls:
            seen_urls.add(r["canonical_url"])
            deduped.append(r)

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL, lineterminator='\n')
    writer.writerow(CSV_EXPORT_COLUMNS)

    preview_rows = []
    for r in deduped:
        date_val = format_to_slash_date(r.get("research_date"))
        completion_val = format_to_slash_date(r.get("completion_date")) or ""
        source_val = r.get("source") or "Youtube"
        audio_link_val = r.get("audio_link") or ""
        text_link_val = r.get("text_link") or "ASR"
        desc_val = r.get("description") or ""
        dataset_name_val = r.get("dataset_name") or r.get("title") or "Indian_English_TTS"
        gender_val = r.get("gender") or ""
        before_dur = r.get("before_processing_duration") or "0:00:00"
        after_dur = r.get("after_processing_duration") or "0:00:00"
        total_files = r.get("total_files") or ""
        researched_by = r.get("researched_by") or ""
        assigned_to = r.get("assigned_to") or r.get("revalidated_by") or researched_by
        status_val = r.get("status") or "Approved"
        data_type = r.get("data_type_augmentation_status") or "Original"
        data_available = r.get("data_available") or "Lightining AV/HF"
        speakers_id = r.get("speakers_id") or ""
        spk_channel = r.get("speaker_channel_name") or r.get("channel_name") or speakers_id
        accent = r.get("accent") or "Normal Accent"
        licensing = r.get("licensing") or "CC"
        handover = r.get("handover") or "No"
        remark = r.get("remark") or r.get("rejection_reason") or ""

        writer.writerow([
            date_val, completion_val, source_val, audio_link_val, text_link_val,
            desc_val, dataset_name_val, gender_val, before_dur, after_dur,
            total_files, researched_by, assigned_to, status_val, data_type,
            data_available, speakers_id, spk_channel, accent, licensing,
            handover, remark
        ])

        preview_rows.append({
            "Date": date_val,
            "Completion_date": completion_val,
            "Source": source_val,
            "Audio_Link": audio_link_val,
            "Text_Link": text_link_val,
            "Description": desc_val,
            "Dataset Name": dataset_name_val,
            "Gender": gender_val,
            "Before_Processing_Duration": before_dur,
            "After_Processing_Duration": after_dur,
            "Total_files": total_files,
            "Researched_By": researched_by,
            "Assigned_to": assigned_to,
            "Status": status_val,
            "Data_type (Augmentation_Status)": data_type,
            "Data_available": data_available,
            "Speakers_ID": speakers_id,
            "Speaker/Channel_Name": spk_channel,
            "Accent": accent,
            "Licensing": licensing,
            "Handover": handover,
            "Remark": remark
        })

    return {
        "count": len(deduped),
        "columns": CSV_EXPORT_COLUMNS,
        "csv_text": output.getvalue(),
        "rows": preview_rows
    }


# ==============================================================================
# STATS & DASHBOARD METRICS
# ==============================================================================

@app.get("/api/stats")
def get_stats():
    """Summary metrics across all records and statuses."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT status, COUNT(*) as count FROM records GROUP BY status")
    status_counts = {r["status"]: r["count"] for r in cursor.fetchall()}

    cursor.execute("SELECT COUNT(DISTINCT speakers_id) as count FROM records WHERE status = 'APPROVED'")
    approved_speakers = cursor.fetchone()["count"]

    cursor.execute("SELECT SUM(duration_seconds) as total_sec FROM records WHERE status = 'APPROVED'")
    total_approved_sec = cursor.fetchone()["total_sec"] or 0

    total_h = total_approved_sec // 3600
    total_m = (total_approved_sec % 3600) // 60

    cursor.execute("""
        SELECT id, research_date, description, status, speakers_id, source, researched_by, duplicate_status
        FROM records
        ORDER BY id DESC
        LIMIT 6
    """)
    recent_rows = []
    for r in cursor.fetchall():
        row_dict = dict(r)
        row_dict["research_date_ddmmyy"] = format_to_ddmmyy(row_dict.get("research_date"))
        desc = (row_dict.get("description") or "").strip()
        row_dict["title"] = desc.split("\n")[0] if desc else "Audio recording"
        recent_rows.append(row_dict)

    conn.close()
    approved_hours_dec = round(total_approved_sec / 3600.0, 1)
    total_all = sum(status_counts.values())

    return {
        "pending_revalidation": status_counts.get("PENDING_REVALIDATION", 0),
        "approved": status_counts.get("APPROVED", 0),
        "rejected_by_researcher": status_counts.get("REJECTED_BY_RESEARCHER", 0),
        "rejected_at_revalidation": status_counts.get("REJECTED_AT_REVALIDATION", 0),
        "pending_research": status_counts.get("PENDING_RESEARCH", 0),
        "total_records": total_all,
        "approved_speakers": approved_speakers,
        "approved_duration_formatted": f"{total_h}h {total_m}m",
        "approved_hours_decimal": approved_hours_dec,
        "approved_duration_seconds": total_approved_sec,
        "recent_submissions": recent_rows,
        "max_speaker_hours": 3.5
    }


@app.get("/api/next-speaker-id")
def get_next_speaker_id():
    """Calculates the next sequential Speaker ID (e.g. SPK_006)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT speakers_id FROM records")
    rows = cursor.fetchall()
    conn.close()

    max_num = 0
    import re
    for r in rows:
        spk = r["speakers_id"] or ""
        match = re.search(r'SPK_(\d+)', spk, re.IGNORECASE)
        if match:
            num = int(match.group(1))
            if num > max_num:
                max_num = num

    next_id = f"SPK_{max_num + 1:03d}"
    return {"next_speaker_id": next_id}
