# Implementation Plan: Selective Audio Download, Endpoint Processing & UI Segment Validation

## 1. Overview & Core Requirements

Based on your workflow specifications:
1. **Selective Ingestion**: Researchers/reviewers selectively choose specific approved recordings (or input a specific URL) to trigger audio downloading and endpoint processing, rather than processing everything in an unmonitored batch.
2. **Automated Unpacking & Server Streaming**: Instead of delivering an opaque ZIP file to download, the system intercepts the ZIP stream from the processing server (`http://111.118.177.236:8008`), automatically unpacks the segmented WAVs and metadata CSV, and mounts them locally for browser playback.
3. **Interactive UI for Manual Segment Validation**: Displays every segmented audio clip directly in the Web UI with:
   - Built-in audio player (`<audio>`) with speed toggle (`0.8x`, `1.0x`, `1.2x`) and keyboard shortcuts.
   - Editable transcript text box (enabling quick correction of Sanskrit terms and dialect words).
   - Quick decision buttons: **Accept (Keep)** vs **Reject (Cut)** per segment.
   - Automatic recalculation of `after_processing_duration` and `total_files` upon finalization.

---

## 2. End-to-End Workflow Architecture

```mermaid
graph TD
    A[Web UI: Approved Records Table] -->|Select Record & Click 'Process Pipeline'| B[API: /api/pipeline/process/{id}]
    
    subgraph Step 1: Selective Download
        B --> C[Audio Downloader: backend/audio_downloader.py]
        C -->|yt-dlp + ffmpeg| D[Save 16kHz WAV: static/media/raw/{record_id}.wav]
    end
    
    subgraph Step 2: Audio Processing Server: 111.118.177.236:8008
        D --> E[Client: backend/endpoint_Data_up.py]
        E -->|POST /run-pipeline/stream| S1[Denoise: BS-Roformer / DeepFilter]
        S1 --> S2[VAD Segmentation: Silero]
        S2 --> S3[ASR Transcription: Whisper Large v3]
        S3 --> S4[Package Zip: segments + metadata.csv]
    end
    
    subgraph Step 3: Server Zip Extraction & Database Storage
        S4 -->|Streaming Download| F[Auto-Unpack into: static/media/segments/{record_id}/]
        F --> G[Parse metadata.csv -> Insert into SQLite audio_segments table]
        G --> H[Update Record Status: 'VALIDATION_READY']
    end
    
    subgraph Step 4: UI Manual Validation Workspace
        H --> I[Web UI: Segment Validation Studio]
        I -->|Play Segment Audio| J[Researcher listens to clip]
        I -->|Edit text| K[Correct Sanskrit terms in transcript]
        I -->|Click Decision| L[Accept / Reject Segment]
        L -->|Click 'Finalize Validation'| M[Save Approved Segments & Update Parent Record]
    end
```

---

## 3. Database Schema Updates (`backend/database.py`)

A new table **`audio_segments`** will be added to track individual segmented clips for each parent recording:

```sql
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
    validation_status TEXT DEFAULT 'PENDING',  -- 'ACCEPTED', 'REJECTED', 'PENDING'
    rejection_reason TEXT,
    validated_by TEXT,
    validated_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_segments_record_id ON audio_segments(record_id);
CREATE INDEX IF NOT EXISTS idx_segments_status ON audio_segments(validation_status);
```

In the parent `records` table:
- New status state: `PIPELINE_PROCESSING` and `VALIDATION_READY`.
- Updated fields on finalization: `after_processing_duration`, `total_files`, `completion_date`.

---

## 4. Module Details

### 4.1. Audio Downloader Engine (`backend/audio_downloader.py`)
- **Technology**: `yt-dlp` Python library + existing system `ffmpeg`.
- **Functionality**:
  - `download_audio(url: str, output_path: str) -> Dict[str, Any]`
  - Converts source audio directly to clean, standard 16kHz mono WAV (`pcm_s16le`, 16000Hz, 1 channel).
  - Normalizes volume / extracts clean audio track without re-encoding bloat.
  - Returns metadata: actual duration, file size, format.

### 4.2. Endpoint Client & Unpack Service (`backend/endpoint_Data_up.py` / `pipeline_service.py`)
- **Stream Processing**:
  - Connects to `http://111.118.177.236:8008/run-pipeline/stream`.
  - Captures real-time Server-Sent Events (SSE) progress (`step 1/5: Denoising`, `step 2/5: VAD`, `step 3/5: ASR`).
  - Downloads the final generated archive.
- **Auto-Unpacking**:
  - Extracts the archive into `static/media/segments/{record_id}/`.
  - Reads the generated metadata CSV:
    ```csv
    file_name,audio_path,transcription,language,gender
    ```
  - Calculates each segment's exact duration using audio file headers.
  - Seeds the `audio_segments` table with `validation_status = 'PENDING'` (or default `'ACCEPTED'` with easy one-click reject).

### 4.3. Backend API Endpoints (`backend/app.py`)
- `POST /api/pipeline/run/{record_id}`:
  - Initiates selective downloading and endpoint processing for the selected record.
  - Runs in an asynchronous background worker so the UI stays responsive.
- `GET /api/pipeline/status/{record_id}`:
  - Returns current pipeline state (`DOWNLOADING`, `PROCESSING`, `EXTRACTING`, `VALIDATION_READY`, `FAILED`) and live SSE progress percentage.
