"""
Pipeline Service: Orchestrates audio endpoint processing for the Audio Research Workflow.

Responsibilities:
1. Sends a downloaded WAV file to the processing server (http://111.118.177.236:8008)
   via /run-pipeline/stream (Server-Sent Events for live progress).
2. Downloads and auto-unpacks the resulting ZIP into static/media/segments/{record_id}/.
3. Parses the metadata CSV produced by the server.
4. Computes per-segment WAV durations using the wave module.
5. Persists all segments into the audio_segments SQLite table (status=PENDING).
6. Updates parent record status to VALIDATION_READY.

Progress state machine:
  IDLE -> DOWNLOADING -> PROCESSING -> EXTRACTING -> VALIDATION_READY | FAILED
"""

import asyncio
import csv
import io
import json
import logging
import re
import time
import wave
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from backend.database import get_connection, duration_to_seconds, format_duration

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
PROCESSING_SERVER_URL = "http://111.118.177.236:8008"
DENOISE_MODEL        = "bs_roformer"
DENOISE_CHECKPOINT   = "1"
DEEPFILTER_OPTION    = "2"
ASR_MODEL            = "openai-whisper-large-v3"
LANGUAGE             = "english"
MAX_CONCURRENT       = 3

BASE_DIR             = Path(__file__).parent.parent
SEGMENTS_DIR         = BASE_DIR / "static" / "media" / "segments"
SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job progress store  {record_id: dict}
_pipeline_jobs: Dict[int, Dict[str, Any]] = {}


# ── Database helpers ──────────────────────────────────────────────────────────

def ensure_audio_segments_table():
    """Idempotently creates/migrates the pipeline_jobs table."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_jobs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id            INTEGER NOT NULL UNIQUE,
            job_id               TEXT DEFAULT '',
            file_name            TEXT DEFAULT '',
            audio_link           TEXT DEFAULT '',
            speaker_id           TEXT DEFAULT '',
            speaker_name         TEXT DEFAULT '',
            gender               TEXT DEFAULT 'auto',
            denoise_model        TEXT DEFAULT 'BS-Roformer',
            asr_model            TEXT DEFAULT 'Whisper Large-v3',
            language             TEXT DEFAULT 'English',
            stage                TEXT DEFAULT 'QUEUED',
            step_id              TEXT DEFAULT 'validate',
            step_index           INTEGER DEFAULT 0,
            total_steps          INTEGER DEFAULT 6,
            step_name            TEXT DEFAULT 'Initializing',
            progress_pct         INTEGER DEFAULT 0,
            started_at           TEXT,
            started_at_display   TEXT,
            finished_at          TEXT,
            elapsed_seconds      INTEGER DEFAULT 0,
            took_formatted       TEXT DEFAULT '',
            segments_count       INTEGER DEFAULT 0,
            error                TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL,
            FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pj_record_id ON pipeline_jobs(record_id);
        CREATE INDEX IF NOT EXISTS idx_pj_stage     ON pipeline_jobs(stage);
    """)
    conn.commit()
    conn.close()


def format_took(seconds: int) -> str:
    """Format elapsed seconds to 'Took Xm Ys' or 'Took Xh Ym Zs'."""
    s = max(0, int(seconds or 0))
    if s >= 3600:
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        return f"Took {h}h {m}m {sec}s"
    m = s // 60
    sec = s % 60
    return f"Took {m}m {sec}s"


def format_job_timestamp(dt: Optional[datetime] = None) -> str:
    """Format timestamp like 'Oct 22, 12:36 PM'."""
    d = dt or datetime.now()
    return d.strftime("%b %d, %I:%M %p")


def upsert_pipeline_job(record_id: int, **fields) -> Dict[str, Any]:
    """Persists job update to SQLite and updates in-memory cache."""
    conn = get_connection()
    cursor = conn.cursor()
    now_iso = datetime.now().isoformat()
    fields["updated_at"] = now_iso

    cursor.execute("SELECT * FROM pipeline_jobs WHERE record_id = ?", (record_id,))
    existing = cursor.fetchone()

    if existing:
        set_cols = ", ".join(f"{k} = ?" for k in fields)
        cursor.execute(
            f"UPDATE pipeline_jobs SET {set_cols} WHERE record_id = ?",
            list(fields.values()) + [record_id]
        )
    else:
        fields["record_id"] = record_id
        if "created_at" not in fields:
            fields["created_at"] = now_iso
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        cursor.execute(
            f"INSERT INTO pipeline_jobs ({cols}) VALUES ({placeholders})",
            list(fields.values())
        )

    conn.commit()
    cursor.execute("SELECT * FROM pipeline_jobs WHERE record_id = ?", (record_id,))
    row = cursor.fetchone()
    conn.close()

    job_dict = dict(row) if row else fields
    _pipeline_jobs[record_id] = job_dict
    return job_dict


