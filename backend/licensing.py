"""
Automatic License Detection Engine for YouTube Audio Research Workflow.

Implements rights document screening guidance, multi-source claim detection,
conflict identification, and claim vs verified permission differentiation.

Categories supported:
- Standard YouTube License
- CC BY
- CC BY-SA
- CC BY-ND
- CC BY-NC
- CC BY-NC-SA
- CC BY-NC-ND
- Public Domain
- Direct Written Permission
- YouTube Third-Party AI Training Permission
- Unknown / Not Specified
"""

import os
import re
import json
import logging
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, parse_qs
import requests

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# 1. STANDARDIZED LICENSING CATEGORIES (Rights Document)
# ------------------------------------------------------------------------------

STANDARD_YOUTUBE = "Standard YouTube License"
CC_BY = "CC BY"
CC_BY_SA = "CC BY-SA"
CC_BY_ND = "CC BY-ND"
CC_BY_NC = "CC BY-NC"
CC_BY_NC_SA = "CC BY-NC-SA"
CC_BY_NC_ND = "CC BY-NC-ND"
PUBLIC_DOMAIN = "Public Domain"
DIRECT_PERMISSION = "Direct Written Permission"
AI_TRAINING_PERMISSION = "YouTube Third-Party AI Training Permission"
UNKNOWN = "Unknown / Not Specified"

ALL_CATEGORIES = [
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
]

# ------------------------------------------------------------------------------
# 2. SCREENING GUIDANCE & VERIFICATION RULES (Rights Document)
# ------------------------------------------------------------------------------