- `GET /api/records/{record_id}/segments`:
  - Returns all extracted audio segments, playback URLs, transcriptions, and validation statuses.
- `POST /api/segments/{segment_id}/validate`:
  - Updates validation decision (`ACCEPTED` / `REJECTED`) and modified transcription text.
- `POST /api/records/{record_id}/finalize-validation`:
  - Recalculates total accepted seconds and segment count.
  - Updates parent record `after_processing_duration`, `total_files`, `status = 'COMPLETED'`.

---

## 5. UI Design: Manual Segment Validation Studio

### 5.1. Location & Entry Points
1. **Approved Records Table**:
   - Each row receives an action button:
     - If not processed yet: **"⚡ Process Pipeline"** button.
     - If processing: **"⏳ Processing (45%)"** animated progress pill.
     - If processed: **"🎧 Validate Segments ({n})"** badge with attention glow.
2. **Multi-Select Checkboxes**:
   - Researchers can select 1, 2, or more records and click **"⚡ Process Selected"** from the bulk toolbar.

### 5.2. Segment Validation Modal / Workspace
When the user opens **"🎧 Validate Segments"**, an interactive review suite opens:

```text
+------------------------------------------------------------------------------------------------+
|  🎧 Segment Validation: Record #42 - Ancient Vedic Chanting and Grammar                        |
|  Speaker: Dr. Sharma | Gender: Male | Raw Duration: 14m 20s | Segments: 28                     |
+------------------------------------------------------------------------------------------------+
|  Summary Stats:                                                                                |
|  [ 24 Accepted (11m 45s) ]   [ 4 Rejected (01m 20s) ]   [ Total Accepted: 11m 45s ]           |
|  [ Accept All ]  [ Reject All ]  [ Playback Speed: 1.0x v ]                                    |
+------------------------------------------------------------------------------------------------+
|  [Seg #1] 00:00 - 00:08 (8.2s)                                     Status: [ ACCEPTED (v) ]   |
|  [ > Play ] =======================[0:04/0:08]======               [ Reject (X) ]             |
|  Transcript:                                                                                  |
|  [ The oral tradition preserved Vedic chants with acoustic precision through Shiksha rules. ] |
+------------------------------------------------------------------------------------------------+
|  [Seg #2] 00:08 - 00:15 (6.7s)                                     Status: [ ACCEPTED (v) ]   |
|  [ > Play ] =======================[0:00/0:07]======               [ Reject (X) ]             |
|  Transcript: (User edited Sanskrit term)                                                      |
|  [ Panini Ashtadhyayi provides an axiomatic system of phonetic morphology.                  ] |
+------------------------------------------------------------------------------------------------+
|  [Seg #3] 00:15 - 00:21 (5.9s)  *Heavy background cough*           Status: [ REJECTED (X) ]   |
|  [ > Play ] =======================[0:00/0:06]======               [ Restore (v) ]            |
|  Transcript: [ [inaudible noise] and then the speaker paused.                               ] |
+------------------------------------------------------------------------------------------------+
|                                  [ Cancel ]    [ Save & Finalize Record (11m 45s, 24 Segments) ]|
+------------------------------------------------------------------------------------------------+
```

### 5.3. Key Features of the Validation UI
1. **Audio Player per Segment**:
   - Zero-latency browser audio playback directly from local `/static/media/segments/...`.
   - Keyboard shortcuts: `Space` = Play/Pause current segment, `Down Arrow` = Next segment.
2. **Editable Transcription**:
   - Instant text editing so researchers can fix spelling of Sanskrit terms before final dataset export.
3. **One-Click Toggles**:
   - Toggle segment acceptance with a single click or hotkey (`A` for Accept, `R` for Reject).
   - Rejected segments are grayed out with a strike-through indicator and excluded from the duration sum.
4. **Live Duration Ledger Synchronization**:
   - As segments are accepted or rejected, the top summary dynamically calculates the exact accepted time down to the second.
5. **Finalize Action**:
   - Clicking **"Save & Finalize Record"** locks in the accepted segments, updates the database, and marks the parent record as complete with its RFC 4180 export values.

---

## 6. Implementation Steps

| Step | Component | Description |
| :--- | :--- | :--- |
| **Step 1** | Dependencies | Install `yt-dlp` in Python environment; verify `ffmpeg` binding. |
| **Step 2** | Database Layer | Add `audio_segments` table schema and CRUD helpers in [`backend/database.py`](backend/database.py). |
| **Step 3** | Downloader Module | Create `backend/audio_downloader.py` to download selected URLs to standardized 16kHz WAV. |
| **Step 4** | Pipeline Service | Create `backend/pipeline_service.py` to handle the server streaming call, unzip into `static/media/segments/`, and parse the CSV into `audio_segments`. |
| **Step 5** | REST Endpoints | Add `/api/pipeline/run/{id}`, `/api/records/{id}/segments`, `/api/segments/{id}/validate`, and `/api/records/{id}/finalize` in [`backend/app.py`](backend/app.py). |
| **Step 6** | UI Integration | Update [`static/index.html`](static/index.html) and [`static/app.js`](static/app.js) with the **Process Pipeline** trigger button, live status indicators, and the **Segment Validation Studio** modal. |
| **Step 7** | Verification | Test selective download, processing against `http://111.118.177.236:8008`, UI segment playback, editing, and finalization. |

---

## 7. Next Step

Please confirm if this plan matches your expectations. Once confirmed, we will begin implementation with Step 1.