def get_all_pipeline_jobs() -> List[Dict[str, Any]]:
    """Returns all pipeline jobs ordered by active jobs first, then updated_at DESC."""
    ensure_audio_segments_table()
    conn = get_connection()
    cursor = conn.cursor()

    # Auto-sync: Discover any records with pipeline statuses that don't have a row in pipeline_jobs yet
    cursor.execute("""
        SELECT r.id, r.status, r.audio_link, r.speakers_id, r.speaker_channel_name,
               r.gender, r.description, r.platform_item_id, r.updated_at, r.created_at
        FROM records r
        LEFT JOIN pipeline_jobs pj ON r.id = pj.record_id
        WHERE pj.id IS NULL AND r.status IN ('PIPELINE_PROCESSING', 'VALIDATION_READY', 'COMPLETED')
    """)
    orphan_records = cursor.fetchall()
    now_iso = datetime.now().isoformat()
    now_display = format_job_timestamp(datetime.now())

    for rec in orphan_records:
        r = dict(rec)
        rec_id = r["id"]
        status = r["status"]
        gender = (r.get("gender") or "auto").lower()
        gen = gender[0].upper() if gender else "U"
        item_id = r.get("platform_item_id") or ""
        desc = (r.get("description") or "").strip()
        if item_id:
            file_name = f"{item_id}_{gen}.wav"
        elif desc:
            clean_desc = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in desc[:35]).strip("_")
            file_name = f"{clean_desc}_{gen}.wav"
        else:
            file_name = f"audio_rec_{rec_id}_{gen}.wav"

        stage = "PROCESSING" if status == "PIPELINE_PROCESSING" else "DONE"
        pct = 25 if stage == "PROCESSING" else 100
        step_id = "validate" if stage == "PROCESSING" else "zip"
        step_index = 1 if stage == "PROCESSING" else 5
        step_name = "Processing in background..." if stage == "PROCESSING" else "Finished"

        seg_cnt = 0

        cursor.execute("""
            INSERT OR REPLACE INTO pipeline_jobs
            (record_id, job_id, file_name, audio_link, speaker_id, speaker_name,
             gender, denoise_model, asr_model, language, stage, step_id, step_index,
             total_steps, step_name, progress_pct, started_at, started_at_display,
             finished_at, elapsed_seconds, took_formatted, segments_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec_id, f"job_{rec_id:04d}", file_name, r.get("audio_link") or "",
            r.get("speakers_id") or "", r.get("speaker_channel_name") or "",
            gender, "BS-Roformer", "Whisper Large-v3", "English",
            stage, step_id, step_index, 6, step_name, pct,
            r.get("updated_at") or now_iso, now_display,
            now_iso if stage == "DONE" else None, 0, "", seg_cnt,
            r.get("created_at") or now_iso, now_iso
        ))
    if orphan_records:
        conn.commit()

    cursor.execute("""
        SELECT pj.*, r.status as record_status, r.description as record_description,
               r.before_processing_duration, r.after_processing_duration
        FROM pipeline_jobs pj
        LEFT JOIN records r ON pj.record_id = r.id
        ORDER BY 
            CASE pj.stage 
                WHEN 'PROCESSING' THEN 1 
                WHEN 'DOWNLOADING' THEN 2 
                WHEN 'EXTRACTING' THEN 3 
                ELSE 4 
            END,
            pj.updated_at DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_record(record_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_record_status(record_id: int, status: str, extra: Optional[Dict] = None):
    conn = get_connection()
    cursor = conn.cursor()
    updates = {"status": status, "updated_at": datetime.now().isoformat()}
    if extra:
        updates.update(extra)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    cursor.execute(
        f"UPDATE records SET {set_clause} WHERE id = ?",
        list(updates.values()) + [record_id]
    )
    conn.commit()
    conn.close()


def get_wav_duration(wav_path: str) -> float:
    try:
        with wave.open(wav_path, "r") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


def get_segments(record_id: int) -> List[Dict[str, Any]]:
    return []


def validate_segment(
    segment_id: int,
    status: str,
    transcription: Optional[str],
    rejection_reason: Optional[str],
    validated_by: Optional[str]
) -> Dict[str, Any]:
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    fields: Dict[str, Any] = {"validation_status": status, "validated_at": now}
    if validated_by:
        fields["validated_by"] = validated_by
    if transcription is not None:
        fields["transcription"] = transcription
    if rejection_reason is not None:
        fields["rejection_reason"] = rejection_reason

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    cursor.execute(
        f"UPDATE audio_segments SET {set_clause} WHERE id = ?",
        list(fields.values()) + [segment_id]
    )
    conn.commit()
    cursor.execute("SELECT * FROM audio_segments WHERE id = ?", (segment_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}


def finalize_record_validation(record_id: int, finalized_by: str) -> Dict[str, Any]:
    """
    Locks in accepted segments, recomputes totals, updates the parent record.
    Returns summary dict with accepted_count, total_accepted_seconds, etc.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Accepted segments only
    cursor.execute(
        """SELECT duration_seconds FROM audio_segments
           WHERE record_id = ? AND validation_status = 'ACCEPTED'""",
        (record_id,)
    )
    accepted = cursor.fetchall()
    accepted_count = len(accepted)
    total_seconds = sum(float(r["duration_seconds"] or 0) for r in accepted)

    # Total segments
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM audio_segments WHERE record_id = ?",
        (record_id,)
    )
    total_segs = cursor.fetchone()["cnt"]

    h = int(total_seconds) // 3600
    m = (int(total_seconds) % 3600) // 60
    s = int(total_seconds) % 60
    after_dur = f"{h:02d}:{m:02d}:{s:02d}"

    now = datetime.now().isoformat()
    today = datetime.now().strftime("%d%m%y")

    cursor.execute("""
        UPDATE records
        SET after_processing_duration = ?,
            total_files = ?,
            completion_date = ?,
            data_available = 'Processed & Segmented',
            status = 'COMPLETED',
            revalidated_by = ?,
            updated_at = ?
        WHERE id = ?
    """, (after_dur, str(accepted_count), today, finalized_by, now, record_id))
    conn.commit()
    conn.close()

    return {
        "record_id": record_id,
        "total_segments": total_segs,
        "accepted_count": accepted_count,
        "rejected_count": total_segs - accepted_count,
        "after_processing_duration": after_dur,
        "total_accepted_seconds": total_seconds,
        "completion_date": today,
        "status": "COMPLETED"
    }


# ── Job Progress Store ────────────────────────────────────────────────────────

def _set_job(record_id: int, **kwargs):
    """Updates in-memory store and SQLite table."""
    job = _pipeline_jobs.setdefault(record_id, {"record_id": record_id})
    job.update(kwargs)
    job["updated_at"] = datetime.now().isoformat()
    try:
        upsert_pipeline_job(record_id, **kwargs)
    except Exception as exc:
        logger.warning(f"Failed to upsert job {record_id} to DB: {exc}")


def get_job_status(record_id: int) -> Dict[str, Any]:
    ensure_audio_segments_table()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT pj.*, r.status as record_status, r.description as record_description,
               r.before_processing_duration, r.after_processing_duration
        FROM pipeline_jobs pj
        LEFT JOIN records r ON pj.record_id = r.id
        WHERE pj.record_id = ?
    """, (record_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        d = dict(row)
        _pipeline_jobs[record_id] = d
        return d

    return _pipeline_jobs.get(record_id, {
        "record_id": record_id,
        "stage": "IDLE",
        "progress_pct": 0,
        "step_name": "Not started",
        "step_index": 0,
        "total_steps": 6,
        "error": None
    })


# ── Endpoint Processing ───────────────────────────────────────────────────────

async def run_pipeline_for_record(
    record_id: int,
    wav_path: str,
    gender: str = "auto",
    language: str = "english",
    start_time: Optional[float] = None
) -> Dict[str, Any]:
    """
    Sends a local WAV file to the processing server /run-pipeline/stream endpoint.
    Streams SSE events for live progress updates, then downloads and unpacks the result.

    Returns dict with keys: success, segments_dir, segments (list), error.
    """
    ensure_audio_segments_table()
    record_out_dir = SEGMENTS_DIR / str(record_id)
    record_out_dir.mkdir(parents=True, exist_ok=True)

    t0 = start_time or time.time()
    elapsed = int(time.time() - t0)

    _set_job(
        record_id,
        stage="PROCESSING",
        step_id="validate",
        step_index=0,
        total_steps=6,
        progress_pct=15,
        step_name="Connecting to processing server (111.118.177.236:8008)...",
        elapsed_seconds=elapsed,
        took_formatted=format_took(elapsed)
    )

    upload_name = Path(wav_path).name
    final_status = None
    final_error = None
    job_id = ""

    async with httpx.AsyncClient(base_url=PROCESSING_SERVER_URL, timeout=None) as client:
        # ── Step 1: Health check ──────────────────────────────────────────
        try:
            resp = await client.get("/pipeline/status", timeout=15)
            if resp.status_code >= 500:
                raise RuntimeError(f"Server returned {resp.status_code}")
        except Exception as exc:
            elapsed = int(time.time() - t0)
            _set_job(record_id, stage="FAILED", error=f"Server unreachable: {exc}",
                     step_name=f"Connection failed: {exc}", elapsed_seconds=elapsed,
                     took_formatted=format_took(elapsed))
            update_record_status(record_id, "PIPELINE_FAILED")
            return {"success": False, "error": str(exc)}

        # ── Step 2: Stream pipeline ───────────────────────────────────────
        elapsed = int(time.time() - t0)
        _set_job(
            record_id,
            stage="PROCESSING",
            step_id="validate",
            step_index=0,
            progress_pct=18,
            step_name="Uploading audio to processing server...",
            elapsed_seconds=elapsed,
            took_formatted=format_took(elapsed)
        )

        try:
            with open(wav_path, "rb") as audio_file:
                async with client.stream(
                    "POST",
                    "/run-pipeline/stream",
                    files={"file": (upload_name, audio_file, "audio/wav")},
                    data={
                        "denoise_model": DENOISE_MODEL,
                        "denoise_checkpoint": DENOISE_CHECKPOINT,
                        "asr_model": ASR_MODEL,
                        "language": language,
                        "gender": gender,
                    }
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise RuntimeError(
                            f"Pipeline stream error {resp.status_code}: {body[:300]!r}"
                        )

                    job_id = resp.headers.get("x-job-id", "")
                    if job_id:
                        _set_job(record_id, job_id=job_id)

                    last_progress_msg = ""

                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except Exception:
                            continue

                        etype = event.get("type")
                        if etype == "log":
                            log_text = (event.get("line") or "").strip()
                            if log_text:
                                print(f"[{upload_name}] {log_text}", flush=True)
                                # Check for segment transcription progress, e.g. " 14/20 [00:44" or " 1/32 ["
                                m_seg = re.search(r"(\d+)/(\d+)\s*\[", log_text)
                                if m_seg:
                                    try:
                                        c_seg, t_seg = int(m_seg.group(1)), int(m_seg.group(2))
                                        if 0 < c_seg <= t_seg:
                                            cur_elapsed = int(time.time() - t0)
                                            sub_pct = min(92, 75 + int((c_seg / t_seg) * 17))
                                            sub_name = f"Transcribing segment {c_seg}/{t_seg}..."
                                            _set_job(
                                                record_id,
                                                stage="PROCESSING",
                                                step_id="asr",
                                                step_index=4,
                                                progress_pct=sub_pct,
                                                step_name=sub_name,
                                                elapsed_seconds=cur_elapsed,
                                                took_formatted=format_took(cur_elapsed)
                                            )
                                    except Exception:
                                        pass
                        elif etype == "progress":
                            job_id = event.get("job_id") or job_id
                            final_status = event.get("status")
                            final_error = event.get("error")
                            pct = max(10, min(92, int(event.get("pct", 0))))
                            step_idx = int(event.get("step_index", 0))
                            step_id = str(event.get("step_id", ""))
                            step_name = event.get("step_name") or f"Processing step {step_idx + 1}/6"
                            cur_elapsed = int(time.time() - t0)

                            msg = f"{step_name} ({pct}%)"
                            if msg != last_progress_msg:
                                print(f"[{upload_name}] [{pct}%] Step {step_idx + 1}/6: {step_name}", flush=True)
                                _set_job(
                                    record_id,
                                    job_id=job_id,
                                    stage="PROCESSING",
                                    step_id=step_id or "processing",
                                    step_index=step_idx,
                                    total_steps=6,
                                    progress_pct=pct,
                                    step_name=step_name,
                                    elapsed_seconds=cur_elapsed,
                                    took_formatted=format_took(cur_elapsed)
                                )
                                last_progress_msg = msg
                        elif etype == "done":
                            print(f"[{upload_name}] Processing complete on server -- downloading archive...", flush=True)
                            break

        except Exception as exc:
            cur_elapsed = int(time.time() - t0)
            _set_job(record_id, stage="FAILED", error=str(exc),
                     step_name=f"Stream error: {exc}", elapsed_seconds=cur_elapsed,
                     took_formatted=format_took(cur_elapsed))
            update_record_status(record_id, "PIPELINE_FAILED")
            return {"success": False, "error": str(exc)}

        cur_elapsed = int(time.time() - t0)
        if final_status == "error":
            err = f"Pipeline server-side error: {final_error}"
            _set_job(record_id, stage="FAILED", error=err, step_name=err,
                     elapsed_seconds=cur_elapsed, took_formatted=format_took(cur_elapsed))
            update_record_status(record_id, "PIPELINE_FAILED")
            return {"success": False, "error": err}

        if final_status == "cancelled":
            err = "Pipeline was cancelled on server."
            _set_job(record_id, stage="FAILED", error=err, step_name=err,
                     elapsed_seconds=cur_elapsed, took_formatted=format_took(cur_elapsed))
            update_record_status(record_id, "PIPELINE_FAILED")
            return {"success": False, "error": err}

        # ── Step 3: Download result ZIP ───────────────────────────────────
        cur_elapsed = int(time.time() - t0)
        _set_job(
            record_id,
            stage="EXTRACTING",
            step_id="zip",
            step_index=5,
            progress_pct=93,
            step_name="Packing Output ZIP / Downloading result...",
            elapsed_seconds=cur_elapsed,
            took_formatted=format_took(cur_elapsed)
        )

        params = {"job_id": job_id} if job_id else None
        try:
            async with client.stream("GET", "/pipeline/download", params=params) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"Download failed {resp.status_code}: {body[:300]!r}")

                raw_bytes = b""
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    raw_bytes += chunk
        except Exception as exc:
            cur_elapsed = int(time.time() - t0)
            _set_job(record_id, stage="FAILED", error=str(exc), step_name=f"Result download error: {exc}",
                     elapsed_seconds=cur_elapsed, took_formatted=format_took(cur_elapsed))
            update_record_status(record_id, "PIPELINE_FAILED")
            return {"success": False, "error": str(exc)}

        # ── Step 4: Unpack ZIP ────────────────────────────────────────────
        cur_elapsed = int(time.time() - t0)
        _set_job(
            record_id,
            stage="EXTRACTING",
            step_id="zip",
            step_index=5,
            progress_pct=96,
            step_name="Unpacking segments & loading metadata...",
            elapsed_seconds=cur_elapsed,
            took_formatted=format_took(cur_elapsed)
        )

        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                zf.extractall(record_out_dir)
        except zipfile.BadZipFile:
            fallback_wav = record_out_dir / "output.wav"
            fallback_wav.write_bytes(raw_bytes)

    # ── Step 5: Parse metadata CSV & seed DB ─────────────────────────────
    segments = _parse_and_store_segments(record_id, record_out_dir, gender, language)

    # ── Step 6: Mark Done ────────────────────────────────────────────────
    total_elapsed = int(time.time() - t0)
    took_str = format_took(total_elapsed)
    now_iso = datetime.now().isoformat()

    update_record_status(record_id, "VALIDATION_READY")
    _set_job(
        record_id,
        stage="DONE",
        step_id="zip",
        step_index=5,
        total_steps=6,
        progress_pct=100,
        step_name="Finished",
        finished_at=now_iso,
        elapsed_seconds=total_elapsed,
        took_formatted=took_str,
        segments_count=len(segments)
    )

    return {
        "success": True,
        "segments_dir": str(record_out_dir),
        "segments": segments,
        "error": None
    }


def _parse_and_store_segments(
    record_id: int,
    out_dir: Path,
    gender: str,
    language: str
) -> List[Dict[str, Any]]:
    """
    Finds the metadata CSV in out_dir, reads each segment row,
    computes WAV duration, and inserts/replaces rows in audio_segments.
    Returns the list of segment dicts stored.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Clear existing segments for this record (re-processing should refresh them)
    cursor.execute("DELETE FROM audio_segments WHERE record_id = ?", (record_id,))

    # Find metadata CSV (server produces: accepted_metadata.csv or metadata.csv)
    csv_candidates = sorted(out_dir.rglob("*.csv"))
    meta_csv = next(
        (c for c in csv_candidates
         if "metadata" in c.name.lower() and "rejected" not in c.name.lower()
         and "audit" not in c.name.lower()),
        None
    )

    segments = []

    if meta_csv:
        try:
            with open(meta_csv, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for idx, row in enumerate(reader):
                    file_name = row.get("file_name", "").strip()
                    audio_path_raw = row.get("audio_path", "").strip()
                    transcription = row.get("transcription", "").strip()
                    row_lang = row.get("language", language).strip()
                    row_gender = row.get("gender", gender).strip()

                    # Extract the true segment WAV filename
                    # In metadata.csv, audio_path is e.g. "61_m4Q9b4h0jpg/61_m4Q9b4h0jpg_seg_0001.wav"
                    target_seg_name = Path(audio_path_raw).name if audio_path_raw else file_name
                    if not target_seg_name.lower().endswith(".wav"):
                        target_seg_name += ".wav"

                    abs_wav = None
                    # Search specifically for files (not directories) matching target_seg_name
                    file_matches = [f for f in out_dir.rglob(target_seg_name) if f.is_file()]
                    if file_matches:
                        abs_wav = file_matches[0]
                    elif (out_dir / "segmented" / audio_path_raw).is_file():
                        abs_wav = out_dir / "segmented" / audio_path_raw
                    elif (out_dir / audio_path_raw).is_file():
                        abs_wav = out_dir / audio_path_raw
                    else:
                        abs_wav = out_dir / "segmented" / target_seg_name

                    rel_url = (
                        f"/static/media/segments/{record_id}/"
                        + abs_wav.relative_to(out_dir).as_posix()
                        if abs_wav.exists() and abs_wav.is_file() else
                        f"/static/media/segments/{record_id}/segmented/{target_seg_name}"
                    )

                    dur = get_wav_duration(str(abs_wav)) if (abs_wav.exists() and abs_wav.is_file()) else 0.0
                    display_name = abs_wav.name if (abs_wav.exists() and abs_wav.is_file()) else target_seg_name

                    now = datetime.now().isoformat()
                    cursor.execute("""
                        INSERT INTO audio_segments
                        (record_id, segment_index, file_name, audio_path, relative_url,
                         transcription, original_transcription, duration_seconds,
                         language, gender, validation_status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                    """, (
                        record_id, idx,
                        display_name, str(abs_wav), rel_url,
                        transcription, transcription,
                        dur, row_lang, row_gender, now
                    ))
                    seg_id = cursor.lastrowid
                    segments.append({
                        "id": seg_id,
                        "record_id": record_id,
                        "segment_index": idx,
                        "file_name": display_name,
                        "audio_path": str(abs_wav),
                        "relative_url": rel_url,
                        "transcription": transcription,
                        "original_transcription": transcription,
                        "duration_seconds": dur,
                        "language": row_lang,
                        "gender": row_gender,
                        "validation_status": "PENDING",
                        "created_at": now
                    })
        except Exception as exc:
            logger.error(f"[Record {record_id}] Failed to parse metadata CSV: {exc}")
    else:
        # No CSV found — index every WAV file as a segment with empty transcript
        wav_files = sorted(out_dir.rglob("*.wav"))
        logger.warning(
            f"[Record {record_id}] No metadata CSV found; indexing {len(wav_files)} WAV(s) directly"
        )
        for idx, wav_file in enumerate(wav_files):
            rel_url = (
                f"/static/media/segments/{record_id}/"
                + wav_file.relative_to(out_dir).as_posix()
            )
            dur = get_wav_duration(str(wav_file))
            now = datetime.now().isoformat()
            cursor.execute("""
                INSERT INTO audio_segments
                (record_id, segment_index, file_name, audio_path, relative_url,
                 transcription, original_transcription, duration_seconds,
                 language, gender, validation_status, created_at)
                VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, 'PENDING', ?)
            """, (
                record_id, idx, wav_file.name, str(wav_file), rel_url,
                dur, language, gender, now
            ))
            seg_id = cursor.lastrowid
            segments.append({
                "id": seg_id,
                "record_id": record_id,
                "segment_index": idx,
                "file_name": wav_file.name,
                "audio_path": str(wav_file),
                "relative_url": rel_url,
                "transcription": "",
                "original_transcription": "",
                "duration_seconds": dur,
                "language": language,
                "gender": gender,
                "validation_status": "PENDING",
                "created_at": now
            })

    conn.commit()
    conn.close()
    return segments


# ── Full orchestration (Download + Process) ───────────────────────────────────

async def run_full_pipeline(record_id: int, force_redownload: bool = False) -> Dict[str, Any]:
    """
    Full end-to-end pipeline for a single approved record:
      1. Load record from DB.
      2. Download audio via audio_downloader.download_audio().
      3. Send to processing server and unpack segments.
      4. Store segments in DB with PENDING status.
      5. Update record to VALIDATION_READY.

    Progress is tracked in _pipeline_jobs[record_id] for polling.
    """
    ensure_audio_segments_table()
    record = get_record(record_id)
    if not record:
        return {"success": False, "error": f"Record {record_id} not found"}

    url = record.get("audio_link", "")
    if not url:
        return {"success": False, "error": "Record has no audio_link"}

    speaker_id = record.get("speakers_id") or record.get("speaker_channel_name") or f"spk_{record_id}"
    gender = (record.get("gender") or "auto").lower()
    gender = gender if gender in ("male", "female") else "auto"
    t0 = time.time()
    dt_now = datetime.now()
    desc = (record.get("description") or "").strip()
    spk = (record.get("speakers_id") or "").strip()
    gen = gender[0].upper() if gender else "U"
    item_id = record.get("platform_item_id") or ""
    language = "english"
    if item_id:
        file_name = f"{item_id}_{gen}.wav"
    elif desc:
        clean_desc = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in desc[:35]).strip("_")
        file_name = f"{clean_desc}_{gen}.wav"
    else:
        file_name = f"audio_rec_{record_id}_{gen}.wav"

    speaker_name = record.get("speaker_channel_name") or record.get("speakers_id") or ""

    # ── Stage 1: Download ─────────────────────────────────────────────────
    _set_job(
        record_id,
        file_name=file_name,
        audio_link=url,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        gender=gender,
        denoise_model="BS-Roformer",
        asr_model="Whisper Large-v3",
        language="English",
        stage="DOWNLOADING",
        step_id="validate",
        step_index=0,
        total_steps=6,
        progress_pct=5,
        step_name="Downloading source audio from URL...",
        started_at=dt_now.isoformat(),
        started_at_display=format_job_timestamp(dt_now),
        elapsed_seconds=0,
        took_formatted="0s",
        error=None
    )
    update_record_status(record_id, "PIPELINE_PROCESSING")

    from backend.audio_downloader import download_audio
    dl_result = download_audio(
        url=url,
        record_id=record_id,
        speaker_id=speaker_id,
        gender=gender,
        item_id=item_id,
        force_redownload=force_redownload
    )

    if not dl_result["success"]:
        err = f"Download failed: {dl_result['error']}"
        elapsed = int(time.time() - t0)
        _set_job(record_id, stage="FAILED", error=err, step_name=err,
                 elapsed_seconds=elapsed, took_formatted=format_took(elapsed))
        update_record_status(record_id, "PIPELINE_FAILED")
        return {"success": False, "error": err}

    elapsed = int(time.time() - t0)
    _set_job(
        record_id,
        stage="PROCESSING",
        step_id="validate",
        step_index=0,
        progress_pct=15,
        step_name="Download complete — sending to processing server...",
        elapsed_seconds=elapsed,
        took_formatted=format_took(elapsed)
    )

    wav_path = dl_result["file_path"]

    # ── Stage 2: Process on remote server ─────────────────────────────────
    result = await run_pipeline_for_record(
        record_id=record_id,
        wav_path=wav_path,
        gender=gender,
        language=language,
        start_time=t0
    )

    return result