SCREENING_POLICY = {
    CC_BY: {
        "guidance": "Potentially acceptable after attribution and rights verification.",
        "default_verification_status": "NEEDS_VERIFICATION",
        "commercial_acceptable": "CONDITIONAL",
        "requires_verification": True,
        "note": "Attribution and uploader rights ownership must be verified before commercial speech training.",
        "policy_decision": "ACCEPT",
        "policy_reason": "Genuine CC BY; uploader has rights (Attribution & rights verification required)",
        "plain_title": "Likely Allowed for AI Voice Training (Credit Required)",
        "badge_level": "SAFE",
        "badge_icon": "fa-circle-check",
        "badge_label": "LIKELY USABLE (CREDIT REQUIRED)",
        "what_it_means": "The creator tagged this as Creative Commons (CC BY). Anyone is free to reuse, modify, and train AI voice models on this audio, as long as proper credit (attribution) is given to the creator.",
        "action_steps": [
            "Check that this is the uploader's own original voice (not a copyrighted TV show, movie clip, or pop song).",
            "Save the creator's channel name and video link for attribution credit."
        ]
    },
    CC_BY_SA: {
        "guidance": "Require review of ShareAlike implications.",
        "default_verification_status": "FLAGGED_REVIEW",
        "commercial_acceptable": "CONDITIONAL",
        "requires_verification": True,
        "note": "ShareAlike requires derived datasets or models to be released under the same license.",
        "policy_decision": "REVIEW",
        "policy_reason": "CC BY-SA (Legal review of ShareAlike implications required)",
        "plain_title": "ShareAlike Copyleft License — Team Lead Approval Required",
        "badge_level": "CAUTION",
        "badge_icon": "fa-triangle-exclamation",
        "badge_label": "LEGAL REVIEW NEEDED (SHAREALIKE)",
        "what_it_means": "The creator allows reuse, but 'ShareAlike' means any new AI model or dataset built from this voice must also be published openly under the exact same open-source terms.",
        "action_steps": [
            "Check if our project allows publishing the final model openly under CC BY-SA.",
            "Ask team lead/reviewer for clearance before submitting."
        ]
    },
    CC_BY_ND: {
        "guidance": "Flag for rejection or legal review under the conservative policy.",
        "default_verification_status": "FLAGGED_REJECTION_OR_LEGAL_REVIEW",
        "commercial_acceptable": "REJECTED_BY_DEFAULT",
        "requires_verification": True,
        "note": "NoDerivatives restriction prohibits creating adapted voice models unless explicit legal waiver granted.",
        "policy_decision": "REJECT / LEGAL REVIEW",
        "policy_reason": "CC BY-ND (Do not use under conservative AI-training approach)",
        "plain_title": "No Derivatives — Cannot Be Used for AI Voice Training",
        "badge_level": "DANGER",
        "badge_icon": "fa-circle-xmark",
        "badge_label": "CANNOT USE (NO DERIVATIVES)",
        "what_it_means": "The creator tagged this as 'NoDerivatives' (ND), which explicitly forbids altering, adapting, or synthesizing their voice into an AI model.",
        "action_steps": [
            "Do not submit this audio for speech synthesis or TTS modeling.",
            "Find an alternative recording with CC BY or Public Domain instead."
        ]
    },
    CC_BY_NC: {
        "guidance": "Reject for commercial use under the documented policy.",
        "default_verification_status": "REJECTED_COMMERCIAL",
        "commercial_acceptable": "REJECTED",
        "requires_verification": True,
        "note": "NonCommercial restriction prohibits commercial TTS extraction.",
        "policy_decision": "REJECT for commercial use",
        "policy_reason": "CC BY-NC (Commercial use prohibited by license)",
        "plain_title": "Non-Commercial Only — Cannot Train Commercial AI",
        "badge_level": "DANGER",
        "badge_icon": "fa-ban",
        "badge_label": "REJECTED (NON-COMMERCIAL)",
        "what_it_means": "'NonCommercial' (NC) means the creator strictly prohibited commercial applications, businesses, or commercial AI training on this audio.",
        "action_steps": [
            "Do not submit this audio for commercial TTS training.",
            "Look for an alternative video with open commercial rights (CC BY or Public Domain)."
        ]
    },
    CC_BY_NC_SA: {
        "guidance": "Reject for commercial use under the documented policy.",
        "default_verification_status": "REJECTED_COMMERCIAL",
        "commercial_acceptable": "REJECTED",
        "requires_verification": True,
        "note": "NonCommercial and ShareAlike restrictions prohibit commercial TTS extraction.",
        "policy_decision": "REJECT for commercial use",
        "policy_reason": "CC BY-NC-SA (Commercial use prohibited by license)",
        "plain_title": "Non-Commercial & ShareAlike — Cannot Train Commercial AI",
        "badge_level": "DANGER",
        "badge_icon": "fa-ban",
        "badge_label": "REJECTED (NON-COMMERCIAL)",
        "what_it_means": "Restricted by both NonCommercial and ShareAlike terms. It cannot be used in a commercial speech dataset.",
        "action_steps": [
            "Do not submit this audio for commercial TTS.",
            "Select an alternative video with unrestricted commercial permissions."
        ]
    },
    CC_BY_NC_ND: {
        "guidance": "Reject for commercial use under the documented policy.",
        "default_verification_status": "REJECTED_COMMERCIAL",
        "commercial_acceptable": "REJECTED",
        "requires_verification": True,
        "note": "NonCommercial and NoDerivatives restrictions strictly prohibit commercial TTS adaptation.",
        "policy_decision": "REJECT for commercial use",
        "policy_reason": "CC BY-NC-ND (Commercial use prohibited by license)",
        "plain_title": "Non-Commercial & No Derivatives — Strictly Prohibited",
        "badge_level": "DANGER",
        "badge_icon": "fa-ban",
        "badge_label": "REJECTED (STRICT PROHIBITION)",
        "what_it_means": "This audio is strictly locked: no commercial use and no synthetic voice modifications allowed.",
        "action_steps": [
            "Exclude this recording from the dataset.",
            "Choose an alternative recording with open commercial rights."
        ]
    },
    STANDARD_YOUTUBE: {
        "guidance": "Standard YouTube License without separate permission: do not approve for commercial TTS extraction.",
        "default_verification_status": "NOT_APPROVED_WITHOUT_SEPARATE_PERMISSION",
        "commercial_acceptable": "NOT_APPROVED",
        "requires_verification": True,
        "note": "Standard YouTube license grants playback on platform only; separate written permission required for TTS extraction.",
        "policy_decision": "REJECT",
        "policy_reason": "Standard YouTube license with no separate permission (No dataset extraction)",
        "plain_title": "Standard YouTube Copyright — Cannot Train AI Without Permission",
        "badge_level": "DANGER",
        "badge_icon": "fa-copyright",
        "badge_label": "DO NOT USE WITHOUT WRITTEN PERMISSION",
        "what_it_means": "This video is under Standard YouTube Copyright. You can watch it on YouTube, but downloading or training AI voice models on it is NOT permitted without separate written permission.",
        "action_steps": [
            "Do not approve or submit for commercial dataset unless you have an archived written agreement from the creator.",
            "If the creator explicitly granted permission in writing, verify and select 'Direct Written Permission'."
        ]
    },
    PUBLIC_DOMAIN: {
        "guidance": "Public domain: require appropriate evidence and verification.",
        "default_verification_status": "NEEDS_VERIFICATION",
        "commercial_acceptable": "CONDITIONAL",
        "requires_verification": True,
        "note": "Must verify historical Public Domain Mark, CC0 dedication, or official government author status.",
        "policy_decision": "ACCEPT",
        "policy_reason": "Verified public-domain recording",
        "plain_title": "Public Domain / CC0 — Free for All Uses",
        "badge_level": "SAFE",
        "badge_icon": "fa-certificate",
        "badge_label": "FREE TO USE (PUBLIC DOMAIN)",
        "what_it_means": "This recording has no copyright restrictions (CC0 / Public Domain). Anyone can use it freely for commercial AI voice training.",
        "action_steps": [
            "Check that it is a genuine historical recording, official government work, or CC0 dedication.",
            "Record the source or archive link as proof."
        ]
    },
    DIRECT_PERMISSION: {
        "guidance": "Direct written permission: require documentation and verification of explicit written permission.",
        "default_verification_status": "NEEDS_VERIFICATION",
        "commercial_acceptable": "CONDITIONAL",
        "requires_verification": True,
        "note": "Written authorization from copyright holder authorizing commercial TTS training must be archived.",
        "policy_decision": "ACCEPT",
        "policy_reason": "Written permission explicitly covering commercial AI/TTS training",
        "plain_title": "Direct Written Permission from Copyright Holder",
        "badge_level": "SAFE",
        "badge_icon": "fa-file-signature",
        "badge_label": "ALLOWED (WRITTEN PERMISSION)",
        "what_it_means": "The creator or copyright owner directly gave written authorization to use their voice for AI speech synthesis.",
        "action_steps": [
            "Attach or archive the written consent (email, contract, or signed agreement).",
            "Verify the permission explicitly includes commercial speech modeling."
        ]
    },
    AI_TRAINING_PERMISSION: {
        "guidance": "YouTube third-party AI training permission: require appropriate evidence and verification.",
        "default_verification_status": "NEEDS_VERIFICATION",
        "commercial_acceptable": "CONDITIONAL",
        "requires_verification": True,
        "note": "Must verify third-party AI training permission statement or platform-level licensing opt-in.",
        "policy_decision": "ACCEPT, subject to verification",
        "policy_reason": "YouTube third-party AI training enabled for relevant rights holder",
        "plain_title": "Third-Party AI Training Authorized",
        "badge_level": "SAFE",
        "badge_icon": "fa-robot",
        "badge_label": "ALLOWED FOR AI TRAINING",
        "what_it_means": "The creator authorized third-party AI voice and dataset training on this content.",
        "action_steps": [
            "Verify the third-party AI training permission statement in the video description or platform settings."
        ]
    },
    UNKNOWN: {
        "guidance": "No licensing information found. Manual selection and rights verification required. Do not assume CC BY.",
        "default_verification_status": "UNSPECIFIED",
        "commercial_acceptable": "NOT_APPROVED",
        "requires_verification": True,
        "note": "Researcher must manually select licensing and verify permissions. Missing info is never assumed to be CC BY.",
        "policy_decision": "REJECT",
        "policy_reason": "Unknown / unclear ownership (Insufficient rights evidence)",
        "plain_title": "No License Detected — Manual Check Required",
        "badge_level": "GRAY",
        "badge_icon": "fa-question-circle",
        "badge_label": "MANUAL CHECK REQUIRED",
        "what_it_means": "No explicit copyright or Creative Commons statement was found automatically. Never assume missing information means it is free.",
        "action_steps": [
            "Check the video description and channel 'About' tab for copyright or permission notes.",
            "If no permission is stated anywhere, it is Standard YouTube license by default."
        ]
    }
}

