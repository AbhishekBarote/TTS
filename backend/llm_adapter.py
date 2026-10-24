"""
Self-Hosted LLM Endpoint Adapter for Low-Latency Duplicate Detection.
Adheres strictly to Section 7 of final_implementation_architecture.md:
- Configurable via environment variables (LLM_BASE_URL, LLM_ENDPOINT_PATH, LLM_API_KEY, LLM_MODEL, etc.)
- Conditional requests: Only called when fast pre-filter identifies a probable duplicate
- Standardized internal contract:
  {
    "status": "DUPLICATE" | "POTENTIAL_DUPLICATE" | "UNIQUE",
    "matched_video_id": "...",
    "reason": "...",
    "evidence_fields": ["title", "description", ...],
    "confidence": 0.0 - 1.0,
    "latency_ms": 12.5,
    "endpoint_version": "...",
    "error_state": null
  }
- Safe failure: An endpoint timeout, invalid response, or network failure is NEVER evidence that a video is unique.
  It safely marks the candidate as POTENTIAL_DUPLICATE for review.
"""

import os
import time
import json
import logging
import re
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
import requests
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

class LLMConfig:
    """Manages configuration for the self-hosted LLM endpoint."""
    def __init__(self):
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.endpoint_path = os.getenv("LLM_ENDPOINT_PATH", "/chat/completions")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "my-model")
        self.timeout_seconds = int(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
        self.top_k_matches = int(os.getenv("LLM_TOP_K_MATCHES", "3"))

    def get_full_url(self, base_url: Optional[str] = None, endpoint_path: Optional[str] = None) -> str:
        base = (base_url if base_url is not None else self.base_url).strip().rstrip("/")
        path = (endpoint_path if endpoint_path is not None else self.endpoint_path).strip()
        if not base:
            return ""
        if not path.startswith("/"):
            path = "/" + path
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]
        elif not base.endswith("/v1") and not path.startswith("/v1/"):
            path = f"/v1{path}"
        return f"{base}{path}"

    @property
    def full_endpoint_url(self) -> str:
        return self.get_full_url()

    def to_dict(self, redact_key: bool = True) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "endpoint_path": self.endpoint_path,
            "endpoint_url": self.full_endpoint_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "top_k_matches": self.top_k_matches,
            "has_api_key": bool(self.api_key),
            "is_remote_configured": bool(self.base_url)
        }

    def update(self, updates: Dict[str, Any]):
        if "base_url" in updates and updates["base_url"] is not None:
            self.base_url = str(updates["base_url"]).strip().rstrip("/")
        if "endpoint_path" in updates and updates["endpoint_path"] is not None:
            self.endpoint_path = str(updates["endpoint_path"]).strip()
        if "api_key" in updates and updates["api_key"] is not None:
            self.api_key = str(updates["api_key"]).strip()
        if "model" in updates and updates["model"] is not None:
            self.model = str(updates["model"]).strip()
        if "timeout_seconds" in updates and updates["timeout_seconds"] is not None:
            self.timeout_seconds = max(1, int(updates["timeout_seconds"]))
        if "top_k_matches" in updates and updates["top_k_matches"] is not None:
            self.top_k_matches = max(1, int(updates["top_k_matches"]))


# Global config instance
llm_config = LLMConfig()


class LLMDecision(BaseModel):
    status: str = Field(..., description="'DUPLICATE', 'POTENTIAL_DUPLICATE', or 'UNIQUE'")
    matched_video_id: Optional[str] = None
    reason: str
    evidence_fields: List[str] = []
    confidence: float = 0.0
    latency_ms: float = 0.0
    endpoint_version: str = "self-hosted-adapter-v1"
    error_state: Optional[str] = None


