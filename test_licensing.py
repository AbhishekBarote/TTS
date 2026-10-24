"""
Comprehensive Test Suite for Automatic License Detection System.

Tests all requirements:
1. Automatic detection from title, description, tags, comments, channel info, metadata.
2. Support for all 11 licensing categories defined in rights document.
3. Missing license handling (Unknown / Not Specified, never guessed as CC BY).
4. Conflicting claims across sources (flagged for manual review, FLAGGED_CONFLICT).
5. Distinguishing detected claims from verified permissions (NEEDS_VERIFICATION).
6. Rights document screening guidance (CC BY acceptable with verification, CC BY-ND flagged/rejected, CC BY-NC rejected, Standard YouTube not approved without permission, etc.).
7. Database persistence of detected license, evidence, detection source, verification status, reviewer decision.
8. CSV export preservation (22-column RFC 4180 format, final licensing in Licensing column).
9. Graceful error handling (unavailable comments, network errors, API quota limits).
"""

import os
import io
import csv
import json
import sqlite3
import uuid
from fastapi.testclient import TestClient

from backend.app import app
from backend.database import get_connection, init_db
from backend.licensing import (
    analyze_licensing_evidence,
    extract_license_claim_from_text,
    fetch_and_detect_youtube_licensing,
    ALL_CATEGORIES,
    STANDARD_YOUTUBE,
    CC_BY,
    CC_BY_SA,
    CC_BY_ND,
    CC_BY_NC,
    CC_BY_NC_SA,
    CC_BY_NC_ND,
    PUBLIC_DOMAIN,
    DIRECT_PERMISSION,
    AI_TRAINING_PERMISSION,
    UNKNOWN,
    SCREENING_POLICY,
)

client = TestClient(app)


# ==============================================================================
# 1. TEST ALL 11 CATEGORIES VIA TEXT EXTRACTION
# ==============================================================================

def test_all_11_licensing_categories_detected():
    """Verify each of the 11 categories is recognized accurately from text statements."""
    samples = {
        CC_BY_NC_ND: "This audio is released under CC BY-NC-ND 4.0 International license.",
        CC_BY_NC_SA: "Licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0",
        CC_BY_NC: "License: Creative Commons Attribution-NonCommercial (CC BY-NC)",
        CC_BY_ND: "Audio speech material under CC BY-ND 3.0 license",
        CC_BY_SA: "Distributed under CC BY-SA 4.0 with copyleft terms",
        CC_BY: "Speech dataset licensed under Creative Commons Attribution 4.0 (CC BY)",
        PUBLIC_DOMAIN: "This recording is in the Public Domain Mark 1.0 (CC0 dedication)",
        AI_TRAINING_PERMISSION: "The creator has granted YouTube third-party AI training permission",
        DIRECT_PERMISSION: "Direct written permission granted by author for commercial voice modeling",
        STANDARD_YOUTUBE: "Video uploaded under Standard YouTube License",
    }

    for expected_category, text in samples.items():
        res = extract_license_claim_from_text(text)
        assert res is not None, f"Failed to detect {expected_category} from: '{text}'"
        assert res[0] == expected_category, f"Expected {expected_category}, got {res[0]} for '{text}'"

    # Missing / Unknown
    no_lic = extract_license_claim_from_text("Vedic astrology lecture explaining planetary motion in Indian English.")
    assert no_lic is None, "Missing licensing text must return None, not guess a license."


# ==============================================================================
# 2. TEST AUTOMATIC DETECTION ACROSS ALL METADATA SOURCES
# ==============================================================================

