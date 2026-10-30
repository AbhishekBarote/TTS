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
    """No-op: single table architecture stores pipeline_job and audio_segments in JSONB."""
    pass


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
    """Persists job update to DB (JSONB column) and updates in-memory cache."""
    conn = get_connection()
    cursor = conn.cursor()
    now_iso = datetime.now().isoformat()
    fields["updated_at"] = now_iso
    fields["record_id"] = record_id

    cursor.execute("SELECT pipeline_job FROM indian_english_records WHERE id = %s", (record_id,))
    row = cursor.fetchone()
    current_job = {}
    if row and row["pipeline_job"]:
        current_job = row["pipeline_job"] if isinstance(row["pipeline_job"], dict) else json.loads(row["pipeline_job"])

    if "created_at" not in current_job and "created_at" not in fields:
        fields["created_at"] = now_iso

    current_job.update(fields)
    cursor.execute(
        "UPDATE indian_english_records SET pipeline_job = %s WHERE id = %s",
        (json.dumps(current_job), record_id)
    )
    conn.commit()
    conn.close()

    _pipeline_jobs[record_id] = current_job
    return current_job


def get_all_pipeline_jobs() -> List[Dict[str, Any]]:
    """Returns all pipeline jobs ordered by active jobs first, then updated_at DESC."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, pipeline_job, status as record_status, description as record_description,
               before_processing_duration, after_processing_duration, updated_at
        FROM indian_english_records
        WHERE pipeline_job IS NOT NULL
    """)
    rows = cursor.fetchall()
    conn.close()

    job_list = []
    for r in rows:
        job_data = r["pipeline_job"]
        if isinstance(job_data, str):
            try:
                job_data = json.loads(job_data)
            except Exception:
                job_data = {}
        if not isinstance(job_data, dict):
            continue
        job_data["record_id"] = r["id"]
        job_data["record_status"] = r["record_status"]
        job_data["record_description"] = r["record_description"]
        job_data["before_processing_duration"] = r["before_processing_duration"]
        job_data["after_processing_duration"] = r["after_processing_duration"]
        job_list.append(job_data)

    stage_order = {'PROCESSING': 1, 'DOWNLOADING': 2, 'EXTRACTING': 3}
    job_list.sort(key=lambda j: (stage_order.get(j.get("stage"), 4), str(j.get("updated_at", ""))), reverse=False)
    return job_list


