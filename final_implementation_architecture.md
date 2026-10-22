# Final Implementation Architecture: Low-Latency Metadata Duplicate Detection

## 1. Purpose

This document defines the implementation architecture for a metadata-based duplicate-detection workflow. It retrieves video metadata, checks whether content is already represented, and tracks the cumulative accepted duration per speaker.

The system uses a **self-hosted LLM endpoint only when a fast pre-filter identifies a probable duplicate**. Clearly unique candidates should not trigger an LLM request.

**Speaker duration limit:** 3.5 hours per speaker = 210 minutes = 12,600 seconds.

> Endpoint-specific integration remains configurable because the exact endpoint URL, authentication method, request schema, and response schema have not yet been supplied. Do not assume an OpenAI-compatible API until verified.

## 2. High-Level Architecture

```text
                         USER INPUT
                 (query / video / channel / playlist)
                              |
                              v
                     PLATFORM API CALL
                              |
                              v
                  METADATA NORMALIZATION
            (video ID, title, channel, speaker clues,
             description, comments, duration, URL)
                              |
                              v
                   EXACT DUPLICATE CHECK
                 (video ID / canonical URL)
                      /             \
               Exact match         No match
                   |                  |
                   v                  v
             DUPLICATE       FAST PRE-FILTER
             No LLM call     (cheap metadata rules,
             Do not add      hashes / similarity search)
                                    |
                      +-------------+-------------+
                      |                           |
              Clearly unique             Probable duplicate
              No LLM call                  Candidate matches
                      |                           |
                      |                           v
                      |                  SELF-HOSTED LLM ENDPOINT
                      |                  (conditional request only)
                      |                           |
                      |                +----------+----------+
                      |                |          |          |
                      |            DUPLICATE  POTENTIAL   UNIQUE
                      |                         DUPLICATE
                      |                            |
                      +----------------------------+
                                    |
                                    v
                         DUPLICATE FILTER
                    Confirmed duplicates excluded
                                    |
                                    v
                       SPEAKER IDENTIFICATION
                     (known identity if reliable;
                       otherwise mark uncertain)
                                    |
                                    v
                    SPEAKER DURATION VALIDATION
                  current accepted seconds + candidate
                         duration <= 12,600?
                           /             \
                         Yes              No
                          |                |
                          v                v
                    ACCEPT VIDEO       REJECT / SKIP
                    Update ledger      Record reason
                          |
                          v
                 DATABASE / AUDIT LOG
                          |
                          v
                FINAL CSV (existing 11 columns)
```

## 3. Core Processing Rules

1. **Exact identity first:** If the video ID already exists, mark it duplicate immediately. Do not call the LLM.
2. **Fast pre-filter second:** Compare normalized metadata with existing records using inexpensive checks.
3. **Clearly unique:** If no plausible match is found, classify according to the configured pre-filter policy and skip the LLM.
4. **Probable duplicate:** Send the candidate and a limited set of likely matches to the self-hosted LLM endpoint.
5. **LLM decision:** Require one of `DUPLICATE`, `POTENTIAL_DUPLICATE`, or `UNIQUE`, plus a short evidence-based reason.
6. **Duration validation:** Run separately after duplicate detection. Only accepted, non-duplicate videos contribute to the speaker total.
7. **Safe failure:** An endpoint timeout, invalid response, or network failure is not evidence that a video is unique. Mark it for retry/review or fail the check safely.

## 4. Input and Metadata Retrieval

### User input

Support the input types required by the application:
- Search query
- Video URL or ID
- Channel URL or ID
- Playlist URL or ID

### Metadata from the platform API

Retrieve available fields:
- Video ID and canonical URL
- Title
- Channel name and channel ID
- Speaker name or speaker clues, when available
- Description
- Comments, when available and useful
- Video duration
- Publication date
- Tags/category, when available

Keep missing values as unknown. A channel is not necessarily the speaker. Do not infer an identity solely from a channel name.

## 5. Metadata Normalization and Exact Match

Normalize comparison fields without losing the original metadata:
- Trim whitespace and normalize Unicode.
- Normalize case for comparison-only text.
- Canonicalize URLs and remove irrelevant tracking parameters.
- Store duration as integer seconds.
- Use the platform video ID as the primary exact identity key.

If a candidate video ID already exists in the database, return `DUPLICATE` without making an LLM request. A reliable canonical URL match can be used as a secondary exact check.

## 6. Fast Pre-Filter: Decide Whether to Call the LLM

The pre-filter should be inexpensive and should retrieve only plausible existing matches.

Possible signals:
- Normalized title similarity
- Description similarity
- Channel/source clues
- Speaker-name clues
- Text hashes or fingerprints
- Existing embeddings/vector search, if already available
- Transcript similarity or audio fingerprints, if available