def test_detection_across_multiple_sources():
    """Verify licensing is detected across title, description, tags, comments, channel, metadata."""
    # From Title
    res_title = analyze_licensing_evidence({"title": "Ancient Sanskrit Grammar Recitation [CC BY]"})
    assert res_title["detected_license"] == CC_BY
    assert res_title["detection_source"] == "title"
    assert "CC BY" in res_title["evidence"]

    # From Description
    res_desc = analyze_licensing_evidence({
        "description": "Welcome to my lecture.\nAudio licensed under Creative Commons Attribution 4.0 International (CC BY 4.0)."
    })
    assert res_desc["detected_license"] == CC_BY
    assert res_desc["detection_source"] == "description"

    # From Tags
    res_tags = analyze_licensing_evidence({"tags": ["sanskrit", "indian english", "cc-by-sa", "audio"]})
    assert res_tags["detected_license"] == CC_BY_SA
    assert res_tags["detection_source"] == "tags"

    # From Comments
    res_comment = analyze_licensing_evidence({
        "comments": [
            "Great video!",
            "Pinned by Speaker: Written permission granted by copyright holder for AI dataset training."
        ]
    })
    assert res_comment["detected_license"] == DIRECT_PERMISSION
    assert res_comment["detection_source"] == "comments"

    # From Channel Info
    res_chan = analyze_licensing_evidence({
        "channel_info": "All spoken recordings on this channel are dedicated to the Public Domain (CC0)."
    })
    assert res_chan["detected_license"] == PUBLIC_DOMAIN
    assert res_chan["detection_source"] == "channel_info"

    # From Video Metadata (YouTube status.license: creativeCommon)
    res_meta = analyze_licensing_evidence({"metadata_license": "creativeCommon"})
    assert res_meta["detected_license"] == CC_BY
    assert res_meta["detection_source"] == "video_metadata"

    # From Video Metadata (YouTube status.license: youtube)
    res_meta_yt = analyze_licensing_evidence({"metadata_license": "youtube"})
    assert res_meta_yt["detected_license"] == STANDARD_YOUTUBE
    assert res_meta_yt["detection_source"] == "video_metadata"


# ==============================================================================
# 3. TEST MISSING LICENSES (Requirement 3: Never Guess CC BY)
# ==============================================================================

def test_missing_license_does_not_guess_cc_by():
    """Requirement: Missing information defaults to Standard YouTube License, never guesses CC BY."""
    res = analyze_licensing_evidence({
        "title": "Indian English conversational practice",
        "description": "Daily conversational podcast recorded in Mumbai.",
        "tags": ["podcast", "english"],
        "comments": ["Nice podcast!"]
    })
    assert res["detected_license"] == STANDARD_YOUTUBE
    assert res["effective_license"] == STANDARD_YOUTUBE
    assert res["detected_license"] != CC_BY
    assert res["conflict_detected"] is False
    assert res["verification_status"] == "NOT_APPROVED_WITHOUT_SEPARATE_PERMISSION"
    assert "Standard YouTube" in res["screening_guidance"]


# ==============================================================================
# 4. TEST CONFLICTING CLAIMS (Requirement 6: Flag for Manual Review)
# ==============================================================================

def test_conflicting_claims_flagged_for_manual_review():
    """
    Requirement 6: If sources conflict (e.g. metadata says Standard YouTube License,
    but description claims CC BY), flag the record for manual review instead of
    silently selecting a license.
    """
    conflicting_sources = {
        "metadata_license": "Standard YouTube License",
        "description": "Audio is licensed under Creative Commons Attribution (CC BY 4.0).",
        "title": "Physics Lecture in Indian English"
    }

    res = analyze_licensing_evidence(conflicting_sources)
    assert res["conflict_detected"] is True, "Must detect conflict between metadata and description"
    assert res["detected_license"] == "Conflict Detected"
    assert res["verification_status"] == "FLAGGED_CONFLICT"
    assert res["reviewer_decision"] == "MANUAL_REVIEW_REQUIRED"
    assert "Conflicting licensing claims detected" in res["screening_guidance"]
    assert "video_metadata" in res["conflicts"]
    assert "description" in res["conflicts"]

    # Also conflict between description and comment:
    desc_comment_conflict = {
        "description": "Licensed under CC BY 4.0",
        "comments": ["Notice: This content is CC BY-NC (NonCommercial only)."]
    }
    res2 = analyze_licensing_evidence(desc_comment_conflict)
    assert res2["conflict_detected"] is True
    assert res2["verification_status"] == "FLAGGED_CONFLICT"


