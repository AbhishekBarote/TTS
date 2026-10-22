import asyncio
import atexit
import csv
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
import httpx

# ============================================================
# CONFIG -- edit these values, then run the script
# ============================================================
BASE_URL = "http://111.118.177.236:8008/"   # <-- put your real server address and port here

# Which endpoint to call. Choose one:
#   "validate-audio", "convert-audio", "denoise", "vad-segment",
#   "asr-transcription", "run-pipeline", "run-pipeline-stream",
#   "status", "logs", "download", "data-structure", "options"
# NOTE: "run-pipeline" and "run-pipeline-stream" both show LIVE logs of every
# step. The single-step endpoints (denoise, asr-transcription, ...) show logs
# only in the server terminal.
ENDPOINT = "run-pipeline"

INPUT_PATH = "/teamspace/studios/this_studio/tts_data_pipeline/data/raw/male"

# Denoise model: "bs_roformer" | "deepfilter" | "htdemucs_ft" | "audio_separator_denoise" | "all"
DENOISE_MODEL = "bs_roformer"

# BS-Roformer checkpoint (REQUIRED when DENOISE_MODEL is "bs_roformer" or "all").
# Use the menu number the server asks for:
#   "1" -> BS-Roformer ep317 (SDR 12.9755)  [model_bs_roformer_ep_317_sdr_12.9755.ckpt]
#   "2" -> BS-Roformer SW                   [BS-Roformer-SW.ckpt]
DENOISE_CHECKPOINT = "1"

# DeepFilterNet version (REQUIRED when DENOISE_MODEL is "deepfilter" or "all"):
#   "1" -> DeepFilterNet v2  (DeepFilterNet2)
#   "2" -> DeepFilterNet v3  (DeepFilterNet3)
DEEPFILTER_OPTION = "2"

# ASR model: "whisper-large-v3-finetuned" | "openai-whisper-large-v3"
ASR_MODEL = "openai-whisper-large-v3"

# Form field names the server reads for the options above.
# CHANGE THESE to match your server's endpoint definitions.
FIELD_DENOISE_CHECKPOINT = "denoise_checkpoint"
FIELD_DEEPFILTER_OPTION = "deepfilter_option"

# Language of the audio to transcribe: "english" | "sanskrit"
LANGUAGE = "english"

# Gender of the speaker: "auto" | "male" | "female"
GENDER = "auto"

OUTPUT_PATH = "/teamspace/studios/this_studio/asr"

# Only for ENDPOINT = "status" / "logs" / "download":
# a specific job id (printed by run-pipeline-stream / X-Job-Id header).
# Leave "" to use the most recently started job on the server.
JOB_ID = ""

# How many files are sent IN PARALLEL. Set this equal to MAX_PARALLEL_JOBS on
# the server. Extra requests simply wait in the server queue, so a higher value
# is safe but gives no speed-up.
MAX_CONCURRENT_REQUESTS = 3

DOWNLOAD_RETRIES = 5
DOWNLOAD_RETRY_DELAY_SECONDS = 5

# If the server is down (502/connection dropped), wait for it to come back
# this many times, this many seconds apart, before giving up.
SERVER_CHECK_RETRIES = 6
SERVER_CHECK_DELAY_SECONDS = 10

# How many times to try one file again if the server dropped mid-run
# and then came back.
FILE_RETRIES = 2

FILE_INPUT_ENDPOINTS = {
    "validate-audio", "convert-audio", "denoise", "vad-segment",
    "asr-transcription", "run-pipeline", "run-pipeline-stream",
}

# Endpoints whose result contains the transcription metadata CSV
TRANSCRIPTION_ENDPOINTS = {
    "asr-transcription", "run-pipeline", "run-pipeline-stream",
}

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}

VALID_LANGUAGES = {"english", "sanskrit"}

# Header every accepted-metadata CSV must have
EXPECTED_METADATA_COLUMNS = [
    "file_name", "audio_path", "transcription", "language", "gender",
]
# ============================================================

LANGUAGE = str(LANGUAGE).strip().lower()
if LANGUAGE not in VALID_LANGUAGES:
    raise SystemExit(
        f"Invalid LANGUAGE '{LANGUAGE}'. Choose one of: {sorted(VALID_LANGUAGES)}"
    )

GENDER = str(GENDER).strip().lower()
if GENDER not in {"auto", "male", "female"}:
    raise SystemExit(f"Invalid GENDER '{GENDER}'. Choose one of: auto, male, female")