### Routing policy

| Pre-filter result | Action | LLM request? |
|---|---|---|
| Exact video ID/URL match | Mark duplicate | No |
| No plausible match | Continue as unique under configured policy | No |
| Probable match | Send top-K matching records to the self-hosted LLM | Yes |
| Pre-filter cannot decide due to missing/poor metadata | Review queue or configured conservative route | Only if routed as probable |

Tune the pre-filter threshold against labeled examples. A threshold that is too strict can miss real duplicates before they reach the LLM.

Do not use the same speaker alone or the same topic alone as proof of duplication. The same speaker may discuss different topics, and different speakers may discuss the same topic.

## 7. Self-Hosted LLM Endpoint Integration

### 7.1 Configuration

Keep endpoint configuration outside the application code. For example, use environment variables or a secrets manager:

```env
LLM_BASE_URL=https://your-internal-host
LLM_ENDPOINT_PATH=/your/actual/inference/path
LLM_API_KEY=replace_if_required
LLM_MODEL=your_model_name_if_required
LLM_TIMEOUT_SECONDS=20
LLM_TOP_K_MATCHES=3
```

These names are placeholders, not a claim that the endpoint uses this exact configuration or path. Confirm the actual path, authentication, payload, and response format from the endpoint documentation. Never commit real API keys or credentials to source control.

### 7.2 Conditional request

Only after the pre-filter finds a probable duplicate should the backend call the endpoint. Send:
- Candidate video metadata
- A small set of the strongest existing candidate matches
- Pre-filter similarity signals
- A clear duplicate definition
- Instructions that speaker or topic similarity alone does not prove duplication
- The required response schema, if the endpoint supports structured output

Do not send the entire database or all comments by default. Include only evidence needed for the comparison.

### 7.3 Recommended decision contract

The application should normalize the model's response into this internal shape, regardless of the endpoint's actual response format:

```json
{
  "status": "DUPLICATE",
  "matched_video_id": "existing-video-id-or-null",
  "reason": "Evidence-based explanation",
  "evidence_fields": ["title", "description"],
  "confidence": 0.0
}
```

Allowed `status` values:
- `DUPLICATE`
- `POTENTIAL_DUPLICATE`
- `UNIQUE`

The JSON above is the application's desired internal contract. The endpoint may return a different wire format; implement a response adapter once its real response schema is known. Validate parsed output. Treat a model-reported confidence as an uncalibrated score unless evaluated on labeled examples.

### 7.4 Endpoint error handling

- Use a bounded timeout.
- Retry only transient failures, with a small retry count and backoff.
- Do not retry invalid requests indefinitely.
- Do not turn a timeout, HTTP error, or malformed response into `UNIQUE`.
- Store a failure status and allow retry or manual review.
- Record request latency and endpoint/model version, without logging secrets.

## 8. Duplicate Decision Semantics

| Status | Meaning | Action |
|---|---|---|
| `DUPLICATE` | Strong evidence that the same content is already represented | Exclude from new unique records and duration totals |
| `POTENTIAL_DUPLICATE` | Evidence is similar or incomplete | Send for review or stronger content comparison |
| `UNIQUE` | No sufficiently strong duplicate match found | Continue to speaker and duration validation |

Examples:
- Same speaker, different topic: not automatically duplicate.
- Same topic, different speaker: not automatically duplicate.
- Similar title and description: candidate evidence, not proof.
- Same recording uploaded with a changed title: likely duplicate if stronger content evidence supports it.
- Missing speaker name: continue with available evidence; preserve uncertainty.

Where possible, use transcripts or audio fingerprints for stronger content-level comparisons. Metadata alone may not distinguish a re-upload from a separate recording on the same topic.

## 9. Per-Speaker Duration Tracking

The maximum accepted duration is **12,600 seconds per speaker**.

### Rules

1. Confirm the candidate is not a duplicate before duration accounting.
2. Resolve the speaker identity from reliable evidence.
3. If speaker identity is uncertain, route to review rather than silently merging totals.
4. Read the candidate duration in integer seconds.
5. Get the current total for accepted, non-duplicate videos for that speaker.
6. Accept only when `current_total + candidate_duration <= 12600`.
7. Update the accepted record and duration ledger atomically.
8. Duplicate, rejected, and unresolved-review videos do not increase accepted duration.
9. Reprocessing the same video must not add its duration again.

Exactly 12,600 seconds is allowed; any total above it must be rejected or skipped.

### Pseudocode