# ------------------------------------------------------------------------------
# 3. COMPILED REGEX PATTERNS FOR CLAIM EXTRACTION
# ------------------------------------------------------------------------------

# Ordered so specific CC variants are checked before broader ones (e.g., NC-ND before NC, NC before BY)
LICENSE_REGEX_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # CC BY-NC-ND
    (
        CC_BY_NC_ND,
        re.compile(
            r"(?i)\b(?:cc[\s_-]?by[\s_-]?nc[\s_-]?nd|creative\s+commons\s+attribution[\s_-]?noncommercial[\s_-]?noderivatives?|creativecommons\.org/licenses/by-nc-nd)\b"
        ),
    ),
    # CC BY-NC-SA
    (
        CC_BY_NC_SA,
        re.compile(
            r"(?i)\b(?:cc[\s_-]?by[\s_-]?nc[\s_-]?sa|creative\s+commons\s+attribution[\s_-]?noncommercial[\s_-]?sharealike|creativecommons\.org/licenses/by-nc-sa)\b"
        ),
    ),
    # CC BY-NC
    (
        CC_BY_NC,
        re.compile(
            r"(?i)\b(?:cc[\s_-]?by[\s_-]?nc\b|creative\s+commons\s+attribution[\s_-]?noncommercial\b|creativecommons\.org/licenses/by-nc/)"
        ),
    ),
    # CC BY-ND
    (
        CC_BY_ND,
        re.compile(
            r"(?i)\b(?:cc[\s_-]?by[\s_-]?nd\b|creative\s+commons\s+attribution[\s_-]?noderivatives?\b|creativecommons\.org/licenses/by-nd/)"
        ),
    ),
    # CC BY-SA
    (
        CC_BY_SA,
        re.compile(
            r"(?i)\b(?:cc[\s_-]?by[\s_-]?sa\b|creative\s+commons\s+attribution[\s_-]?sharealike\b|creativecommons\.org/licenses/by-sa/)"
        ),
    ),
    # CC BY (General / Reuse Allowed)
    (
        CC_BY,
        re.compile(
            r"(?i)\b(?:cc[\s_-]?by\b(?!\s*[-–—/]\s*(?:nc|nd|sa|non|no|share))|"
            r"creative\s+commons\s+attribution(?!\s*[-–—/]\s*(?:nc|nd|sa|non|no|share))|"
            r"creative\s+commons\s+attribution\s+license\s*\(reuse\s+allowed\)|"
            r"creative\s+commons\s+reuse\s+allowed|"
            r"creative\s+commons\s+licensed|"
            r"creativecommons\.org/licenses/by/)"
        ),
    ),
    # Public Domain / CC0
    (
        PUBLIC_DOMAIN,
        re.compile(
            r"(?i)\b(?:public\s+domain(?:\s+mark|\s+dedication)?|cc0\b|cc-0\b|cc\s+zero|no\s+rights\s+reserved|creativecommons\.org/publicdomain/)\b"
        ),
    ),
    # YouTube Third-Party AI Training Permission
    (
        AI_TRAINING_PERMISSION,
        re.compile(
            r"(?i)\b(?:(?:youtube\s+)?third[\s_-]?party\s+ai\s+training\s+permission|"
            r"licensed\s+for\s+(?:third[\s_-]?party\s+)?ai\s+training|"
            r"ai\s+training\s+(?:permission\s+granted|authorized|allowed)|"
            r"third[\s_-]?party\s+ai\s+permission)\b"
        ),
    ),
    # Direct Written Permission
    (
        DIRECT_PERMISSION,
        re.compile(
            r"(?i)\b(?:direct\s+written\s+permission|"
            r"written\s+permission\s+(?:granted|obtained|from)|"
            r"permission\s+(?:granted|obtained)\s+from\s+(?:the\s+)?(?:author|creator|copyright|owner|publisher|speaker|artist)|"
            r"permission\s+(?:given|provided)\s+for\s+(?:commercial|tts|speech|audio)|"
            r"explicit\s+written\s+consent|"
            r"signed\s+(?:license|permission)\s+agreement)\b"
        ),
    ),
    # Standard YouTube License
    (
        STANDARD_YOUTUBE,
        re.compile(
            r"(?i)\b(?:standard\s+youtube\s+license|standard\s+youtube)\b"
        ),
    ),
]