DENOISE_CHECKPOINT = str(DENOISE_CHECKPOINT).strip()
DEEPFILTER_OPTION = str(DEEPFILTER_OPTION).strip()

if DENOISE_MODEL in ("bs_roformer", "all") and DENOISE_CHECKPOINT not in {"1", "2"}:
    raise SystemExit(f"Invalid DENOISE_CHECKPOINT '{DENOISE_CHECKPOINT}'. "
                     f"Choose 1 (ep317) or 2 (SW).")
if DENOISE_MODEL in ("deepfilter", "all") and DEEPFILTER_OPTION not in {"1", "2"}:
    raise SystemExit(f"Invalid DEEPFILTER_OPTION '{DEEPFILTER_OPTION}'. "
                     f"Choose 1 (DeepFilterNet2) or 2 (DeepFilterNet3).")


def sanitize(name: str) -> str:
    """Spaces -> underscore. Every other special character is REMOVED."""
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"[^A-Za-z0-9_]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "file"


def sanitize_filename(filename: str) -> str:
    """Sanitize the name part only and keep the file extension."""
    p = Path(filename)
    return sanitize(p.stem) + p.suffix.lower()


def detect_gender_from_path(file_path) -> str:
    parts = [p.lower() for p in Path(file_path).resolve().parts]
    if "female" in parts:
        return "female"
    if "male" in parts:
        return "male"
    return "unknown"


def extra_options(kind: str) -> dict:
    """Optional denoise form fields (BS-Roformer checkpoint, DeepFilter version).
    Only the fields the chosen DENOISE_MODEL uses are sent."""
    opts = {}
    if DENOISE_MODEL in ("bs_roformer", "all"):
        opts[FIELD_DENOISE_CHECKPOINT] = DENOISE_CHECKPOINT
    if DENOISE_MODEL in ("deepfilter", "all"):
        opts[FIELD_DEEPFILTER_OPTION] = DEEPFILTER_OPTION
    return {k: v for k, v in opts.items() if v}


def options_summary(kind: str) -> str:
    """Short text for log lines showing which checkpoints/options are used."""
    parts = []
    if DENOISE_MODEL in ("bs_roformer", "all"):
        parts.append(f"bsroformer_ckpt={DENOISE_CHECKPOINT or 'default'}")
    if DENOISE_MODEL in ("deepfilter", "all"):
        parts.append(f"deepfilter={DEEPFILTER_OPTION or 'default'}")
    return ", ".join(parts) or "no extra denoise options"


def check_metadata_columns(out_dir: str):
    """Warn (never stop) if accepted-metadata CSVs lack the expected header."""
    out_path = Path(out_dir)
    if not out_path.exists():
        return

    csv_files = [
        f for f in out_path.rglob("*.csv")
        if "metadata" in f.name.lower()
        and "rejected" not in f.name.lower()
        and "audit" not in f.name.lower()
    ]

    for csv_file in csv_files:
        try:
            with open(csv_file, "r", encoding="utf-8-sig", newline="") as fh:
                header = next(csv.reader(fh), [])
        except Exception as e:
            print(f"   Could not read CSV header of {csv_file.name}: {e!r}")
            continue

        if header == EXPECTED_METADATA_COLUMNS:
            print(f"   CSV columns OK: {csv_file.name}")
        else:
            print(f"   WARNING: unexpected CSV columns in {csv_file.name}\n"
                  f"      found:    {header}\n"
                  f"      expected: {EXPECTED_METADATA_COLUMNS}")


# ------------------------------------------------------------
# Pack all segments of ONE audio folder into ONE zip -> one request, one CSV
# ------------------------------------------------------------
ZIP_GENDER = {}
_ZIP_TMP_DIR = None


def zip_segment_folder(folder: Path, files) -> str:
    global _ZIP_TMP_DIR
    if _ZIP_TMP_DIR is None:
        _ZIP_TMP_DIR = tempfile.mkdtemp(prefix="asr_zips_")
        atexit.register(shutil.rmtree, _ZIP_TMP_DIR, ignore_errors=True)

    zip_dir = Path(_ZIP_TMP_DIR) / str(len(ZIP_GENDER))
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / f"{sanitize(folder.name)}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, arcname=sanitize_filename(f.name))

    ZIP_GENDER[str(zip_path)] = detect_gender_from_path(folder)
    return str(zip_path)