```python
MAX_SPEAKER_SECONDS = 12_600

if duplicate_status == "DUPLICATE":
    exclude_from_duration_ledger()
elif duplicate_status == "POTENTIAL_DUPLICATE":
    send_to_review()
elif not speaker_identity_is_confident:
    send_to_review()
elif current_accepted_seconds + candidate_duration_seconds <= MAX_SPEAKER_SECONDS:
    atomically_save_video_and_duration_ledger()
else:
    reject_or_skip(reason="Would exceed 3.5-hour speaker limit")
```

For videos with multiple speakers, define the policy explicitly. Do not charge the full video duration to every speaker unless that is intended.

## 10. End-to-End Pseudocode

```python
def process_candidate(candidate):
    metadata = normalize_metadata(candidate)

    # Stage 1: exact identity, no LLM call.
    if store.video_id_exists(metadata.video_id):
        return record_duplicate(
            metadata,
            reason="Video ID already exists",
        )

    # Stage 2: fast retrieval and pre-filter.
    matches = retrieve_plausible_matches(metadata)

    if not matches:
        decision = {
            "status": "UNIQUE",
            "matched_video_id": None,
            "reason": "No plausible matches found by pre-filter",
        }
    else:
        # Stage 3: conditional call to the self-hosted endpoint.
        # Adapt request/response handling to the actual endpoint contract.
        decision = call_self_hosted_llm(
            candidate=metadata,
            existing_matches=matches[:TOP_K],
        )
        validate_and_normalize_decision(decision)

    if decision["status"] == "DUPLICATE":
        return save_decision_idempotently(metadata, decision)

    if decision["status"] == "POTENTIAL_DUPLICATE":
        return save_for_review_idempotently(metadata, decision)

    speaker = resolve_speaker(metadata)
    if not speaker.is_confident:
        return save_for_review_idempotently(
            metadata,
            reason="Speaker identity is uncertain",
        )

    return accept_if_within_duration_cap_atomically(
        metadata=metadata,
        speaker_id=speaker.id,
        max_seconds=MAX_SPEAKER_SECONDS,
    )
```

This is architectural pseudocode, not a complete runnable service. It omits stack-specific database calls, API authentication, validation implementations, and endpoint-specific request serialization.

## 11. Data Storage

### Video record
- `video_id` — unique platform ID
- `canonical_url`
- `title`
- `channel_id`
- `channel_name`
- `speaker_id` or unresolved speaker label
- `description`
- `comments` or comment summary, if collected
- `duration_seconds`
- `published_at`
- `duplicate_status`
- `matched_video_id`
- `decision_reason`
- timestamps

### Speaker duration ledger
- `speaker_id`
- `video_id`
- `duration_seconds`
- `accepted_status`
- `accepted_at`

### Decision audit
- `candidate_video_id`
- `status`
- `matched_video_id`
- `reason`
- `evidence_fields`
- pre-filter scores
- endpoint/model version
- request latency
- decision timestamp
- error state, if any

Use unique constraints and idempotent writes. Enforce duration caps in the backend/database transaction, not only in the UI, to protect against concurrent jobs and repeated submissions.

## 12. Low-Latency Requirements

- Exact ID/URL lookup before any LLM call.
- Avoid the LLM for candidates with no plausible matches.
- Retrieve a small top-K set of likely matches.
- Use connection pooling and bounded timeouts.
- Cache stable metadata and completed decisions where safe.
- Track API, pre-filter, LLM, and database latency separately.
- Use a review path for ambiguous cases.
- Do not classify failed requests as unique.
- Keep comments/descriptions as untrusted input.
- Keep secrets out of logs and source control.

## 13. Final Output

Preserve the existing **11-column CSV format**. Keep detailed matching evidence, scores, LLM response details, and endpoint errors in the database or audit log unless the established CSV specification explicitly includes those fields.

## 14. Acceptance Criteria

1. Exact video ID matches are detected without calling the LLM.
2. Clearly unique candidates do not call the LLM.
3. Only probable duplicate candidates are sent to the self-hosted endpoint.
4. The endpoint's actual request and response schema are handled by a dedicated adapter.
5. Endpoint errors never become automatic `UNIQUE` decisions.
6. Same speaker or same topic alone does not force a duplicate result.
7. Uncertain matches can be marked `POTENTIAL_DUPLICATE`.
8. Duplicate videos never increase accepted speaker duration.
9. No accepted speaker total exceeds 12,600 seconds.
10. Reprocessing a video does not create duplicate rows or double-count duration.
11. The existing final 11-column CSV format remains unchanged.

## 15. Information Needed to Finalize Endpoint-Specific Code

Before writing the exact API integration, confirm:
- Endpoint URL and path
- HTTP method
- Authentication requirements
- Required request JSON/body
- Example successful response
- Whether the endpoint expects a model name or other parameters
- Timeout or rate-limit constraints

Until these details are known, the architecture can be implemented around an adapter interface, but the exact request/response code should not be guessed.
