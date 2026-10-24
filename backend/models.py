"""
Pydantic models for data validation and API request/response payloads.
Adheres to final_implementation_architecture.md schemas.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class DuplicateCheckRequest(BaseModel):
    url: str
    speakers_id: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    channel: Optional[str] = None
    source: Optional[str] = None
    before_processing_duration: Optional[str] = None

class DuplicateCheckResponse(BaseModel):
    is_duplicate: bool = False
    status: str = Field(..., description="'DUPLICATE', 'POTENTIAL_DUPLICATE', 'UNIQUE', or 'FETCH_FAILED'")
    matched_video_id: Optional[str] = None
    reason: str
    evidence_fields: List[str] = []
    confidence: float = 0.0
    prefilter_triggered: bool = False
    llm_called: bool = False
    request_latency_ms: float = 0.0
    endpoint_version: Optional[str] = None
    canonical_url: str
    platform: Optional[str] = "YouTube"
    platform_item_id: Optional[str] = None
    existing_record: Optional[Dict[str, Any]] = None
    similar_records: List[Dict[str, Any]] = []
    metadata: Optional[Dict[str, Any]] = None
    speaker_duration_check: Optional[Dict[str, Any]] = None
    licensing_info: Optional[Dict[str, Any]] = None

class RecordCreateRequest(BaseModel):
    research_date: str
    source: str
    audio_link: str
    description: str
    gender: str
    before_processing_duration: str
    accent: str = "Indian English"
    licensing: str
    licensing_evidence: Optional[str] = ""
    sanskrit_evidence: Optional[str] = ""
    speakers_id: str
    researched_by: str
    action: str = Field(..., description="'SEND_FOR_REVIEW', 'REJECT', or 'SAVE_DRAFT'")
    rejection_reason: Optional[str] = None
    duplicate_status: Optional[str] = "UNIQUE"
    matched_video_id: Optional[str] = None
    decision_reason: Optional[str] = None
    evidence_fields: Optional[List[str]] = []
    completion_date: Optional[str] = ""
    text_link: Optional[str] = "ASR"
    dataset_name: Optional[str] = ""
    after_processing_duration: Optional[str] = "0:00:00"
    total_files: Optional[str] = ""
    assigned_to: Optional[str] = ""
    data_type_augmentation_status: Optional[str] = "Original"
    data_available: Optional[str] = "Lightining AV/HF"
    speaker_channel_name: Optional[str] = ""
    handover: Optional[str] = "No"
    remark: Optional[str] = ""
    detected_license: Optional[str] = None
    license_detection_source: Optional[str] = None
    license_verification_status: Optional[str] = None
    license_reviewer_decision: Optional[str] = "PENDING"
    license_conflict_detected: Optional[bool] = False
    license_screening_guidance: Optional[str] = None

class RecordRevalidateRequest(BaseModel):
    action: str = Field(..., description="'APPROVE', 'REJECT', or 'SAVE_ONLY'")
    reviewer: str
    rejection_reason: Optional[str] = None
    notes: Optional[str] = None
    dataset_name: Optional[str] = None
    assigned_to: Optional[str] = None
    handover: Optional[str] = None
    data_available: Optional[str] = None

    # Content fields editable by the revalidator during review
    audio_link: Optional[str] = None
    speaker_channel_name: Optional[str] = None
    speakers_id: Optional[str] = None
    gender: Optional[str] = None
    before_processing_duration: Optional[str] = None
    accent: Optional[str] = None
    source: Optional[str] = None
    licensing: Optional[str] = None
    licensing_evidence: Optional[str] = None
    detected_license: Optional[str] = None
    license_detection_source: Optional[str] = None
    license_verification_status: Optional[str] = None
    license_reviewer_decision: Optional[str] = None
    license_conflict_detected: Optional[bool] = None
    license_screening_guidance: Optional[str] = None
    sanskrit_evidence: Optional[str] = None
    description: Optional[str] = None

class RecordUpdateRequest(BaseModel):
    research_date: Optional[str] = None
    source: Optional[str] = None
    audio_link: Optional[str] = None
    description: Optional[str] = None
    gender: Optional[str] = None
    before_processing_duration: Optional[str] = None
    accent: Optional[str] = None
    licensing: Optional[str] = None
    licensing_evidence: Optional[str] = None
    detected_license: Optional[str] = None
    license_detection_source: Optional[str] = None
    license_verification_status: Optional[str] = None
    license_reviewer_decision: Optional[str] = None
    license_conflict_detected: Optional[bool] = None
    license_screening_guidance: Optional[str] = None
    sanskrit_evidence: Optional[str] = None
    speakers_id: Optional[str] = None
    researched_by: Optional[str] = None
    status: Optional[str] = None
    duplicate_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    completion_date: Optional[str] = None
    text_link: Optional[str] = None
    dataset_name: Optional[str] = None
    after_processing_duration: Optional[str] = None
    total_files: Optional[str] = None
    assigned_to: Optional[str] = None
    data_type_augmentation_status: Optional[str] = None
    data_available: Optional[str] = None
    speaker_channel_name: Optional[str] = None
    handover: Optional[str] = None
    remark: Optional[str] = None
    actor: Optional[str] = "System"

class CsvUploadRequest(BaseModel):
    csv_text: str
    target_status: Optional[str] = "APPROVED"

class LLMConfigUpdateRequest(BaseModel):
    base_url: Optional[str] = None
    endpoint_path: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    top_k_matches: Optional[int] = None

class LLMTestRequest(BaseModel):
    base_url: Optional[str] = None
    endpoint_path: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    test_title: Optional[str] = "Ancient Indian Vedic Chanting and Grammar"
    test_channel: Optional[str] = "swapnalok"
    test_duration: Optional[str] = "00:18:35"

class YouTubeTestRequest(BaseModel):
    api_key: Optional[str] = None
    sample_video_id: Optional[str] = "dQw4w9WgXcQ"

class WebVerifyRequest(BaseModel):
    url: str
    html_content: Optional[str] = None
    researched_by: Optional[str] = "Web Researcher"
    auto_store: Optional[bool] = False

class WebVerifyStoreRequest(BaseModel):
    original_url: str
    canonical_url: str
    final_url: Optional[str] = None
    domain: Optional[str] = None
    title: Optional[str] = None
    headings: Optional[List[str]] = []
    author: Optional[str] = None
    publication_date: Optional[str] = None
    description: Optional[str] = None
    raw_text: Optional[str] = None
    normalized_text: Optional[str] = None
    content_hash: str
    passages: Optional[List[str]] = []
    status: str = "UNIQUE"
    matched_page_id: Optional[str] = None
    matched_url: Optional[str] = None
    prefilter_status: Optional[str] = None
    prefilter_score: Optional[float] = 0.0
    lexical_score: Optional[float] = 0.0
    passage_score: Optional[float] = 0.0
    title_score: Optional[float] = 0.0
    decision_reason: Optional[str] = None
    evidence_fields: Optional[List[str]] = []
    llm_called: Optional[bool] = False
    confidence: Optional[float] = 1.0
    latency_ms: Optional[float] = 0.0
    researched_by: Optional[str] = "Web Researcher"
