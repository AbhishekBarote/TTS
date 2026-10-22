"""
Duplicate Prevention, URL Normalization, and Fast Pre-Filter Engine.
Adheres strictly to Sections 5 & 6 of final_implementation_architecture.md:
1. Exact platform item ID extraction (YouTube, Internet Archive, Vimeo, SoundCloud, etc.)
2. Canonical URL normalization (removing tracking params, standardizing scheme & host)
3. Fast Pre-Filter for routing candidate records:
   - Exact identity check (Video ID / canonical URL) -> DUPLICATE (no LLM call)
   - Inexpensive similarity check (title, description, channel, duration delta)
   - Clearly unique -> Skip LLM
   - Probable duplicate -> Retrieve top-K matches for conditional LLM evaluation
   - Rule: Same speaker alone or same topic alone does NOT prove duplication
"""

import re
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode
from typing import Optional, Tuple, Dict, Any, List
from rapidfuzz import fuzz

TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'si', 'feature', 'fbclid', 'gclid', 'igshid', 'mc_cid', 'mc_eid',
    '_ga', '_gl', 'ref', 'source', 'spm', 'from'
}

def extract_platform_and_item_id(url: str) -> Tuple[str, Optional[str]]:
    """
    Extracts platform name and stable item ID if supported.
    Returns (platform_name, item_id).
    """
    if not url:
        return ("Unknown", None)
    
    url_trimmed = url.strip()
    match = re.search(r'(https?://[^\s]+)', url_trimmed)
    if match:
        url_trimmed = match.group(1)
    parsed = urlparse(url_trimmed)
    hostname = (parsed.hostname or '').lower()
    path = parsed.path

    # YouTube
    if any(h in hostname for h in ['youtube.com', 'youtu.be']):
        if 'youtu.be' in hostname:
            # Format: https://youtu.be/VIDEO_ID
            parts = [p for p in path.split('/') if p]
            if parts:
                return ("YouTube", parts[0][:11])
        else:
            # Query param ?v=VIDEO_ID
            qs = parse_qs(parsed.query)
            if 'v' in qs and qs['v']:
                return ("YouTube", qs['v'][0][:11])
            # Shorts: /shorts/VIDEO_ID
            if '/shorts/' in path:
                parts = path.split('/shorts/')
                if len(parts) > 1:
                    vid = parts[1].split('/')[0].split('?')[0]
                    return ("YouTube", vid[:11])
            # Embed: /embed/VIDEO_ID
            if '/embed/' in path:
                parts = path.split('/embed/')
                if len(parts) > 1:
                    vid = parts[1].split('/')[0].split('?')[0]
                    return ("YouTube", vid[:11])
            # v/: /v/VIDEO_ID
            if '/v/' in path:
                parts = path.split('/v/')
                if len(parts) > 1:
                    vid = parts[1].split('/')[0].split('?')[0]
                    return ("YouTube", vid[:11])
        return ("YouTube", None)

    # Internet Archive
    if 'archive.org' in hostname:
        # Format: /details/ITEM_ID or /embed/ITEM_ID
        match = re.search(r'/(?:details|embed|download)/([^/?#]+)', path)
        if match:
            return ("Internet Archive", match.group(1))
        return ("Internet Archive", None)

    # Vimeo
    if 'vimeo.com' in hostname:
        match = re.search(r'/(?:video/)?([0-9]+)', path)
        if match:
            return ("Vimeo", match.group(1))
        return ("Vimeo", None)

    # SoundCloud
    if 'soundcloud.com' in hostname:
        parts = [p for p in path.split('/') if p]
        if len(parts) >= 2:
            return ("SoundCloud", f"{parts[0]}/{parts[1]}")
        return ("SoundCloud", None)

    # Generic or others
    platform = "Web"
    if hostname:
        clean_host = hostname.replace('www.', '')
        platform = clean_host.split('.')[0].capitalize()
    return (platform, None)