def expand_input_paths(paths):
    """Returns a list of (file_path, relative_parent_folder) tuples."""

    if isinstance(paths, (str, Path)):
        paths = [paths]

    resolved = []

    for raw in paths:
        p = Path(raw)

        if not p.exists():
            print(f"   Skipping (not found): {raw}")
            continue

        if p.is_dir():
            found = sorted(
                f for f in p.rglob("*")
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
            )
            if not found:
                print(f"   No audio files found inside folder: {raw}")
            else:
                print(f"   {raw} -> {len(found)} audio file(s) found")

            if ENDPOINT == "asr-transcription":
                groups = {}
                for f in found:
                    groups.setdefault(f.parent, []).append(f)
                for parent, files in groups.items():
                    rel = Path(".") if parent == p else parent.relative_to(p).parent
                    resolved.append((Path(zip_segment_folder(parent, files)), rel))
            else:
                resolved.extend((f, f.parent.relative_to(p)) for f in found)

        elif p.suffix.lower() in AUDIO_EXTENSIONS:
            resolved.append((p, Path(".")))

        elif p.suffix.lower() == ".zip":
            resolved.append((p, Path(".")))

        else:
            print(f"   Skipping (not a recognized audio file or folder): {raw}")

    return [(str(f), rel) for f, rel in resolved]


def _filename_from_response(resp: httpx.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    if "filename=" in cd:
        return cd.split("filename=")[-1].strip('"; ')
    return fallback


async def save_stream(resp: httpx.Response, out_dir: str, fallback_name: str):
    """Saves the server result into out_dir (zips are unpacked, plain files kept)."""
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    filename = _filename_from_response(resp, fallback_name)

    with tempfile.TemporaryFile() as tmp:
        async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
            tmp.write(chunk)
        tmp.seek(0)

        if zipfile.is_zipfile(tmp):
            tmp.seek(0)
            with zipfile.ZipFile(tmp) as pack:
                pack.extractall(out_dir_path)
            print(f"Saved folder -> {out_dir_path}")
            return out_dir_path

        tmp.seek(0)
        dest = out_dir_path / filename
        with open(dest, "wb") as f:
            while True:
                block = tmp.read(1024 * 1024)
                if not block:
                    break
                f.write(block)
        print(f"Saved -> {dest}")
        return dest


def _job_params(job_id: str = ""):
    return {"job_id": job_id} if job_id else None


# Job ids of pipeline runs started by this script and not yet finished.
# If the script is interrupted (Ctrl+C), the server is told to cancel them.
ACTIVE_JOB_IDS: set = set()


async def cancel_active_jobs(client: httpx.AsyncClient):
    """Ask the server to stop every job this script started that is still running."""
    for jid in list(ACTIVE_JOB_IDS):
        try:
            resp = await client.post("/pipeline/cancel", params={"job_id": jid}, timeout=10)
            print(f"   cancel job {jid} -> {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"   could not cancel job {jid}: {type(e).__name__}: {e!r} "
                  f"(the server also cancels when the connection closes)")
        finally:
            ACTIVE_JOB_IDS.discard(jid)


async def download_with_retry(client: httpx.AsyncClient, path: str, out_dir: str,
                              fallback_name: str, job_id: str = ""):
    last_exc = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            async with client.stream("GET", path, params=_job_params(job_id)) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"{path} -> {resp.status_code}: {body[:500]!r}")
                return await save_stream(resp, out_dir, fallback_name)
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
            last_exc = exc
            print(f"   Download attempt {attempt}/{DOWNLOAD_RETRIES} failed ({exc!r}) "
                  f"-- retrying in {DOWNLOAD_RETRY_DELAY_SECONDS}s...")
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
    raise RuntimeError(f"Download of {path} failed after {DOWNLOAD_RETRIES} attempts") from last_exc


async def print_output_structure(client: httpx.AsyncClient):
    print("\n── Data structure after run ──────────────────────")
    try:
        resp = await client.get("/data/structure")
    except httpx.HTTPError as e:
        print(f"Could not fetch data structure: {type(e).__name__}: {e!r}")
        print("───────────────────────────────────────────────────\n")
        return
    print(resp.status_code, resp.headers.get("content-type"))
    if resp.headers.get("content-type", "").startswith("application/json"):
        print(json.dumps(resp.json(), indent=2))
    else:
        print("Non-JSON response:", resp.text[:500])
    print("───────────────────────────────────────────────────\n")


