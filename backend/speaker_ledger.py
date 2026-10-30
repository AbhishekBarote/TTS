"""
Speaker Duration Ledger and Cap Enforcement.
Adheres strictly to Section 9 of final_implementation_architecture.md:
- Maximum accepted duration is 12,600 seconds per speaker (3.5 hours = 210 minutes).
- Only accepted, non-duplicate videos contribute to the speaker total.
- Current total + candidate duration <= 12,600 is required for acceptance.
- Atomic updates to ensure concurrent jobs or repeated submissions cannot breach cap.
- Reprocessing the same video is idempotent and never double-counts duration.
- Synchronized with records and CSV imports: adding records for the same speaker increments cumulative duration.
"""

import re
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

from backend.database import get_connection

MAX_SPEAKER_SECONDS = 12600  # 3.5 hours * 3600 seconds


def normalize_speaker_key(speaker_str: str) -> str:
    """
    Normalizes speaker ID/name for consistent grouping:
    - Strips leading/trailing whitespace
    - Normalizes internal spacing around dots: 'Dr .Raj' -> 'DR. RAJ'
    - Collapses multiple whitespace: 'SPK   001' -> 'SPK 001'
    - Converts to uppercase
    """
    if not speaker_str:
        return ""
    s = str(speaker_str).strip()
    s = re.sub(r'\s*\.\s*', '. ', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip().upper()


def lock_speaker_for_update(conn: Any, speaker_id: str) -> None:
    """
    Acquires a transaction-level advisory lock on the normalized speaker key
    to prevent concurrent approval or intake race conditions on the duration cap.
    """
    if not speaker_id:
        return
    clean_spk = normalize_speaker_key(speaker_id)
    if not clean_spk:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (clean_spk,))
    except Exception:
        pass


def format_seconds(seconds: int) -> str:
    """Formats integer seconds to Xh Ym Zs string."""
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    else:
        return f"{s}s"


def extract_speaker_identity(record: Dict[str, Any]) -> Tuple[str, str]:
    """
    Extracts the normalized speaker key and best display name from a record dict.
    Prioritizes real human speaker names (e.g. from speaker_channel_name or speakers_id)
    over arbitrary sequential IDs (e.g. SPK_001, SPK_HIST_001).
    """
    spk_id = (record.get("speakers_id") or "").strip()
    spk_chan = (record.get("speaker_channel_name") or "").strip()

    is_placeholder_id = spk_id.upper().startswith("SPK_") or spk_id.upper().startswith("SPK-")

    if spk_chan and is_placeholder_id:
        best_name = spk_chan
    elif spk_chan and not spk_id:
        best_name = spk_chan
    elif spk_id:
        best_name = spk_id
    elif spk_chan:
        best_name = spk_chan
    else:
        best_name = "Unknown Speaker"

    return normalize_speaker_key(best_name), best_name