def canonicalize_url(url: str) -> str:
    """
    Normalizes a URL by:
    - Standardizing scheme to https (or http if IP)
    - Lowercasing host
    - Removing tracking query parameters (utm_*, si, fbclid, etc.)
    - Converting known platforms to standardized canonical format
    - Standardizing paths and trailing slashes
    """
    if not url:
        return ""
    
    url = url.strip()
    match = re.search(r'(https?://[^\s]+)', url)
    if match:
        url = match.group(1)
    if not re.match(r'^[a-zA-Z]+://', url):
        url = 'https://' + url

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in ('http', 'https'):
        scheme = 'https'
    hostname = (parsed.hostname or '').lower()
    if hostname.startswith('www.'):
        hostname = hostname[4:]
    
    platform, item_id = extract_platform_and_item_id(url)

    # For YouTube, canonicalize directly to standard watch URL if item_id exists
    if platform == "YouTube" and item_id:
        return f"https://youtube.com/watch?v={item_id}"
    
    # For Internet Archive, canonicalize to details URL
    if platform == "Internet Archive" and item_id:
        return f"https://archive.org/details/{item_id}"
    
    # For Vimeo
    if platform == "Vimeo" and item_id:
        return f"https://vimeo.com/{item_id}"

    # For generic URLs, strip tracking query params
    query_dict = parse_qs(parsed.query, keep_blank_values=False)
    filtered_params = []
    for k, v in query_dict.items():
        k_lower = k.lower()
        if k_lower in TRACKING_PARAMS:
            continue
        for val in v:
            filtered_params.append((k, val))
    
    # Sort query params for deterministic canonical URL
    filtered_params.sort(key=lambda x: x[0])
    new_query = urlencode(filtered_params)

    # Strip trailing slash from path unless it is root '/'
    path = parsed.path
    if path and path != '/' and path.endswith('/'):
        path = path.rstrip('/')

    return urlunparse((
        scheme,
        hostname,
        path,
        '', # params
        new_query,
        ''  # fragment stripped for canonical URL
    ))


