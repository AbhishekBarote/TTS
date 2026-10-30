# Centralized Audio Research & Revalidation Workflow

A centralized web application for researching, verifying, and curating audio/video recordings featuring **Indian English speech with spoken Sanskrit terms**.

Designed in compliance with [`Centralized_Audio_Research_Revalidation_Workflow.md`](Centralized_Audio_Research_Revalidation_Workflow.md).

---

## 🌟 Key Updates & Features

### 1. Instant Auto-Population on Link Paste
When you paste an audio/video link (YouTube, Archive.org, Vimeo, etc.):
- **Description Auto-Fill**: Automatically extracts and formats the Title & Channel Name:
  ```text
  5 Forgotten Vedic Goddesses: Lost Legends of Ancient India
  channel : swapnalok
  ```
- **Duration Auto-Recording**: Automatically extracts recording duration in standardized `HH:MM:SS` format.
- **Platform Auto-Detection**: Selects the appropriate platform (`YouTube`, `Internet Archive`, etc.).

### 2. Multi-Stage Duplicate Prevention
- **Platform Item ID Detection**: Extracts and matches canonical item IDs from YouTube (`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`), Archive.org, Vimeo.
- **Canonical URL Normalization**: Strips tracking query parameters (`utm_*`, `si`, `fbclid`, `feature`).
- **Real-Time Duplicate Block**: Alerts researchers immediately and blocks duplicate submissions with a direct link to the existing record.

### 3. Streamlined Intake Workflow
- **Mandatory Evidence Removed**: Submissions no longer require a separate mandatory evidence block. Researchers can submit directly upon verifying the recording.
- **Manual Identity Entry**: Replaced the dropdown with a manual text input (`Researched_By` and `Revalidated_By`). The browser remembers your name in `localStorage` across submissions.

### 4. Approved Dataset & RFC 4180 CSV Export
- **Strict 11-Column Schema**:
  ```csv
  Research_Date,Source,Audio_Link,Description,Gender,Before Processing Duration,Accent,Licensing,Speakers_ID,Researched_By,Revalidated_By
  ```
- **Export Rules**:
  - Export **approved records only**.
  - Exactly 11 columns in exact order.
  - Internal audit fields (status, rejection reasons, notes) excluded from export.
  - Defensive duplicate recheck prior to export.
  - RFC 4180 escaping with UTF-8 encoding.

---

## 🚀 Access & Team Sharing

### URLs
- **Local Access:** [http://localhost:8088](http://localhost:8088)
- **Same-Network Team Link:** [http://192.168.1.88:8088](http://192.168.1.88:8088)
  *(Anyone connected to the same Wi-Fi can open this link on their laptop or mobile)*

### Start Server
```bash
python run.py
```