def sync_speaker_duration_ledger(conn: Optional[Any] = None):
    """
    Synchronizes speaker_duration_ledger with records table in PostgreSQL.
    Idempotent: Reprocessing the same record updates without duplicating duration.
    """
    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, speakers_id, speaker_channel_name, duration_seconds, platform_item_id, status, updated_at
            FROM records
            WHERE status = 'APPROVED'
              AND duration_seconds > 0
              AND ((speakers_id IS NOT NULL AND TRIM(speakers_id) != '') OR (speaker_channel_name IS NOT NULL AND TRIM(speaker_channel_name) != ''))
        """)
        rows = cursor.fetchall()
        for r in rows:
            spk_key, _ = extract_speaker_identity(dict(r))
            v_key = f"REC_{r['id']}"
            dur = int(r["duration_seconds"] or 0)
            ts = r["updated_at"] or datetime.now().isoformat()
            cursor.execute("""
                INSERT INTO speaker_duration_ledger (
                    speaker_id, video_id, duration_seconds, accepted_status, accepted_at
                ) VALUES (%s, %s, %s, 'ACCEPTED', %s)
                ON CONFLICT (speaker_id, video_id) DO UPDATE SET
                    duration_seconds = EXCLUDED.duration_seconds,
                    accepted_status = EXCLUDED.accepted_status,
                    accepted_at = EXCLUDED.accepted_at
            """, (spk_key, v_key, dur, ts))
        conn.commit()
    finally:
        if close_after:
            conn.close()


def get_speaker_accepted_seconds(conn_or_speaker: Any = None, speaker_id: Optional[str] = None) -> int:
    """
    Retrieves the total accepted seconds for a speaker in PostgreSQL.
    Supports get_speaker_accepted_seconds(conn, speaker_id) or get_speaker_accepted_seconds(speaker_id).
    """
    if isinstance(conn_or_speaker, str) and speaker_id is None:
        target_speaker = conn_or_speaker
        conn = None
    else:
        conn = conn_or_speaker
        target_speaker = speaker_id or ""

    if not target_speaker:
        return 0

    clean_spk = normalize_speaker_key(target_speaker)
    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    try:
        cursor = conn.cursor()

        # Query all approved records for this speaker
        cursor.execute("""
            SELECT id, speakers_id, speaker_channel_name, duration_seconds
            FROM records
            WHERE status = 'APPROVED'
        """)
        rec_total = 0
        for r in cursor.fetchall():
            r_spk, _ = extract_speaker_identity(dict(r))
            if (r_spk == clean_spk or 
                normalize_speaker_key(r["speakers_id"] or "") == clean_spk or 
                normalize_speaker_key(r["speaker_channel_name"] or "") == clean_spk):
                rec_total += int(r["duration_seconds"] or 0)

        # Also check speaker_duration_ledger for any non-records entries
        cursor.execute("""
            SELECT video_id, duration_seconds
            FROM speaker_duration_ledger
            WHERE UPPER(speaker_id) = %s AND accepted_status = 'ACCEPTED'
        """, (clean_spk,))
        ledger_extra = 0
        for r in cursor.fetchall():
            v_id = str(r["video_id"])
            if not v_id.startswith("REC_"):
                cursor.execute("SELECT id FROM records WHERE platform_item_id = %s OR CAST(id AS TEXT) = %s", (v_id, v_id))
                if not cursor.fetchone():
                    ledger_extra += int(r["duration_seconds"] or 0)

        return rec_total + ledger_extra
    finally:
        if close_after:
            conn.close()


def validate_speaker_duration_cap(
    conn_or_speaker: Any = None,
    speaker_id: Optional[Any] = None,
    candidate_seconds: int = 0,
    exclude_video_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Validates whether adding candidate_seconds to speaker_id would violate
    the 12,600-second (3.5-hour) cap.
    Supports validate_speaker_duration_cap(conn, speaker_id, candidate_seconds, ...)
    or validate_speaker_duration_cap(speaker_id, candidate_seconds, ...).
    """
    if isinstance(conn_or_speaker, str):
        # Called as (speaker_id, candidate_seconds, ...)
        target_speaker = conn_or_speaker
        cand_sec = int(speaker_id or 0)
        ex_vid = candidate_seconds if isinstance(candidate_seconds, str) else exclude_video_id
        conn = None
    else:
        conn = conn_or_speaker
        target_speaker = str(speaker_id or "")
        cand_sec = int(candidate_seconds or 0)
        ex_vid = exclude_video_id

    clean_spk = normalize_speaker_key(target_speaker)
    if not clean_spk:
        return {
            "speaker_id": "",
            "is_valid": True,
            "current_accepted_seconds": 0,
            "candidate_seconds": cand_sec,
            "projected_seconds": cand_sec,
            "max_seconds": MAX_SPEAKER_SECONDS,
            "remaining_seconds": MAX_SPEAKER_SECONDS,
            "percentage_used": 0.0,
            "message": "No speaker assigned",
            "formatted_current": "0s",
            "formatted_remaining": format_seconds(MAX_SPEAKER_SECONDS)
        }

    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    try:
        current_sec = get_speaker_accepted_seconds(conn, clean_spk)

        # If updating an existing accepted video, deduct its duration from current_sec
        if ex_vid:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT duration_seconds FROM speaker_duration_ledger
                WHERE UPPER(speaker_id) = %s AND video_id = %s AND accepted_status = 'ACCEPTED'
            """, (clean_spk, str(ex_vid)))
            row = cursor.fetchone()
            if row and row["duration_seconds"]:
                current_sec = max(0, current_sec - int(row["duration_seconds"]))

        projected_sec = current_sec + max(0, cand_sec)
        is_valid = projected_sec <= MAX_SPEAKER_SECONDS
        remaining_sec = max(0, MAX_SPEAKER_SECONDS - current_sec)
        pct = round((current_sec / MAX_SPEAKER_SECONDS) * 100.0, 1) if MAX_SPEAKER_SECONDS > 0 else 0.0

        if is_valid:
            msg = f"Within 3.5h limit ({format_seconds(current_sec)} used, {format_seconds(remaining_sec)} remaining)."
        else:
            excess = projected_sec - MAX_SPEAKER_SECONDS
            msg = f"Would exceed 3.5-hour speaker limit by {format_seconds(excess)} (Current: {format_seconds(current_sec)}, Candidate: {format_seconds(cand_sec)}, Limit: {format_seconds(MAX_SPEAKER_SECONDS)})."

        return {
            "speaker_id": clean_spk,
            "is_valid": is_valid,
            "current_accepted_seconds": current_sec,
            "candidate_seconds": cand_sec,
            "projected_seconds": projected_sec,
            "max_seconds": MAX_SPEAKER_SECONDS,
            "remaining_seconds": remaining_sec,
            "percentage_used": pct,
            "message": msg,
            "formatted_current": format_seconds(current_sec),
            "formatted_remaining": format_seconds(remaining_sec)
        }
    finally:
        if close_after:
            conn.close()


def record_accepted_duration_atomically(
    conn_or_speaker: Any = None,
    speaker_id: Optional[Any] = None,
    video_id: Optional[Any] = None,
    duration_seconds: int = 0,
    accepted_status: str = "ACCEPTED"
) -> bool:
    """
    Atomically updates the PostgreSQL speaker duration ledger.
    Supports record_accepted_duration_atomically(conn, speaker_id, video_id, duration_seconds)
    or record_accepted_duration_atomically(speaker_id, video_id, duration_seconds).
    """
    if isinstance(conn_or_speaker, str):
        target_speaker = conn_or_speaker
        target_vid = str(speaker_id or "")
        dur_sec = int(video_id or 0)
        status_val = str(duration_seconds) if isinstance(duration_seconds, str) else accepted_status
        conn = None
    else:
        conn = conn_or_speaker
        target_speaker = str(speaker_id or "")
        target_vid = str(video_id or "")
        dur_sec = int(duration_seconds or 0)
        status_val = accepted_status

    if not target_speaker or not target_vid:
        return False

    clean_spk = normalize_speaker_key(target_speaker)
    now_iso = datetime.now().isoformat()
    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO speaker_duration_ledger (
                speaker_id, video_id, duration_seconds, accepted_status, accepted_at
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (speaker_id, video_id) DO UPDATE SET
                duration_seconds = EXCLUDED.duration_seconds,
                accepted_status = EXCLUDED.accepted_status,
                accepted_at = EXCLUDED.accepted_at
        """, (clean_spk, str(target_vid), int(dur_sec), status_val, now_iso))
        conn.commit()
        return True
    finally:
        if close_after:
            conn.close()