def fast_prefilter(
    candidate: Dict[str, Any],
    existing_records: List[Dict[str, Any]],
    top_k: int = 3
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Stage 2: Fast Pre-filter (Section 6 of final_implementation_architecture.md).
    Inexpensive checks comparing candidate against existing records:
    - Exact video ID / canonical URL check (Stage 1) -> EXACT_DUPLICATE
    - Normalized title similarity
    - Description similarity
    - Channel / source clues
    - Duration match / delta
    
    Returns:
    (routing_status, matching_records)
    where routing_status is:
    - "EXACT_DUPLICATE": exact video ID / canonical URL match found -> no LLM call
    - "PROBABLE_DUPLICATE": 1 or more probable candidate matches found -> route to LLM
    - "CLEARLY_UNIQUE": no plausible matches found -> no LLM call
    """
    cand_url = canonicalize_url(candidate.get("audio_link") or candidate.get("url") or "")
    cand_platform, cand_item_id = extract_platform_and_item_id(cand_url)
    cand_desc = (candidate.get("description") or "").strip().lower()
    cand_title = (candidate.get("title") or "").strip().lower()
    if not cand_title and cand_desc:
        cand_title = cand_desc.split("\n")[0]
    
    cand_channel = (candidate.get("channel") or candidate.get("channel_name") or "").strip().lower()
    cand_dur_sec = candidate.get("duration_seconds") or 0

    # 1. Exact Identity Check (Stage 1)
    for rec in existing_records:
        rec_url = canonicalize_url(rec.get("audio_link") or rec.get("canonical_url") or "")
        rec_item_id = rec.get("platform_item_id")
        rec_platform = rec.get("source") or ""

        # Exact canonical URL match
        if cand_url and rec_url and cand_url == rec_url:
            rec_copy = dict(rec)
            rec_copy["match_reason"] = "Exact Canonical URL match"
            return ("EXACT_DUPLICATE", [rec_copy])

        # Exact Platform Video ID match
        if cand_item_id and rec_item_id and cand_item_id == rec_item_id:
            if cand_platform in ("YouTube", "Internet Archive", "Vimeo") and rec_platform in ("YouTube", "Internet Archive", "Vimeo"):
                rec_copy = dict(rec)
                rec_copy["match_reason"] = f"Exact {cand_platform} Item ID match: '{cand_item_id}'"
                return ("EXACT_DUPLICATE", [rec_copy])

    # 2. Inexpensive Pre-Filter Scoring
    scored_candidates = []

    for rec in existing_records:
        rec_desc = (rec.get("description") or "").strip().lower()
        rec_title = (rec.get("title") or "").strip().lower()
        if not rec_title and rec_desc:
            rec_title = rec_desc.split("\n")[0]
        rec_channel = (rec.get("channel_name") or rec.get("channel") or "").strip().lower()
        rec_dur_sec = rec.get("duration_seconds") or 0

        title_sim = fuzz.token_sort_ratio(cand_title, rec_title) if (cand_title and rec_title) else 0
        desc_sim = fuzz.token_sort_ratio(cand_desc, rec_desc) if (cand_desc and rec_desc) else 0
        text_sim = max(title_sim, desc_sim)

        same_channel = bool(cand_channel and rec_channel and cand_channel == rec_channel)
        dur_delta = abs(cand_dur_sec - rec_dur_sec) if (cand_dur_sec and rec_dur_sec) else 999
        same_duration = (dur_delta <= 3 and cand_dur_sec > 0)

        # Inexpensive heuristic score:
        # Base weight: text similarity
        score = text_sim
        if same_channel:
            score += 15
        if same_duration:
            score += 20
        
        # Plausible match threshold:
        # Title/desc similarity >= 65%, or (similarity >= 50% with same channel & duration)
        is_plausible = (
            text_sim >= 65 or
            (text_sim >= 50 and (same_channel or same_duration)) or
            (title_sim >= 55 and same_duration)
        )

        if is_plausible:
            match_data = dict(rec)
            match_data["prefilter_score"] = score
            match_data["title_similarity"] = title_sim
            match_data["desc_similarity"] = desc_sim
            match_data["same_channel"] = same_channel
            match_data["same_duration"] = same_duration
            scored_candidates.append(match_data)

    if not scored_candidates:
        return ("CLEARLY_UNIQUE", [])

    # Sort descending by pre-filter score and take top_k
    scored_candidates.sort(key=lambda x: x["prefilter_score"], reverse=True)
    return ("PROBABLE_DUPLICATE", scored_candidates[:top_k])


def check_similarity(
    candidate: Dict[str, Any],
    existing_records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Legacy helper maintained for backward compatibility.
    Compares candidate against existing records for:
    - Matching Speaker ID
    - Similar Description/Title
    - Same Source platform and identical/near duration
    """
    warnings = []
    cand_speaker = (candidate.get("speakers_id") or "").strip().upper()
    cand_desc = (candidate.get("description") or "").strip().lower()
    cand_source = (candidate.get("source") or "").strip().lower()
    cand_dur = (candidate.get("before_processing_duration") or "").strip()

    for rec in existing_records:
        rec_id = rec.get("id")
        rec_speaker = (rec.get("speakers_id") or "").strip().upper()
        rec_desc = (rec.get("description") or "").strip().lower()
        rec_source = (rec.get("source") or "").strip().lower()
        rec_dur = (rec.get("before_processing_duration") or "").strip()
        rec_url = rec.get("audio_link") or ""
        rec_status = rec.get("status") or "UNKNOWN"

        similarity_reasons = []

        if cand_speaker and rec_speaker and cand_speaker == rec_speaker:
            similarity_reasons.append(f"Matching Speaker ID: '{cand_speaker}'")

        if cand_desc and rec_desc:
            ratio = fuzz.token_sort_ratio(cand_desc, rec_desc)
            if ratio >= 75:
                similarity_reasons.append(f"High description similarity ({ratio}% match)")

        if cand_source and rec_source and cand_dur and rec_dur:
            if cand_source == rec_source and cand_dur == rec_dur:
                similarity_reasons.append(f"Same source '{rec.get('source')}' and duration '{cand_dur}'")

        if similarity_reasons:
            warnings.append({
                "record_id": rec_id,
                "speakers_id": rec.get("speakers_id"),
                "audio_link": rec_url,
                "description": rec.get("description"),
                "status": rec_status,
                "researched_by": rec.get("researched_by"),
                "reasons": similarity_reasons
            })

    return warnings
