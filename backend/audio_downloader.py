"""
Audio Downloader Engine for the Centralized Audio Research & Revalidation Workflow.
Downloads audio from YouTube, Internet Archive, Vimeo, SoundCloud, and direct URLs
using yt-dlp, then converts to standardized 16kHz mono WAV via ffmpeg.

Features:
- Idempotent: skips re-download if valid WAV already exists
- Automatic gender/speaker folder organization
- Returns download metadata (duration_seconds, file_size, format)
- Retries on transient network errors with exponential backoff
"""

import os
import re
import time
import wave
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Root directory for raw downloaded audio
BASE_DIR = Path(__file__).parent.parent
RAW_AUDIO_DIR = BASE_DIR / "static" / "media" / "raw"
RAW_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_CMD = "ffmpeg"   # assumes ffmpeg is on PATH
SAMPLE_RATE = 16000      # 16kHz mono WAV – optimal for Whisper ASR and TTS training
MAX_RETRIES = 3
RETRY_DELAY = 5          # seconds between retry attempts


def sanitize_name(name: str) -> str:
    """Sanitize a string for use as a filename component."""
    name = re.sub(r"\s+", "_", str(name).strip())
    name = re.sub(r"[^A-Za-z0-9_\-]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "audio"


def get_wav_duration(wav_path: str) -> float:
    """Returns exact duration of a WAV file in seconds."""
    try:
        with wave.open(wav_path, "r") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate) if rate > 0 else 0.0
    except Exception:
        return 0.0