async def server_is_up(client: httpx.AsyncClient) -> bool:
    try:
        resp = await client.get("/pipeline/status", timeout=15)
        return resp.status_code not in (502, 503, 504)
    except httpx.HTTPError:
        return False


async def wait_for_server(client: httpx.AsyncClient, retries: int = None) -> bool:
    retries = SERVER_CHECK_RETRIES if retries is None else retries
    for attempt in range(1, retries + 1):
        if await server_is_up(client):
            return True
        print(f"   Server not reachable (attempt {attempt}/{retries}) -- "
              f"retrying in {SERVER_CHECK_DELAY_SECONDS}s...")
        await asyncio.sleep(SERVER_CHECK_DELAY_SECONDS)
    return False


def is_server_down_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, RuntimeError) and any(
        f"-> {code}" in str(exc) for code in (502, 503, 504))


SERVER_DOWN_HELP = (
    "   The API server is not responding (502 / connection dropped).\n"
    "   It most likely crashed or was killed (often out of memory -- with\n"
    "   several parallel jobs, try lowering MAX_PARALLEL_JOBS on the server).\n"
    "   Check the terminal where the API server runs, restart it, then run\n"
    "   this script again."
)


async def process_one_file(client: httpx.AsyncClient, file_path: str, out_dir: str):

    name = Path(file_path).name
    upload_name = sanitize_filename(name)
    if GENDER in ("male", "female"):
        gender = GENDER
    else:
        gender = ZIP_GENDER.get(file_path) or detect_gender_from_path(file_path)

    if ENDPOINT == "validate-audio":
        with open(file_path, "rb") as f:
            resp = await client.post("/validate-audio", files={"file": (upload_name, f)})
        if resp.status_code >= 400:
            raise RuntimeError(f"validate-audio -> {resp.status_code}: {resp.text[:500]!r}")
        print(f"[{name}]", json.dumps(resp.json(), indent=2))

    elif ENDPOINT == "convert-audio":
        with open(file_path, "rb") as f:
            async with client.stream("POST", "/convert-audio",
                                      files={"file": (upload_name, f)}) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"convert-audio -> {resp.status_code}: {body[:500]!r}")
                print(f"   [{name}] job id: {resp.headers.get('x-job-id')}")
                await save_stream(resp, out_dir, "converted.wav")

    elif ENDPOINT == "denoise":
        print(f"▶ {name}  (uploaded as {upload_name}; denoise={DENOISE_MODEL}, "
              f"{options_summary('denoise')})")
        with open(file_path, "rb") as f:
            async with client.stream("POST", "/denoise",
                                      files={"file": (upload_name, f)},
                                      data={"model": DENOISE_MODEL,
                                            **extra_options("denoise")}) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"denoise -> {resp.status_code}: {body[:500]!r}")
                print(f"   [{name}] job id: {resp.headers.get('x-job-id')}")
                await save_stream(resp, out_dir, "denoised.wav")

    elif ENDPOINT == "vad-segment":
        with open(file_path, "rb") as f:
            async with client.stream("POST", "/vad-segment",
                                      files={"file": (upload_name, f)}) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"vad-segment -> {resp.status_code}: {body[:500]!r}")
                print(f"   [{name}] job id: {resp.headers.get('x-job-id')}")
                await save_stream(resp, out_dir, "segments")

    elif ENDPOINT == "asr-transcription":
        # Runs in its own server-side workspace -> several can run in parallel.
        # (Live logs show in the server terminal. The job id printed after the
        #  run lets you read the full log with ENDPOINT = "logs" and JOB_ID.
        #  Use run-pipeline-stream if you want live logs here in the client.)
        print(f"▶ {name}  (uploaded as {upload_name}; asr={ASR_MODEL}, "
              f"language={LANGUAGE}, gender={gender}) "
              f"-- waiting for server...")
        with open(file_path, "rb") as f:
            async with client.stream("POST", "/asr-transcription",
                                      files={"file": (upload_name, f)},
                                      data={"model": ASR_MODEL,
                                            "language": LANGUAGE,
                                            "gender": gender}) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"asr-transcription -> {resp.status_code}: {body[:500]!r}")
                print(f"   [{name}] job id: {resp.headers.get('x-job-id')}")
                await save_stream(resp, out_dir, "transcriptions")

    elif ENDPOINT == "run-pipeline-blocking":
        # Legacy blocking call (no live logs). Not reachable from the normal
        # config: "run-pipeline" now uses the streaming branch below.
        print(f"▶ {name}  (uploaded as {upload_name}; denoise={DENOISE_MODEL}, "
              f"asr={ASR_MODEL}, {options_summary('pipeline')}, "
              f"language={LANGUAGE}, gender={gender}) "
              f"-- waiting for server...")
        with open(file_path, "rb") as f:
            async with client.stream("POST", "/run-pipeline",
                                      files={"file": (upload_name, f)},
                                      data={"denoise_model": DENOISE_MODEL,
                                            "asr_model": ASR_MODEL,
                                            "language": LANGUAGE,
                                            "gender": gender,
                                            **extra_options("denoise")}) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"run-pipeline -> {resp.status_code}: {body[:500]!r}")
                print(f"   [{name}] job id: {resp.headers.get('x-job-id')}")
                await save_stream(resp, out_dir, "pipeline_output")

    elif ENDPOINT in ("run-pipeline", "run-pipeline-stream"):
        # Both options use the streaming endpoint so every server log line
        # (validate, convert, denoise, VAD, ASR) is printed live here.
        print(f"   [{name}] starting pipeline (uploaded as {upload_name}) "
              f"(denoise={DENOISE_MODEL}, asr={ASR_MODEL}, "
              f"{options_summary('pipeline')}, "
              f"language={LANGUAGE}, gender={gender})")
        got_any_event = False
        job_id = ""
        final_status = None
        final_error = None
        last_progress = None

        with open(file_path, "rb") as f:
            async with client.stream("POST", "/run-pipeline/stream",
                                      files={"file": (upload_name, f)},
                                      data={"denoise_model": DENOISE_MODEL,
                                            "asr_model": ASR_MODEL,
                                            "language": LANGUAGE,
                                            "gender": gender,
                                            **extra_options("denoise")}) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"run-pipeline/stream -> {resp.status_code}: {body}")

                job_id = resp.headers.get("x-job-id", "")
                print(f"   [{name}] job id: {job_id}")
                if job_id:
                    ACTIVE_JOB_IDS.add(job_id)

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    got_any_event = True
                    event = json.loads(line[len("data:"):].strip())

                    if event.get("type") == "log":
                        print(f"[{name}] {event['line']}")
                    elif event.get("type") == "progress":
                        job_id = event.get("job_id") or job_id
                        final_status = event.get("status")
                        final_error = event.get("error")
                        step = f"{event.get('step_index', 0) + 1}/{event.get('total_steps', '?')}"
                        msg = (f"[{name}] [{event.get('pct', 0):>3}%] step {step} "
                               f"{event.get('step_name') or ''}  (status={final_status})")
                        if msg != last_progress:      # server repeats progress every 0.5s
                            print(msg)
                            last_progress = msg
                    elif event.get("type") == "done":
                        print(f"── [{name}] stream closed by server ──")

        ACTIVE_JOB_IDS.discard(job_id)      # stream finished normally -> nothing to cancel

        if not got_any_event:
            raise RuntimeError("No SSE events received -- check the server logs / connection.")
        if final_status == "error":
            raise RuntimeError(f"Pipeline failed on server: {final_error}")
        if final_status == "cancelled":
            raise RuntimeError(f"Pipeline was cancelled on server: {final_error}")

        print(f"✔  [{name}] pipeline finished -- fetching result (job {job_id})...")
        await download_with_retry(client, "/pipeline/download", out_dir,
                                  "pipeline_output", job_id=job_id)