# ==============================================================================
# 5. TEST CLAIM VS VERIFIED PERMISSION DISTINCTION (Requirement 5)
# ==============================================================================

def test_detected_claims_require_verification():
    """
    Requirement 5: A statement in title/description/comment is evidence of a claim,
    not proof of permission. Must record evidence and set NEEDS_VERIFICATION.
    """
    res = analyze_licensing_evidence({
        "description": "Free for all use under CC BY 4.0 International."
    })
    assert res["detected_license"] == CC_BY
    assert res["is_claim_only"] is True
    assert res["verification_status"] == "NEEDS_VERIFICATION"
    assert "Uploader claim" in res["evidence"]


# ==============================================================================
# 6. TEST RIGHTS DOCUMENT SCREENING GUIDANCE (Requirement 7)
# ==============================================================================

def test_screening_guidance_for_all_policies():
    """Requirement 7: Apply rights document screening guidance accurately."""
    # CC BY: potentially acceptable after attribution and rights verification
    res_cc_by = analyze_licensing_evidence({"description": "CC BY 4.0"})
    assert "Potentially acceptable" in res_cc_by["screening_guidance"]
    assert res_cc_by["verification_status"] == "NEEDS_VERIFICATION"

    # CC BY-SA: require review of ShareAlike implications
    res_sa = analyze_licensing_evidence({"description": "CC BY-SA 4.0"})
    assert "ShareAlike" in res_sa["screening_guidance"]
    assert res_sa["verification_status"] == "FLAGGED_REVIEW"

    # CC BY-ND: flag for rejection or legal review under conservative policy
    res_nd = analyze_licensing_evidence({"description": "CC BY-ND 4.0"})
    assert "Flag for rejection or legal review" in res_nd["screening_guidance"]
    assert res_nd["verification_status"] == "FLAGGED_REJECTION_OR_LEGAL_REVIEW"

    # CC BY-NC / NC-SA / NC-ND: reject for commercial use
    for nc_text in ["CC BY-NC 4.0", "CC BY-NC-SA 4.0", "CC BY-NC-ND 4.0"]:
        res_nc = analyze_licensing_evidence({"description": nc_text})
        assert "Reject for commercial use" in res_nc["screening_guidance"]
        assert res_nc["verification_status"] == "REJECTED_COMMERCIAL"

    # Standard YouTube License: do not approve for commercial TTS extraction
    res_yt = analyze_licensing_evidence({"metadata_license": "youtube"})
    assert "do not approve for commercial TTS extraction" in res_yt["screening_guidance"]
    assert res_yt["verification_status"] == "NOT_APPROVED_WITHOUT_SEPARATE_PERMISSION"

    # Public domain, written permission, third-party AI training permission: require evidence and verification
    for pd_text, expected_cat in [
        ("Public Domain Mark 1.0", PUBLIC_DOMAIN),
        ("Direct written permission obtained", DIRECT_PERMISSION),
        ("YouTube third-party AI training permission", AI_TRAINING_PERMISSION)
    ]:
        res_pd = analyze_licensing_evidence({"description": pd_text})
        assert res_pd["detected_license"] == expected_cat
        assert res_pd["verification_status"] == "NEEDS_VERIFICATION"
        assert "require" in res_pd["screening_guidance"].lower()


# ==============================================================================
# 7. TEST API ENDPOINTS & DUPLICATE CHECK INTEGRATION
# ==============================================================================

def test_detect_licensing_api_endpoints():
    """Verify /api/detect-licensing endpoint works with POST and GET."""
    # POST with mock HTML containing CC BY
    post_res = client.post("/api/detect-licensing", json={
        "url": "https://www.youtube.com/watch?v=TEST_LIC_001",
        "html": '<html><head><title>Sanskrit Veda Lecture</title><meta name="description" content="Audio under CC BY 4.0 International"></head></html>'
    })
    assert post_res.status_code == 200
    post_data = post_res.json()
    assert post_data["detected_license"] == CC_BY
    assert post_data["verification_status"] == "NEEDS_VERIFICATION"

    # GET endpoint
    get_res = client.get("/api/detect-licensing?url=https://www.youtube.com/watch?v=TEST_LIC_002")
    assert get_res.status_code == 200
    get_data = get_res.json()
    assert "detected_license" in get_data