def get_record(record_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM indian_english_records WHERE id = %s", (record_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_record_status(record_id: int, status: str, extra: Optional[Dict] = None):
    conn = get_connection()
    cursor = conn.cursor()
    updates = {"status": status, "updated_at": datetime.now().isoformat()}
    if extra:
        updates.update(extra)
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    cursor.execute(
        f"UPDATE indian_english_records SET {set_clause} WHERE id = %s",
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT audio_segments FROM indian_english_records WHERE id = %s", (record_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row["audio_segments"]:
        return []
    segs = row["audio_segments"]
    if isinstance(segs, str):
        segs = json.loads(segs)
    return segs if isinstance(segs, list) else []


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

    target_rec_id = segment_id // 10000 if segment_id > 10000 else None
    if target_rec_id:
        cursor.execute("SELECT id, audio_segments FROM indian_english_records WHERE id = %s", (target_rec_id,))
        rows = cursor.fetchall()
    else:
        cursor.execute("SELECT id, audio_segments FROM indian_english_records WHERE audio_segments != '[]'::jsonb")
        rows = cursor.fetchall()

    target_segment = None
    for r in rows:
        rec_id = r["id"]
        segs = r["audio_segments"]
        if isinstance(segs, str):
            segs = json.loads(segs)
        if not isinstance(segs, list):
            continue

        updated = False
        for s in segs:
            if s.get("id") == segment_id:
                s["validation_status"] = status
                s["validated_at"] = now
                if validated_by:
                    s["validated_by"] = validated_by
                if transcription is not None:
                    s["transcription"] = transcription
                if rejection_reason is not None:
                    s["rejection_reason"] = rejection_reason
                target_segment = dict(s)
                updated = True
                break
        if updated:
            cursor.execute(
                "UPDATE indian_english_records SET audio_segments = %s WHERE id = %s",
                (json.dumps(segs), rec_id)
            )
            conn.commit()
            break

    conn.close()
    return target_segment or {}


def finalize_record_validation(record_id: int, finalized_by: str) -> Dict[str, Any]:
    """
    Locks in accepted segments, recomputes totals, updates the parent record.
    Returns summary dict with accepted_count, total_accepted_seconds, etc.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT audio_segments FROM indian_english_records WHERE id = %s", (record_id,))
    row = cursor.fetchone()
    segs = []
    if row and row["audio_segments"]:
        raw_segs = row["audio_segments"]
        segs = raw_segs if isinstance(raw_segs, list) else json.loads(raw_segs)

    total_segs = len(segs)
    accepted = [s for s in segs if s.get("validation_status") == "ACCEPTED"]
    accepted_count = len(accepted)
    total_seconds = sum(float(s.get("duration_seconds") or 0) for s in accepted)

    h = int(total_seconds) // 3600
    m = (int(total_seconds) % 3600) // 60
    s = int(total_seconds) % 60
    after_dur = f"{h:02d}:{m:02d}:{s:02d}"

    now = datetime.now().isoformat()
    today = datetime.now().strftime("%d%m%y")

    cursor.execute("""
        UPDATE indian_english_records
        SET after_processing_duration = %s,
            total_files = %s,
            completion_date = %s,
            data_available = 'Processed & Segmented',
            status = 'COMPLETED',
            revalidated_by = %s,
            updated_at = %s
        WHERE id = %s
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


def _set_job(record_id: int, **kwargs):
    """Updates in-memory store and DB column."""
    job = _pipeline_jobs.setdefault(record_id, {"record_id": record_id})
    job.update(kwargs)
    job["updated_at"] = datetime.now().isoformat()
    try:
        upsert_pipeline_job(record_id, **kwargs)
    except Exception as exc:
        logger.warning(f"Failed to upsert job {record_id} to DB: {exc}")


def get_job_status(record_id: int) -> Dict[str, Any]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT pipeline_job, status as record_status, description as record_description,
               before_processing_duration, after_processing_duration
        FROM indian_english_records
        WHERE id = %s
    """, (record_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row["pipeline_job"]:
        job_data = row["pipeline_job"]
        if isinstance(job_data, str):
            job_data = json.loads(job_data)
        if isinstance(job_data, dict):
            job_data["record_status"] = row["record_status"]
            job_data["record_description"] = row["record_description"]
            job_data["before_processing_duration"] = row["before_processing_duration"]
            job_data["after_processing_duration"] = row["after_processing_duration"]
            _pipeline_jobs[record_id] = job_data
            return job_data

    return _pipeline_jobs.get(record_id, {
        "record_id": record_id,
        "stage": "IDLE",
        "progress_pct": 0,
        "step_name": "Not started",
        "step_index": 0,
        "total_steps": 6,
        "error": None
    })


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
    computes WAV duration, and stores segments as JSONB in indian_english_records.
    Returns the list of segment dicts stored.
    """
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

                    target_seg_name = Path(audio_path_raw).name if audio_path_raw else file_name
                    if not target_seg_name.lower().endswith(".wav"):
                        target_seg_name += ".wav"

                    abs_wav = None
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
                    seg_id = (record_id * 10000) + (idx + 1)
                    now = datetime.now().isoformat()

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
            seg_id = (record_id * 10000) + (idx + 1)
            now = datetime.now().isoformat()

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

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE indian_english_records SET audio_segments = %s WHERE id = %s",
        (json.dumps(segments), record_id)
    )
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