async def main():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=None) as client:

        if ENDPOINT == "options":
            for path in ["/options/denoise-models",
                         "/options/bsroformer-checkpoints",
                         "/options/deepfilter-models",
                         "/options/asr-models",
                         "/options/languages",
                         "/options/vad-model",
                         "/options/audio-separator"]:
                try:
                    resp = await client.get(path)
                except httpx.HTTPError as e:
                    print(path, "-> request failed:", f"{type(e).__name__}: {e!r}")
                    continue
                if resp.status_code >= 400:
                    print(path, f"-> {resp.status_code} (not available on this server)")
                    continue
                print(path, "->", json.dumps(resp.json(), indent=2))

        elif ENDPOINT == "status":
            resp = await client.get("/pipeline/status", params=_job_params(JOB_ID))
            print(json.dumps(resp.json(), indent=2))

        elif ENDPOINT == "logs":
            resp = await client.get("/pipeline/logs", params=_job_params(JOB_ID))
            data = resp.json()
            for line in data.get("log_lines", []):
                print(line)
            print(f"\n(status={data.get('status')}, {data.get('total')} lines, "
                  f"job={data.get('job_id')})")

        elif ENDPOINT == "download":
            await download_with_retry(client, "/pipeline/download", OUTPUT_PATH,
                                      "pipeline_output", job_id=JOB_ID)
            check_metadata_columns(OUTPUT_PATH)

        elif ENDPOINT == "data-structure":
            await print_output_structure(client)

        elif ENDPOINT in FILE_INPUT_ENDPOINTS:
            resolved = expand_input_paths(INPUT_PATH)

            if not resolved:
                print("No audio files resolved from INPUT_PATH -- check the path above.")
                return

            print("   Checking that the server is reachable...")
            if not await wait_for_server(client):
                print(SERVER_DOWN_HELP)
                return
            print("   Server is up.")

            # Every request has its own workspace on the server, so ALL
            # endpoints can now run in parallel.
            limit = max(1, MAX_CONCURRENT_REQUESTS)
            semaphore = asyncio.Semaphore(limit)
            total = len(resolved)
            print(f"   Sending {total} file(s), up to {limit} at a time "
                  f"(language={LANGUAGE})")

            succeeded, failed = [], []
            server_dead = {"flag": False}

            async def worker(i, file_path, rel_dir):
                safe_rel = Path(*[sanitize(part) for part in rel_dir.parts]) \
                    if rel_dir.parts else Path(".")
                out_dir = str(Path(OUTPUT_PATH) / safe_rel / sanitize(Path(file_path).stem))
                async with semaphore:
                    if server_dead["flag"]:
                        print(f"[{i}/{total}] {Path(file_path).name}  ->  SKIPPED (server is down)")
                        failed.append((file_path, "skipped: server is down"))
                        return

                    print(f"[{i}/{total}] {Path(file_path).name}  ->  {out_dir}")
                    for attempt in range(1, FILE_RETRIES + 1):
                        try:
                            await process_one_file(client, file_path, out_dir)
                            succeeded.append(file_path)
                            saved_count = sum(1 for p in Path(out_dir).rglob("*") if p.is_file()) \
                                if Path(out_dir).exists() else 0
                            print(f"✔ [{i}/{total}] DONE and saved: {Path(file_path).name}\n"
                                  f"   {saved_count} file(s) in {out_dir}\n"
                                  f"   Progress: {len(succeeded)} done, {len(failed)} failed, "
                                  f"{total - len(succeeded) - len(failed)} remaining")

                            if ENDPOINT in TRANSCRIPTION_ENDPOINTS:
                                check_metadata_columns(out_dir)
                            return
                        except Exception as e:
                            err_text = f"{type(e).__name__}: {e!r}"

                            if is_server_down_error(e):
                                print(f"   Connection problem: {err_text}")
                                print("   Checking whether the server is still alive...")
                                if await wait_for_server(client):
                                    if attempt < FILE_RETRIES:
                                        print(f"   Server is back -- retrying this file "
                                              f"(attempt {attempt + 1}/{FILE_RETRIES})")
                                        continue
                                    print("   Server is up but the run keeps failing -- "
                                          "check the server logs for the real error.")
                                else:
                                    print(SERVER_DOWN_HELP)
                                    server_dead["flag"] = True

                            print(f"   FAILED: {file_path}\n    ERROR: {err_text}")
                            failed.append((file_path, err_text))
                            return

            try:
                await asyncio.gather(*(
                    worker(i, fp, rel) for i, (fp, rel) in enumerate(resolved, start=1)
                ))
            except (asyncio.CancelledError, KeyboardInterrupt):
                # Ctrl+C: closing the connections already makes the server cancel
                # its jobs; this explicit call makes sure of it and kills queued ones.
                print("\n   Interrupted -- asking the server to stop the running job(s)...")
                await cancel_active_jobs(client)
                raise

            if ENDPOINT in {"run-pipeline", "run-pipeline-stream"}:
                await print_output_structure(client)

            print("\n" + "=" * 60)
            print(f"Batch complete: {len(succeeded)} succeeded, {len(failed)} failed "
                  f"(of {total})")
            if failed:
                print("Failed files:")
                for fp, err in failed:
                    print(f"  - {fp}: {err}")
            print("=" * 60)

        else:
            print(f"Unknown ENDPOINT: {ENDPOINT}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user -- the server has been told to cancel the job(s).")