def build_output_path(record_id: int, speaker_id: str, gender: str, item_id: str = "") -> Path:
    """Constructs the standardized output WAV path.
    Pattern: static/media/raw/{gender}/{speaker_safe}/{record_id}_{item_id}.wav
    """
    gender_folder = gender.lower() if gender.lower() in ("male", "female") else "unknown"
    speaker_folder = sanitize_name(speaker_id) or f"record_{record_id}"
    stem = f"{record_id}_{sanitize_name(item_id)}" if item_id else str(record_id)
    path = RAW_AUDIO_DIR / gender_folder / speaker_folder / f"{stem}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def convert_to_wav(input_path: str, output_path: str) -> bool:
    """Converts any audio format to 16kHz mono WAV using ffmpeg.
    Returns True on success, False on failure.
    """
    cmd = [
        FFMPEG_CMD, "-y",
        "-i", input_path,
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(output_path)
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            logger.error(f"ffmpeg conversion failed: {result.stderr[-500:]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg conversion timed out after 600s")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH – please install ffmpeg")
        return False


def download_audio(
    url: str,
    record_id: int,
    speaker_id: str,
    gender: str,
    item_id: str = "",
    force_redownload: bool = False
) -> Dict[str, Any]:
    """
    Downloads audio from a URL (YouTube, Archive.org, Vimeo, SoundCloud, direct links)
    and converts it to a standardized 16kHz mono WAV file.

    Args:
        url: Source URL to download from.
        record_id: The database record ID (used for file naming).
        speaker_id: Speaker identifier for folder organization.
        gender: Speaker gender ('male', 'female', 'auto').
        item_id: Platform item ID (YouTube video ID, Archive item ID, etc.).
        force_redownload: If True, re-downloads even if WAV already exists.

    Returns:
        Dict with keys:
            success (bool), file_path (str), duration_seconds (float),
            file_size_bytes (int), error (str or None)
    """
    output_wav = build_output_path(record_id, speaker_id, gender, item_id)

    # Idempotency: skip if valid WAV already present
    if not force_redownload and output_wav.exists() and output_wav.stat().st_size > 1024:
        dur = get_wav_duration(str(output_wav))
        logger.info(f"[Record {record_id}] Audio already downloaded: {output_wav}")
        return {
            "success": True,
            "file_path": str(output_wav),
            "duration_seconds": dur,
            "file_size_bytes": output_wav.stat().st_size,
            "error": None,
            "skipped": True
        }

    # Temporary download location (yt-dlp chooses the container format)
    tmp_dir = RAW_AUDIO_DIR / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_stem = f"tmp_{record_id}_{int(time.time())}"
    tmp_out_template = str(tmp_dir / f"{tmp_stem}.%(ext)s")

    raw_url = str(url or "").strip()
    match = re.search(r'(https?://[^\s]+)', raw_url)
    clean_url = match.group(1) if match else ""
    if not clean_url:
        return {
            "success": False,
            "file_path": None,
            "duration_seconds": 0.0,
            "file_size_bytes": 0,
            "error": f"Invalid or non-HTTP audio URL: {raw_url[:100]}"
        }

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"📥 [Record {record_id}] Downloading audio from URL: {clean_url} (Attempt {attempt}/{MAX_RETRIES})", flush=True)
            # Use yt-dlp CLI to download best audio stream
            import yt_dlp

            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": tmp_out_template,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
                "retries": 2,
                # Post-processing: extract audio to intermediate format
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "wav",
                    "preferredquality": "0",
                }],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=True)
                raw_duration = float(info.get("duration") or 0.0)

            # Find the downloaded file
            downloaded_files = list(tmp_dir.glob(f"{tmp_stem}.*"))
            if not downloaded_files:
                raise RuntimeError("yt-dlp completed but output file not found")

            downloaded_file = downloaded_files[0]

            print(f"🔄 [Record {record_id}] Converting audio to standardized 16kHz mono WAV via ffmpeg...", flush=True)
            # Convert to standardized 16kHz mono WAV
            if downloaded_file.suffix.lower() == ".wav":
                # Re-encode to ensure correct sample rate / channels
                ok = convert_to_wav(str(downloaded_file), str(output_wav))
            else:
                ok = convert_to_wav(str(downloaded_file), str(output_wav))

            # Cleanup tmp file
            try:
                downloaded_file.unlink()
            except Exception:
                pass

            if not ok:
                raise RuntimeError("ffmpeg conversion to 16kHz WAV failed")

            actual_duration = get_wav_duration(str(output_wav)) or raw_duration
            file_size = output_wav.stat().st_size

            print(f"✅ [Record {record_id}] Audio standardized: {output_wav.name} ({actual_duration:.1f}s, {file_size // 1024} KB)", flush=True)
            logger.info(
                f"[Record {record_id}] Downloaded & converted: {output_wav} "
                f"({actual_duration:.1f}s, {file_size // 1024}KB)"
            )
            return {
                "success": True,
                "file_path": str(output_wav),
                "duration_seconds": actual_duration,
                "file_size_bytes": file_size,
                "error": None,
                "skipped": False
            }

        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                f"[Record {record_id}] Download attempt {attempt}/{MAX_RETRIES} failed: {exc}"
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    return {
        "success": False,
        "file_path": None,
        "duration_seconds": 0.0,
        "file_size_bytes": 0,
        "error": last_error,
        "skipped": False
    }


def download_direct_url(url: str, output_path: str) -> Dict[str, Any]:
    """
    Downloads a direct audio URL (e.g., .mp3, .m4a, .wav) using requests
    and converts to 16kHz WAV. Used as fallback for non-platform URLs.
    """
    import requests as _requests

    tmp_path = str(output_path) + ".tmp_download"
    try:
        r = _requests.get(url, stream=True, timeout=(10, 60))
        r.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)

        ok = convert_to_wav(tmp_path, output_path)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        if not ok:
            return {"success": False, "error": "ffmpeg conversion failed"}

        dur = get_wav_duration(output_path)
        return {
            "success": True,
            "file_path": output_path,
            "duration_seconds": dur,
            "file_size_bytes": Path(output_path).stat().st_size,
            "error": None
        }
    except Exception as exc:
        return {"success": False, "file_path": None, "duration_seconds": 0.0,
                "file_size_bytes": 0, "error": str(exc)}