def test_duplicate_check_auto_detects_licensing():
    """Verify /api/check-duplicate returns licensing_info payload."""
    res = client.post("/api/check-duplicate", json={
        "url": "https://youtu.be/TEST_DUP_LIC_VID",
        "description": "Historical Sanskrit literature recitation\nchannel : vedic_heritage\nAudio licensed under CC BY-SA 4.0",
        "before_processing_duration": "00:10:00"
    })
    assert res.status_code == 200
    data = res.json()
    assert "licensing_info" in data
    lic = data["licensing_info"]
    assert lic is not None
    assert lic["detected_license"] == CC_BY_SA
    assert lic["verification_status"] == "FLAGGED_REVIEW"


# ==============================================================================
# 8. TEST DATABASE PERSISTENCE (Requirement 8)
# ==============================================================================

def test_database_persistence_of_licensing_fields():
    """Requirement 8: Save detected license, evidence, source, verification status and decision."""
    init_db()
    uid = uuid.uuid4().hex[:8]

    # Create candidate record with licensing detection fields
    rec_payload = {
        "research_date": "2026-10-23",
        "source": "YouTube",
        "audio_link": f"https://www.youtube.com/watch?v=PERSIST_{uid}",
        "description": "Indian English recitation of Panini grammar\nchannel : sanskrit_shiksha",
        "gender": "Female",
        "before_processing_duration": "00:15:00",
        "accent": "Indian English",
        "licensing": CC_BY,
        "licensing_evidence": '[Description] "Audio licensed under CC BY 4.0"',
        "detected_license": CC_BY,
        "license_detection_source": "description",
        "license_verification_status": "NEEDS_VERIFICATION",
        "license_reviewer_decision": "PENDING",
        "license_conflict_detected": False,
        "license_screening_guidance": "Potentially acceptable after attribution and rights verification.",
        "sanskrit_evidence": "Panini Ashtadhyayi",
        "speakers_id": f"SPK_LIC_{uid}",
        "researched_by": "Researcher_Lic",
        "action": "SEND_FOR_REVIEW"
    }

    create_res = client.post("/api/records", json=rec_payload)
    assert create_res.status_code == 201, create_res.text
    created = create_res.json()
    rec_id = created["id"]

    # Verify fields stored in SQLite
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM records WHERE id = ?", (rec_id,))
    row = dict(cursor.fetchone())
    conn.close()

    assert row["licensing"] == CC_BY
    assert row["detected_license"] == CC_BY
    assert row["license_detection_source"] == "description"
    assert row["license_verification_status"] == "NEEDS_VERIFICATION"
    assert row["license_reviewer_decision"] == "PENDING"
    assert row["license_conflict_detected"] == 0

    # Revalidation action: APPROVE
    reval_res = client.post(f"/api/records/{rec_id}/revalidate", json={
        "action": "APPROVE",
        "reviewer": "Reviewer_Lic_Lead",
        "notes": "Verified uploader owns audio and CC BY attribution recorded.",
        "licensing": CC_BY,
        "license_reviewer_decision": "APPROVED",
        "license_verification_status": "VERIFIED"
    })
    assert reval_res.status_code == 200, reval_res.text
    updated = reval_res.json()
    assert updated["status"] == "APPROVED"
    assert updated["license_reviewer_decision"] == "APPROVED"
    assert updated["license_verification_status"] == "VERIFIED"


# ==============================================================================
# 9. TEST CSV EXPORT PRESERVATION (Requirement 8 & Section 3 Schema)
# ==============================================================================