def remove_speaker_duration(
    conn_or_speaker: Any = None,
    speaker_id: Optional[str] = None,
    video_id: Optional[str] = None
):
    """
    Removes a video entry from the PostgreSQL duration ledger.
    Supports remove_speaker_duration(conn, speaker_id, video_id)
    or remove_speaker_duration(speaker_id, video_id).
    """
    if isinstance(conn_or_speaker, str) and video_id is None:
        target_speaker = conn_or_speaker
        target_vid = str(speaker_id or "")
        conn = None
    else:
        conn = conn_or_speaker
        target_speaker = str(speaker_id or "")
        target_vid = str(video_id or "")

    if not target_speaker or not target_vid:
        return

    clean_spk = normalize_speaker_key(target_speaker)
    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    try:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM speaker_duration_ledger
            WHERE UPPER(speaker_id) = %s AND video_id = %s
        """, (clean_spk, str(target_vid)))
        conn.commit()
    finally:
        if close_after:
            conn.close()


def get_all_speaker_ledgers(conn: Optional[Any] = None) -> List[Dict[str, Any]]:
    """
    Retrieves duration tracking metrics for all speakers in PostgreSQL.
    Synchronizes records and ledger so cumulative duration updates whenever
    the same speaker is added via CSV import or approved entries.
    """
    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    try:
        # Auto-synchronize ledger from approved records
        sync_speaker_duration_ledger(conn)

        cursor = conn.cursor()

        # Aggregate all approved records by normalized speaker
        cursor.execute("""
            SELECT id, speakers_id, speaker_channel_name, duration_seconds
            FROM records
            WHERE status = 'APPROVED'
        """)
        speaker_map = {}
        for r in cursor.fetchall():
            spk_key, display_name = extract_speaker_identity(dict(r))
            if not spk_key:
                continue
            dur = int(r["duration_seconds"] or 0)

            if spk_key not in speaker_map:
                speaker_map[spk_key] = {
                    "speaker_id": spk_key,
                    "speaker_name": display_name,
                    "accepted_seconds": 0,
                    "record_count": 0
                }
            speaker_map[spk_key]["accepted_seconds"] += dur
            speaker_map[spk_key]["record_count"] += 1
            if display_name and not display_name.upper().startswith("SPK_"):
                speaker_map[spk_key]["speaker_name"] = display_name

        # Include standalone ledger entries
        cursor.execute("""
            SELECT speaker_id, video_id, duration_seconds
            FROM speaker_duration_ledger
            WHERE accepted_status = 'ACCEPTED' AND speaker_id IS NOT NULL AND TRIM(speaker_id) != ''
        """)
        for r in cursor.fetchall():
            spk_key = normalize_speaker_key(r["speaker_id"])
            v_id = str(r["video_id"])
            dur = int(r["duration_seconds"] or 0)

            if not v_id.startswith("REC_"):
                cursor.execute("SELECT id FROM records WHERE platform_item_id = %s OR CAST(id AS TEXT) = %s", (v_id, v_id))
                if not cursor.fetchone():
                    if spk_key in speaker_map:
                        speaker_map[spk_key]["accepted_seconds"] += dur
                        speaker_map[spk_key]["record_count"] += 1
                    else:
                        speaker_map[spk_key] = {
                            "speaker_id": spk_key,
                            "speaker_name": spk_key,
                            "accepted_seconds": dur,
                            "record_count": 1
                        }

        results = []
        for spk_key in sorted(speaker_map.keys()):
            data = speaker_map[spk_key]
            sec = data["accepted_seconds"]
            remaining_sec = max(0, MAX_SPEAKER_SECONDS - sec)
            pct = round((sec / MAX_SPEAKER_SECONDS) * 100.0, 1) if MAX_SPEAKER_SECONDS > 0 else 0.0

            results.append({
                "speaker_id": data["speaker_id"],
                "speaker_name": data["speaker_name"],
                "accepted_seconds": sec,
                "max_seconds": MAX_SPEAKER_SECONDS,
                "remaining_seconds": remaining_sec,
                "percentage_used": pct,
                "record_count": data["record_count"],
                "is_capped": sec >= MAX_SPEAKER_SECONDS,
                "formatted_accepted": format_seconds(sec),
                "formatted_remaining": format_seconds(remaining_sec)
            })

        return results
    finally:
        if close_after:
            conn.close()