def extract_license_claim_from_text(text: str) -> Optional[Tuple[str, str]]:
    """
    Scans a block of text for licensing claims.
    Returns: (category, snippet_evidence) or None if no license detected.
    """
    if not text or not isinstance(text, str):
        return None

    clean_text = text.strip()
    if not clean_text:
        return None

    for category, pattern in LICENSE_REGEX_PATTERNS:
        match = pattern.search(clean_text)
        if match:
            # Extract surrounding context for evidence (up to ~120 characters)
            start = max(0, match.start() - 25)
            end = min(len(clean_text), match.end() + 45)
            snippet = clean_text[start:end].replace("\n", " ").strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(clean_text):
                snippet = snippet + "..."
            return (category, snippet)

    return None


def extract_claims_from_sources(sources_dict: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """
    Evaluates individual sources (title, description, tags, comments, metadata, channel_info).
    Returns mapping: source_name -> (detected_category, evidence_snippet).
    """
    claims: Dict[str, Tuple[str, str]] = {}

    # 1. Metadata field (official platform license, e.g. status.license or watch-page tag)
    meta_lic = sources_dict.get("metadata_license") or ""
    if meta_lic:
        if isinstance(meta_lic, str):
            lower_m = meta_lic.lower().strip()
            if "creativecommon" in lower_m or "creative commons" in lower_m:
                claims["video_metadata"] = (CC_BY, f"Platform license metadata specifies: '{meta_lic}'")
            elif "youtube" in lower_m or "standard" in lower_m:
                claims["video_metadata"] = (STANDARD_YOUTUBE, f"Platform license metadata specifies: '{meta_lic}'")
            else:
                claim = extract_license_claim_from_text(meta_lic)
                if claim:
                    claims["video_metadata"] = (claim[0], f"Platform metadata claims: '{claim[1]}'")

    # 2. Description
    desc = sources_dict.get("description") or ""
    if desc:
        claim = extract_license_claim_from_text(desc)
        if claim:
            claims["description"] = (claim[0], f"Description: \"{claim[1]}\"")

    # 3. Title
    title = sources_dict.get("title") or ""
    if title:
        claim = extract_license_claim_from_text(title)
        if claim:
            claims["title"] = (claim[0], f"Title: \"{claim[1]}\"")

    # 4. Tags / Keywords
    tags = sources_dict.get("tags") or []
    if isinstance(tags, list):
        tags_text = " ".join(tags)
    else:
        tags_text = str(tags)
    if tags_text:
        claim = extract_license_claim_from_text(tags_text)
        if claim:
            claims["tags"] = (claim[0], f"Video tags: \"{claim[1]}\"")

    # 5. Comments (top comments, pinned comments)
    comments = sources_dict.get("comments") or []
    if isinstance(comments, str):
        comments = [comments]
    for c in comments:
        if c:
            claim = extract_license_claim_from_text(c)
            if claim:
                claims["comments"] = (claim[0], f"Comment: \"{claim[1]}\"")
                break

    # 6. Channel Information
    chan = sources_dict.get("channel_info") or ""
    if chan:
        claim = extract_license_claim_from_text(chan)
        if claim:
            claims["channel_info"] = (claim[0], f"Channel info: \"{claim[1]}\"")

    # 7. Explicit Permission Links
    links = sources_dict.get("links") or []
    if isinstance(links, str):
        links = [links]
    for link in links:
        if link:
            claim = extract_license_claim_from_text(link)
            if claim:
                claims["permission_links"] = (claim[0], f"Link: \"{claim[1]}\"")
                break

    return claims


def analyze_licensing_evidence(sources_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aggregates claims from all sources, detects conflicts, applies screening guidance,
    and returns a full license detection result.

    Requirement 5: Distinguishes detected claims from verified permissions.
    Requirement 6: Flags conflicts for manual review instead of silently selecting.
    Requirement 7: Applies screening guidance based on category.
    """
    claims = extract_claims_from_sources(sources_dict)

    # Distinct categories detected across sources
    unique_categories = list({cat for cat, _ in claims.values()})

    # Sources checked list
    sources_checked = [
        k for k in ["video_metadata", "title", "description", "tags", "comments", "channel_info", "permission_links"]
        if k in sources_dict and sources_dict[k]
    ]

    # CASE A: CONFLICT DETECTED
    if len(unique_categories) > 1:
        evidence_parts = [f"[{src}] {snip} -> {cat}" for src, (cat, snip) in claims.items()]
        evidence_str = " | ".join(evidence_parts)

        conflict_details = {src: cat for src, (cat, _) in claims.items()}
        conflict_msg = (
            f"Conflicting licensing claims detected across sources: "
            + ", ".join(f"{src} ({cat})" for src, cat in conflict_details.items())
            + ". Flagged for manual review."
        )

        return {
            "detected_license": "Conflict Detected",
            "effective_license": UNKNOWN,  # Do not silently pick one
            "conflict_detected": True,
            "conflicts": conflict_details,
            "detection_source": "conflict",
            "evidence": evidence_str,
            "verification_status": "FLAGGED_CONFLICT",
            "reviewer_decision": "MANUAL_REVIEW_REQUIRED",
            "screening_guidance": conflict_msg,
            "commercial_acceptable": "MANUAL_REVIEW_REQUIRED",
            "requires_verification": True,
            "is_claim_only": True,
            "sources_checked": sources_checked,
            "claims": {k: {"category": v[0], "snippet": v[1]} for k, v in claims.items()},
            "policy_decision": "REVIEW",
            "policy_reason": "Conflicting licensing claims across sources (Manual review required)",
            "plain_title": "Conflicting License Information Found",
            "badge_level": "CAUTION",
            "badge_icon": "fa-triangle-exclamation",
            "badge_label": "CONFLICT DETECTED — REVIEW NEEDED",
            "what_it_means": "Different parts of this video claim different copyright rules (for example, metadata says Standard License but the description says Creative Commons). You cannot proceed without manual checking.",
            "action_steps": [
                "Inspect the video description and metadata directly to see which claim is authentic.",
                "Consult your team lead or reviewer before using this audio."
            ]
        }

    # CASE B: SINGLE CLEAR CLAIM DETECTED
    elif len(unique_categories) == 1:
        cat = unique_categories[0]
        policy = SCREENING_POLICY.get(cat, SCREENING_POLICY[UNKNOWN])

        primary_source = list(claims.keys())[0]
        evidence_parts = [f"[{src}] {snip}" for src, (_, snip) in claims.items()]
        evidence_str = " | ".join(evidence_parts)

        # Build evidence label distinguishing claim from verified permission
        claim_context = "Claim detected"
        if primary_source == "video_metadata":
            claim_context = "Platform metadata statement"
        elif primary_source in ("description", "comments", "title"):
            claim_context = f"Uploader claim in {primary_source} (not verified permission)"

        return {
            "detected_license": cat,
            "effective_license": cat,
            "conflict_detected": False,
            "conflicts": {},
            "detection_source": primary_source,
            "evidence": f"({claim_context}) {evidence_str}",
            "verification_status": policy["default_verification_status"],
            "reviewer_decision": "PENDING",
            "screening_guidance": policy["guidance"],
            "commercial_acceptable": policy["commercial_acceptable"],
            "requires_verification": policy["requires_verification"],
            "is_claim_only": True,
            "sources_checked": sources_checked,
            "claims": {k: {"category": v[0], "snippet": v[1]} for k, v in claims.items()},
            "policy_decision": policy.get("policy_decision", "ACCEPT"),
            "policy_reason": policy.get("policy_reason", ""),
            "plain_title": policy.get("plain_title", cat),
            "badge_level": policy.get("badge_level", "SAFE"),
            "badge_icon": policy.get("badge_icon", "fa-circle-info"),
            "badge_label": policy.get("badge_label", cat),
            "what_it_means": policy.get("what_it_means", ""),
            "action_steps": policy.get("action_steps", [])
        }

    # CASE C: NO LICENSING FOUND -> Default to Standard YouTube License (platform default)
    else:
        policy = SCREENING_POLICY[STANDARD_YOUTUBE]
        return {
            "detected_license": STANDARD_YOUTUBE,
            "effective_license": STANDARD_YOUTUBE,
            "conflict_detected": False,
            "conflicts": {},
            "detection_source": "default",
            "evidence": "No explicit licensing statements detected; defaulted to Standard YouTube License (platform default).",
            "verification_status": policy["default_verification_status"],
            "reviewer_decision": "PENDING",
            "screening_guidance": policy["guidance"],
            "commercial_acceptable": policy["commercial_acceptable"],
            "requires_verification": True,
            "is_claim_only": False,
            "sources_checked": sources_checked,
            "claims": {},
            "policy_decision": policy.get("policy_decision", "REJECT"),
            "policy_reason": policy.get("policy_reason", "Standard YouTube license with no separate permission (No dataset extraction)"),
            "plain_title": policy.get("plain_title", STANDARD_YOUTUBE),
            "badge_level": policy.get("badge_level", "DANGER"),
            "badge_icon": policy.get("badge_icon", "fa-copyright"),
            "badge_label": policy.get("badge_label", STANDARD_YOUTUBE),
            "what_it_means": policy.get("what_it_means", ""),
            "action_steps": policy.get("action_steps", [])
        }


# ------------------------------------------------------------------------------
# 4. YOUTUBE INGESTION & ROBUST SOURCE SCRAPING
# ------------------------------------------------------------------------------

def extract_youtube_video_id(url: str) -> Optional[str]:
    """Extracts 11-char YouTube video ID from various YouTube URL formats."""
    if not url:
        return None
    url = url.strip()
    match = re.search(r'(?:v=|\/embed\/|\/11\/|\/v\/|youtu\.be\/|\/shorts\/)([a-zA-Z0-9_-]{11})', url)
    if match:
        return match.group(1)
    # Check query param 'v'
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return qs["v"][0]
    except Exception:
        pass
    return None


def fetch_and_detect_youtube_licensing(
    url: str,
    platform_item_id: Optional[str] = None,
    html_content: Optional[str] = None,
    candidate_description: Optional[str] = None,
    candidate_title: Optional[str] = None,
    yt_api_key: Optional[str] = None,
    timeout: float = 4.0
) -> Dict[str, Any]:
    """
    Comprehensive YouTube licensing detection:
    1. Checks Official YouTube Data API v3 if API key is provided (videos, commentThreads, channels).
    2. Gracefully falls back to watch page HTML extraction if no key, quota exceeded, or network fails.
    3. Handles disabled comments, missing metadata, and API limits without failing.
    4. Evaluates title, description, tags, comments, channel info, and platform license.
    """
    video_id = platform_item_id or extract_youtube_video_id(url)
    api_key = yt_api_key or os.getenv("YOUTUBE_API_KEY", "").strip()

    sources_dict: Dict[str, Any] = {
        "metadata_license": "",
        "title": candidate_title or "",
        "description": candidate_description or "",
        "tags": [],
        "comments": [],
        "channel_info": "",
        "links": []
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }

    api_succeeded = False

    # --------------------------------------------------------------------------
    # Step A: Official YouTube Data API v3 (if key configured and video_id known)
    # --------------------------------------------------------------------------
    if api_key and video_id:
        try:
            # 1. Video snippet, contentDetails, status
            api_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails,status&id={video_id}&key={api_key}"
            res = requests.get(api_url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", [])
                if items:
                    api_succeeded = True
                    snippet = items[0].get("snippet", {})
                    status_info = items[0].get("status", {})

                    # Official YouTube status license: 'creativeCommon' or 'youtube'
                    sources_dict["metadata_license"] = status_info.get("license", "")
                    sources_dict["title"] = snippet.get("title", "")
                    sources_dict["description"] = snippet.get("description", "")
                    sources_dict["tags"] = snippet.get("tags", [])
                    channel_id = snippet.get("channelId")
                    sources_dict["channel_info"] = snippet.get("channelTitle", "")

                    # 2. Try fetching top comments (gracefully handle disabled comments or quota)
                    try:
                        comment_url = (
                            f"https://www.googleapis.com/youtube/v3/commentThreads?"
                            f"part=snippet&videoId={video_id}&maxResults=10&order=relevance&key={api_key}"
                        )
                        c_res = requests.get(comment_url, headers=headers, timeout=timeout)
                        if c_res.status_code == 200:
                            c_data = c_res.json()
                            for c_item in c_data.get("items", []):
                                top = c_item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                                text = top.get("textDisplay") or top.get("textOriginal") or ""
                                if text:
                                    sources_dict["comments"].append(text)
                    except Exception as c_err:
                        logger.debug(f"Comments unavailable via API for {video_id}: {c_err}")

                    # 3. Channel Info
                    if channel_id:
                        try:
                            chan_url = f"https://www.googleapis.com/youtube/v3/channels?part=snippet&id={channel_id}&key={api_key}"
                            chan_res = requests.get(chan_url, headers=headers, timeout=timeout)
                            if chan_res.status_code == 200:
                                chan_items = chan_res.json().get("items", [])
                                if chan_items:
                                    chan_snip = chan_items[0].get("snippet", {})
                                    chan_desc = chan_snip.get("description", "")
                                    sources_dict["channel_info"] += f" | {chan_desc}"
                        except Exception as ch_err:
                            logger.debug(f"Channel info unavailable via API for {channel_id}: {ch_err}")
        except Exception as e:
            logger.warning(f"YouTube Data API query failed or quota hit for {video_id}: {e}")

    # --------------------------------------------------------------------------
    # Step B: Watch Page HTML Scraping Fallback (No key, quota hit, or complementary)
    # --------------------------------------------------------------------------
    raw_html = html_content or ""
    if not raw_html and video_id and not api_succeeded:
        try:
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            res = requests.get(watch_url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                raw_html = res.text
        except Exception as e:
            logger.debug(f"Watch page fetch failed for {video_id}: {e}")

    if raw_html:
        # Extract title if missing
        if not sources_dict["title"]:
            t_match = re.search(r"<title>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
            if t_match:
                sources_dict["title"] = t_match.group(1).replace("- YouTube", "").strip()

        # Extract description meta
        if not sources_dict["description"]:
            d_match = re.search(r'<meta\s+name="description"\s+content="(.*?)"', raw_html, re.IGNORECASE | re.DOTALL)
            if d_match:
                sources_dict["description"] = d_match.group(1).strip()
            # Try shortDescription in player response
            sd_match = re.search(r'"shortDescription":"(.*?)"', raw_html)
            if sd_match:
                short_desc = sd_match.group(1).encode("utf-8").decode("unicode_escape", errors="ignore")
                if len(short_desc) > len(sources_dict["description"]):
                    sources_dict["description"] = short_desc

        # Extract tags meta
        if not sources_dict["tags"]:
            k_match = re.search(r'<meta\s+name="keywords"\s+content="(.*?)"', raw_html, re.IGNORECASE)
            if k_match:
                sources_dict["tags"] = [t.strip() for t in k_match.group(1).split(",") if t.strip()]

        # Extract player response license
        if not sources_dict["metadata_license"]:
            lic_match = re.search(r'"license":"([^"]+)"', raw_html)
            if lic_match:
                sources_dict["metadata_license"] = lic_match.group(1)

            # Check metadata rows for License title
            if not sources_dict["metadata_license"]:
                if "Creative Commons Attribution license" in raw_html:
                    sources_dict["metadata_license"] = "Creative Commons Attribution license (reuse allowed)"
                elif "Standard YouTube License" in raw_html:
                    sources_dict["metadata_license"] = "Standard YouTube License"

        # Check for pinned comment text in raw HTML if accessible
        if not sources_dict["comments"]:
            comment_matches = re.findall(r'"content":{"simpleText":"([^"]{15,300})"}', raw_html)
            if comment_matches:
                sources_dict["comments"].extend(comment_matches[:5])

    # Extract any explicit links in description
    if sources_dict["description"]:
        found_links = re.findall(r'https?://[^\s<>"]+', sources_dict["description"])
        sources_dict["links"] = found_links

    # Run multi-source analysis
    result = analyze_licensing_evidence(sources_dict)
    result["video_id"] = video_id
    result["url"] = url
    result["api_key_used"] = api_succeeded

    return result