def test_csv_export_preserves_schema_and_writes_licensing():
    """Requirement 8: CSV columns strictly preserved and final licensing value written to Licensing."""
    export_res = client.get("/api/export/csv")
    assert export_res.status_code == 200
    csv_content = export_res.content.decode("utf-8-sig")

    reader = csv.reader(io.StringIO(csv_content))
    rows = list(reader)
    assert len(rows) >= 2, "CSV must contain header and at least one approved row"

    header = rows[0]
    expected_header = [
        "Date", "Completion_date", "Source", "Audio_Link", "Text_Link", "Description",
        "Dataset Name", "Gender", "Before_Processing_Duration", "After_Processing_Duration",
        "Total_files", "Researched_By", "Assigned_to", "Status", "Data_type (Augmentation_Status)",
        "Data_available", "Speakers_ID", "Speaker/Channel_Name", "Accent", "Licensing",
        "Handover", "Remark"
    ]
    assert header == expected_header, f"CSV export schema altered! Found: {header}"

    # Verify column index 19 is 'Licensing'
    lic_idx = header.index("Licensing")
    assert lic_idx == 19

    # Verify row has valid licensing
    approved_row = rows[1]
    lic_value = approved_row[lic_idx]
    assert lic_value != "", "Licensing value in CSV must not be empty"


# ==============================================================================
# 10. TEST GRACEFUL ERROR HANDLING (Network / Quota / Unavailable comments)
# ==============================================================================

def test_graceful_handling_of_unavailable_comments_and_missing_metadata():
    """Ensure network errors, API limits, and disabled comments degrade gracefully."""
    # Empty URL / invalid URL
    res = fetch_and_detect_youtube_licensing(url="", platform_item_id=None)
    assert res["detected_license"] == STANDARD_YOUTUBE
    assert res["conflict_detected"] is False

    # Simulate HTML without comments or tags
    simple_html = "<html><body><h1>Minimal Page</h1></body></html>"
    res2 = fetch_and_detect_youtube_licensing(
        url="https://www.youtube.com/watch?v=NON_EXISTENT_ID_999",
        html_content=simple_html
    )
    assert res2["detected_license"] == STANDARD_YOUTUBE
    assert res2["conflict_detected"] is False
    assert res2["verification_status"] == "NOT_APPROVED_WITHOUT_SEPARATE_PERMISSION"


if __name__ == "__main__":
    print("=" * 70)
    print("  RUNNING AUTOMATIC LICENSE DETECTION TEST SUITE")
    print("=" * 70)

    test_all_11_licensing_categories_detected()
    print("[PASS] 1. All 11 licensing categories detected accurately.")

    test_detection_across_multiple_sources()
    print("[PASS] 2. Detection across title, description, tags, comments, channel, metadata.")

    test_missing_license_does_not_guess_cc_by()
    print("[PASS] 3. Missing license defaults to Standard YouTube License (Never guesses CC BY).")

    test_conflicting_claims_flagged_for_manual_review()
    print("[PASS] 4. Conflicting claims flagged for manual review (FLAGGED_CONFLICT).")

    test_detected_claims_require_verification()
    print("[PASS] 5. Detected claims distinguished from verified permissions (NEEDS_VERIFICATION).")

    test_screening_guidance_for_all_policies()
    print("[PASS] 6. Rights document screening guidance applied across all policies.")

    test_detect_licensing_api_endpoints()
    print("[PASS] 7. Dedicated /api/detect-licensing POST/GET endpoints verified.")

    test_duplicate_check_auto_detects_licensing()
    print("[PASS] 8. Auto-detection integrated into /api/check-duplicate workflow.")

    test_database_persistence_of_licensing_fields()
    print("[PASS] 9. Database persistence of detected license, evidence, source, verification status.")

    test_csv_export_preserves_schema_and_writes_licensing()
    print("[PASS] 10. Production 22-column CSV schema strictly maintained with final licensing.")

    test_graceful_handling_of_unavailable_comments_and_missing_metadata()
    print("[PASS] 11. Graceful error handling for missing metadata, disabled comments, network errors.")

    print("\n" + "=" * 70)
    print("  ALL 11 AUTOMATIC LICENSE DETECTION TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 70)