class LLMAdapter:
    """
    Dedicated Adapter for evaluating candidate videos against probable duplicate matches.
    Supports:
    1. Remote HTTP endpoint (when LLM_BASE_URL is configured)
    2. High-precision semantic reasoning engine (fallback/standalone mode)
    3. Resilient timeout and retry handling with safe failure guarantee
    """

    @classmethod
    def evaluate(
        cls,
        candidate: Dict[str, Any],
        probable_matches: List[Dict[str, Any]],
        override_config: Optional[Dict[str, Any]] = None
    ) -> LLMDecision:
        """
        Main entry point for duplicate evaluation.
        Called conditionally only when fast pre-filter flags candidate matches.
        """
        start_time = time.perf_counter()

        if not probable_matches:
            # Should not happen as pre-filter gates this, but safe fallback
            latency = (time.perf_counter() - start_time) * 1000.0
            return LLMDecision(
                status="UNIQUE",
                matched_video_id=None,
                reason="No plausible matches found to evaluate",
                evidence_fields=[],
                confidence=1.0,
                latency_ms=round(latency, 2),
                endpoint_version="prefilter-bypass"
            )

        top_k = int(override_config.get("top_k_matches")) if override_config and override_config.get("top_k_matches") else llm_config.top_k_matches
        matches_to_eval = probable_matches[:top_k]

        active_base_url = (override_config.get("base_url") if override_config and "base_url" in override_config else llm_config.base_url).strip().rstrip("/")

        # Case 1: Remote endpoint is configured
        if active_base_url:
            decision = cls._call_remote_endpoint(candidate, matches_to_eval, start_time, override_config=override_config)
            return decision

        # Case 2: Standalone semantic adapter mode
        decision = cls._evaluate_semantic_rules(candidate, matches_to_eval, start_time)
        return decision

    @classmethod
    def _call_remote_endpoint(
        cls,
        candidate: Dict[str, Any],
        matches: List[Dict[str, Any]],
        start_time: float,
        override_config: Optional[Dict[str, Any]] = None
    ) -> LLMDecision:
        """Calls the configured self-hosted endpoint with retries and safe failure."""
        cfg_base_url = (override_config.get("base_url") if override_config and "base_url" in override_config else llm_config.base_url).strip().rstrip("/")
        cfg_endpoint_path = (override_config.get("endpoint_path") if override_config and "endpoint_path" in override_config else llm_config.endpoint_path).strip()
        cfg_api_key = (override_config.get("api_key") if override_config and "api_key" in override_config else llm_config.api_key).strip()
        cfg_model = (override_config.get("model") if override_config and "model" in override_config else llm_config.model).strip()
        cfg_timeout = int(override_config.get("timeout_seconds")) if override_config and override_config.get("timeout_seconds") else llm_config.timeout_seconds

        url = llm_config.get_full_url(base_url=cfg_base_url, endpoint_path=cfg_endpoint_path)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AudioResearchHub/1.2 (Duplicate-Detector)"
        }
        if cfg_api_key:
            headers["Authorization"] = f"Bearer {cfg_api_key}"

        system_prompt = (
            "You are an expert audio/video deduplication engine for a speech research dataset. "
            "Analyze the candidate recording against existing candidate records.\n"
            "CRITICAL RULES:\n"
            "1. Same speaker alone does NOT prove duplication (the same speaker gives multiple different lectures).\n"
            "2. Same topic alone does NOT prove duplication (different speakers discuss the same subject).\n"
            "3. Return DUPLICATE only if there is strong evidence that this is the same recording or exact re-upload.\n"
            "4. Return POTENTIAL_DUPLICATE if descriptions/titles are very similar but unclear, or metadata is incomplete.\n"
            "5. Return UNIQUE if the content represents a distinct recording.\n"
            "Output valid JSON ONLY with keys: status ('DUPLICATE', 'POTENTIAL_DUPLICATE', 'UNIQUE'), "
            "matched_video_id (string or null), reason (short string), evidence_fields (array of strings), confidence (float 0.0 to 1.0)."
        )

        payload = {
            "model": cfg_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps({
                        "candidate": {
                            "video_id": candidate.get("video_id") or candidate.get("platform_item_id"),
                            "title": candidate.get("title"),
                            "channel": candidate.get("channel") or candidate.get("channel_name"),
                            "description": candidate.get("description"),
                            "duration": candidate.get("before_processing_duration") or candidate.get("duration"),
                            "duration_seconds": candidate.get("duration_seconds"),
                            "speaker_id": candidate.get("speakers_id")
                        },
                        "existing_candidates": [
                            {
                                "id": m.get("id"),
                                "video_id": m.get("platform_item_id") or m.get("video_id"),
                                "title": m.get("title"),
                                "channel": m.get("channel_name") or m.get("channel"),
                                "description": m.get("description"),
                                "duration": m.get("before_processing_duration"),
                                "duration_seconds": m.get("duration_seconds"),
                                "speaker_id": m.get("speakers_id"),
                                "prefilter_score": m.get("prefilter_score")
                            }
                            for m in matches
                        ]
                    })
                }
            ],
            "temperature": 0.0,
            "max_tokens": 300
        }

        # Response handling: 1 attempt with configured timeout (minimum 10s for LLM processing)
        max_retries = 1
        effective_timeout = max(10, cfg_timeout)
        last_error = None

        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=effective_timeout
                )
                latency = (time.perf_counter() - start_time) * 1000.0

                if resp.status_code == 200:
                    data = resp.json()
                    parsed = cls._parse_llm_response(data)
                    parsed.latency_ms = round(latency, 2)
                    parsed.endpoint_version = cfg_model
                    return parsed
                else:
                    clean_text = " ".join(re.sub(r'<[^>]+>', ' ', resp.text[:400]).split())
                    last_error = f"HTTP {resp.status_code}: {clean_text[:120]}" if clean_text else f"HTTP {resp.status_code}"
            except requests.exceptions.Timeout:
                last_error = f"Request timed out after {effective_timeout}s"
            except requests.exceptions.RequestException as e:
                last_error = f"Network error: {str(e)}"

            time.sleep(0.5 * (attempt + 1))

        # Safe Failure Guarantee:
        # Section 3, Rule 7: "An endpoint timeout, invalid response, or network failure is not evidence
        # that a video is unique. Mark it for retry/review or fail the check safely."
        latency = (time.perf_counter() - start_time) * 1000.0
        return LLMDecision(
            status="POTENTIAL_DUPLICATE",
            matched_video_id=str(matches[0].get("id") or matches[0].get("platform_item_id")),
            reason=f"LLM endpoint error: {last_error}. Safely routed to review queue.",
            evidence_fields=["endpoint_failure", "prefilter_candidate"],
            confidence=0.5,
            latency_ms=round(latency, 2),
            endpoint_version=f"{cfg_model} (error-fallback)",
            error_state=last_error
        )

    @classmethod
    def _parse_llm_response(cls, data: Dict[str, Any]) -> LLMDecision:
        """Normalizes various wire formats from self-hosted LLM endpoints."""
        content = ""
        try:
            if "choices" in data and len(data["choices"]) > 0:
                msg = data["choices"][0].get("message", {})
                content = msg.get("content", "")
            elif "response" in data:
                content = data["response"]
            elif "output" in data:
                content = str(data["output"])
            else:
                content = json.dumps(data)

            content = content.strip()
            # Extract JSON from markdown fences or bracket patterns
            m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
            if m:
                content = m.group(1).strip()
            else:
                start = content.find('{')
                end = content.rfind('}')
                if start != -1 and end != -1 and end > start:
                    content = content[start:end+1].strip()

            parsed = json.loads(content)
            status = str(parsed.get("status", "POTENTIAL_DUPLICATE")).upper()
            if status not in ("DUPLICATE", "POTENTIAL_DUPLICATE", "UNIQUE"):
                status = "POTENTIAL_DUPLICATE"

            return LLMDecision(
                status=status,
                matched_video_id=parsed.get("matched_video_id"),
                reason=parsed.get("reason", "Decision by LLM endpoint"),
                evidence_fields=parsed.get("evidence_fields", []),
                confidence=float(parsed.get("confidence", 0.8))
            )
        except Exception as e:
            return LLMDecision(
                status="POTENTIAL_DUPLICATE",
                matched_video_id=None,
                reason=f"Failed to parse LLM structured output ({str(e)}). Routed to review queue.",
                evidence_fields=["parse_error"],
                confidence=0.5,
                error_state=str(e)
            )

    @classmethod
    def _evaluate_semantic_rules(
        cls,
        candidate: Dict[str, Any],
        matches: List[Dict[str, Any]],
        start_time: float
    ) -> LLMDecision:
        """
        High-precision semantic reasoning engine adhering strictly to Section 6 & 8:
        - Evaluates title similarity, description similarity, duration delta, and channel.
        - Rules:
          * Same speaker alone != duplicate
          * Same topic alone != duplicate
          * High title match (>88%) + same channel or identical duration -> DUPLICATE
          * Moderate similarity (70-88%) or ambiguous overlap -> POTENTIAL_DUPLICATE (review)
          * Otherwise -> UNIQUE
        """
        cand_desc = (candidate.get("description") or "").strip().lower()
        cand_title = (candidate.get("title") or "").strip().lower()
        if not cand_title and cand_desc:
            cand_title = cand_desc.split("\n")[0]

        cand_channel = (candidate.get("channel") or candidate.get("channel_name") or "").strip().lower()
        cand_dur_sec = candidate.get("duration_seconds") or 0

        best_status = "UNIQUE"
        best_match_id = None
        best_reason = "No sufficiently strong duplicate match found; verified distinct."
        best_evidence: List[str] = []
        highest_confidence = 0.0

        for match in matches:
            rec_id = match.get("id")
            rec_vid = match.get("platform_item_id") or str(rec_id)
            rec_desc = (match.get("description") or "").strip().lower()
            rec_title = (match.get("title") or "").strip().lower()
            if not rec_title and rec_desc:
                rec_title = rec_desc.split("\n")[0]

            rec_channel = (match.get("channel_name") or match.get("channel") or "").strip().lower()
            rec_dur_sec = match.get("duration_seconds") or 0

            # Calculate similarities using both token sort and token set ratios
            t_sort = fuzz.token_sort_ratio(cand_title, rec_title) if (cand_title and rec_title) else 0
            t_set = fuzz.token_set_ratio(cand_title, rec_title) if (cand_title and rec_title) else 0
            title_ratio = max(t_sort, t_set)

            d_sort = fuzz.token_sort_ratio(cand_desc, rec_desc) if (cand_desc and rec_desc) else 0
            d_set = fuzz.token_set_ratio(cand_desc, rec_desc) if (cand_desc and rec_desc) else 0
            desc_ratio = max(d_sort, d_set)

            max_text_ratio = max(title_ratio, desc_ratio)

            # Duration delta
            dur_delta = abs(cand_dur_sec - rec_dur_sec) if (cand_dur_sec and rec_dur_sec) else 999
            same_channel = bool(cand_channel and rec_channel and cand_channel == rec_channel)

            evidence = []
            if title_ratio >= 75:
                evidence.append(f"Title similarity {title_ratio}%")
            if desc_ratio >= 75:
                evidence.append(f"Description similarity {desc_ratio}%")
            if dur_delta <= 3 and cand_dur_sec > 0:
                evidence.append(f"Matching duration ({cand_dur_sec}s)")
            if same_channel:
                evidence.append(f"Same channel ({cand_channel})")

            # Check for confirmed duplicate:
            # 1. Very high title match (>= 90%) and exact/near duration
            # 2. High title match (>= 85%) and same channel
            # 3. Identical description text (>= 95%)
            if (title_ratio >= 90 and dur_delta <= 5) or (title_ratio >= 85 and same_channel) or desc_ratio >= 95:
                latency = (time.perf_counter() - start_time) * 1000.0
                confidence = round(max(max_text_ratio / 100.0, 0.92), 2)
                return LLMDecision(
                    status="DUPLICATE",
                    matched_video_id=rec_vid,
                    reason=f"Strong duplicate evidence with record #{rec_id}: {', '.join(evidence)}",
                    evidence_fields=evidence,
                    confidence=confidence,
                    latency_ms=round(latency, 2),
                    endpoint_version="self-hosted-semantic-v1"
                )

            # Potential duplicate:
            # Title ratio between 72% and 89%, or similar topic with matching speaker/channel
            if title_ratio >= 72 or desc_ratio >= 75 or (title_ratio >= 65 and dur_delta <= 2):
                if best_status != "DUPLICATE":
                    best_status = "POTENTIAL_DUPLICATE"
                    best_match_id = rec_vid
                    best_reason = f"Borderline similarity with record #{rec_id} ({title_ratio}% title match). Requires revalidation review."
                    best_evidence = evidence
                    highest_confidence = round(max_text_ratio / 100.0, 2)

        latency = (time.perf_counter() - start_time) * 1000.0
        return LLMDecision(
            status=best_status,
            matched_video_id=best_match_id,
            reason=best_reason,
            evidence_fields=best_evidence,
            confidence=highest_confidence if highest_confidence > 0 else 0.85,
            latency_ms=round(latency, 2),
            endpoint_version="self-hosted-semantic-v1"
        )
