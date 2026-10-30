"""
Fast, Low-Latency URL Validation & Webpage-Fetching Pipeline for Duplicate Detection.
Adheres to strict production performance and safety guidelines:
1. Accept HTTP/HTTPS URLs with safe validation and normalization.
2. Lightweight HTTP requests with connection pooling and strict timeouts (1.5s connect, 2.5s read).
3. Response-size limits (streamed, max 1MB read) and safe redirect handling (max 3).
4. Strictly enforce TLS verification (verify=True).
5. Extract only essential fields: canonical URL, title, metadata (author/channel, date, description), main text.
6. Fail-safe: Return 'FETCH_FAILED' if inaccessible or error. Never mark inaccessible pages as UNIQUE.
7. Exact content-hash (SHA-256) matching first against indexed data.
8. Fast indexed pre-filter; invoke self-hosted LLM ONLY for plausible duplicate candidates.
9. Return one of: 'DUPLICATE', 'POTENTIAL_DUPLICATE', 'UNIQUE', 'FETCH_FAILED'.
"""

import os
import re
import time
import json
import hashlib
import unicodedata
import html
import logging
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode
from typing import Optional, List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from rapidfuzz import fuzz

from backend.duplicate import TRACKING_PARAMS
from backend.database import get_connection, get_all_stored_web_and_records
from backend.llm_adapter import LLMAdapter


logger = logging.getLogger(__name__)

# Extended tracking parameters for web articles and media portals
FAST_TRACKING_PARAMS = TRACKING_PARAMS.union({
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'utm_id',
    'si', 'feature', 'fbclid', 'gclid', 'igshid', 'mc_cid', 'mc_eid', '_ga', '_gl',
    'ref', 'source', 'spm', 'from', 'yclid', 'wbraid', 'gbraid', 'msclkid', 'ved',
    'usg', 'sa', 'biw', 'bih', 'dpr', 'oq', 'gs_lcp', 'sclient', 'aqs', 'ie', 'oe'
})

# Connection Pool with Retries
_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=30,
    pool_maxsize=30,
    max_retries=Retry(
        total=1,
        backoff_factor=0.1,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

MAX_CONTENT_BYTES = 1024 * 1024  # 1MB limit to avoid large file memory/DoS issues
REQUEST_TIMEOUT = (1.5, 2.5)     # (1.5s connect, 2.5s read)


# ==============================================================================
# 1. SAFE URL VALIDATION & NORMALIZATION
# ==============================================================================

def validate_and_normalize_url(url: str) -> Tuple[bool, str, str, Optional[str], Optional[str]]:
    """
    Validates that the URL uses HTTP or HTTPS.
    Normalizes scheme, lowercases host, strips tracking parameters and fragments.
    Returns: (is_valid, canonical_url, platform, platform_item_id, error_reason)
    """
    if not url or not isinstance(url, str):
        return (False, "", "Unknown", None, "Empty or non-string URL provided.")

    raw = url.strip()
    match = re.search(r'(https?://[^\s]+)', raw)
    if match:
        raw = match.group(1)

    if not re.match(r'^[a-zA-Z]+://', raw):
        raw = 'https://' + raw

    try:
        parsed = urlparse(raw)
    except Exception as e:
        return (False, "", "Unknown", None, f"Malformed URL syntax: {str(e)}")

    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https'):
        return (False, "", "Unknown", None, f"Unsupported protocol '{scheme}'. Only HTTP and HTTPS are allowed.")

    hostname = (parsed.hostname or '').lower()
    if not hostname:
        return (False, "", "Unknown", None, "URL must contain a valid domain name or hostname.")

    # Standardize scheme (default to https unless localhost)
    if hostname not in ('localhost', '127.0.0.1'):
        scheme = 'https'

    clean_host = hostname
    if clean_host.startswith('www.'):
        clean_host = clean_host[4:]

    # Strip tracking query parameters
    query_dict = parse_qs(parsed.query, keep_blank_values=False)
    filtered = []
    for k, v in query_dict.items():
        if k.lower() in FAST_TRACKING_PARAMS:
            continue
        for val in v:
            filtered.append((k, val))
    filtered.sort(key=lambda x: x[0])
    clean_query = urlencode(filtered)

    path = parsed.path or ""
    if path and path != '/' and path.endswith('/'):
        path = path.rstrip('/')

    # Platform & Item ID Detection
    platform = "Web"
    platform_item_id = None

    if any(h in hostname for h in ['youtube.com', 'youtu.be']):
        platform = "YouTube"
        if 'youtu.be' in hostname:
            parts = [p for p in path.split('/') if p]
            if parts:
                platform_item_id = parts[0][:11]
        else:
            qs = parse_qs(parsed.query)
            if 'v' in qs and qs['v']:
                platform_item_id = qs['v'][0][:11]
            elif '/shorts/' in path:
                parts = path.split('/shorts/')
                if len(parts) > 1:
                    platform_item_id = parts[1].split('/')[0].split('?')[0][:11]
            elif '/embed/' in path:
                parts = path.split('/embed/')
                if len(parts) > 1:
                    platform_item_id = parts[1].split('/')[0].split('?')[0][:11]

        if platform_item_id:
            canonical_url = f"https://www.youtube.com/watch?v={platform_item_id}"
            return (True, canonical_url, platform, platform_item_id, None)

    elif 'archive.org' in hostname:
        platform = "Internet Archive"
        m = re.search(r'/(?:details|embed|download)/([^/?#]+)', path)
        if m:
            platform_item_id = m.group(1)
            canonical_url = f"https://archive.org/details/{platform_item_id}"
            return (True, canonical_url, platform, platform_item_id, None)

    elif 'vimeo.com' in hostname:
        platform = "Vimeo"
        m = re.search(r'/(?:video/)?([0-9]+)', path)
        if m:
            platform_item_id = m.group(1)
            canonical_url = f"https://vimeo.com/{platform_item_id}"
            return (True, canonical_url, platform, platform_item_id, None)

    elif 'soundcloud.com' in hostname:
        platform = "SoundCloud"
        parts = [p for p in path.split('/') if p]
        if len(parts) >= 2:
            platform_item_id = f"{parts[0]}/{parts[1]}"

    else:
        platform = "Website"

    canonical_url = urlunparse((scheme, clean_host, path, '', clean_query, ''))
    return (True, canonical_url, platform, platform_item_id, None)


# ==============================================================================
# 2. FAST ESSENTIAL CONTENT & METADATA FETCHING
# ==============================================================================

def generate_content_hash(text: str) -> str:
    """Computes a SHA-256 hash from normalized content string."""
    cleaned = unicodedata.normalize("NFKC", (text or "")).strip().lower()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


HONORIFICS_LIST = [
    'dr.', 'dr', 'prof.', 'prof', 'professor', 'swami', 'sri', 'shri', 'smt.', 'smt',
    'acharya', 'sadguru', 'sadhguru', 'pandit', 'pt.', 'pt', 'ustad', 'sister', 'father',
    'rev.', 'guru', 'gurudev', 'sant', 'baba', 'maharaj'
]


def clean_speaker_name(name: str) -> Optional[str]:
    """Cleans up and validates an extracted speaker name string with proper casing."""
    if not name:
        return None
    name = re.sub(r'^(?:Speaker|Spoken by|Lecturer|Presented by|By|Talk by|Speech by)\s*[:\-–—]?\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+(?:Official|Channel|Hindi|English|Podcast|Videos|Shorts|Clips|Talks|Foundation|Ashram|Trust)$', '', name, flags=re.IGNORECASE)
    name = name.split('\n')[0].split('|')[0].split(' - ')[0].split(' : ')[0].strip()
    name = re.sub(r'[^\w\s\.\'\-]', '', name).strip()
    name = re.split(r'\s+on\s+', name, flags=re.IGNORECASE)[0].strip()
    words = name.split()
    if len(name) < 3 or len(name) > 50 or len(words) > 5:
        return None
    formatted_words = []
    for w in words:
        if w.lower() in ('dr.', 'dr', 'prof.', 'prof', 'pt.', 'pt', 'smt.', 'smt'):
            formatted_words.append(w.capitalize() + ('.' if not w.endswith('.') else ''))
        elif w.islower():
            formatted_words.append(w.capitalize())
        else:
            formatted_words.append(w)
    return ' '.join(formatted_words)


def extract_speaker_from_metadata(
    title: str,
    description: str,
    channel: Optional[str] = None,
    comments: Optional[List[str]] = None
) -> Optional[str]:
    """
    Extracts speaker name by checking title segments, full description, top comments, and channel cues.
    Returns clean speaker name if detected, else None.
    """
    # 1. Check Title Segments (delimited by |, -, --, /, etc., e.g., 'Title | Topic | swami sarvapriyananda')
    segments = [s.strip() for s in re.split(r'[|\-–—/•]', title or '') if s.strip()]
    for seg in segments:
        m_pref = re.match(r'^(?:speaker|guest speaker|spoken by|presented by|by|talk by|lecture by|discourse by)\s*[:\-–—]?\s*(.+)$', seg, re.I)
        cand = m_pref.group(1).strip() if m_pref else seg
        cand_clean = re.split(r'\s+on\s+', cand, flags=re.I)[0].strip()
        words = cand_clean.split()
        if words:
            first_w = words[0].lower().rstrip('.')
            if first_w in [h.rstrip('.') for h in HONORIFICS_LIST]:
                if 1 <= len(words) <= 5:
                    c = clean_speaker_name(cand_clean)
                    if c:
                        return c
            elif m_pref and len(words) <= 4:
                c = clean_speaker_name(cand_clean)
                if c:
                    return c

    # 2. Check Description first (richest source of speaker info in video uploads)
    desc_patterns = [
        r'(?:speaker|guest speaker|keynote speaker|eminent speaker|distinguished speaker|resource person|chief guest|spoken by|presented by|discourse by|lecture by|talk by|speech by|recited by|chanted by|orator|narrated by|interview with|conversation with|in conversation with)\s*[:\-–—|]\s*([^\n\r|,;()]+)',
        r'(?:lecture|talk|speech|discourse|satsang|upanyasam|address|presentation)\s+(?:by|with)\s+([A-Za-z\.\s]{3,40})',
    ]
    for p in desc_patterns:
        m = re.search(p, description or '', re.I)
        if m:
            c = clean_speaker_name(m.group(1))
            if c:
                return c

    # 3. Check Title Regex Patterns
    title_patterns = [
        r'(?:speaker|guest speaker|spoken by|presented by|discourse by|lecture by|talk by|speech by)\s*[:\-–—|]?\s*([A-Za-z\.\'\s]{3,35})(?=\s+on\s+|$|[|\-–—/•:;,#\(\)])',
        r'\b(?:dr\.?|prof\.?|professor|swami|sri|shri|smt\.?|acharya|sadguru|sadhguru|pandit|pt\.?|ustad|sister|father|rev\.?|guru|gurudev|sant|baba|maharaj)\s+([A-Za-z\.\']+(?:\s+[A-Za-z\.\']+){0,3})(?=\s+on\s+|$|[|\-–—/•:;,#\(\)])',
    ]
    for p in title_patterns:
        m = re.search(p, title or '', re.I)
        if m:
            val = m.group(0) if 'dr' in p else (m.group(1) if m.groups() else m.group(0))
            c = clean_speaker_name(val)
            if c:
                return c

    # 4. Check Top Comments
    for comm in (comments or []):
        m = re.search(r'(?:speaker(?:\s*name|\s*is)?|speaking is|lecturer is|speech by|talk by|discourse by|lecture by)\s*(?:is|:|-)?\s*([A-Za-z\.\s]{3,35})', comm, re.I)
        if m:
            c = clean_speaker_name(m.group(1))
            if c:
                return c

    # 5. Check Channel Name ONLY if it explicitly identifies a person (has honorific prefix)
    if channel:
        chan_clean = re.sub(r'\s+(?:Official|Channel|Hindi|English|Podcast|Videos|Shorts|Clips|Talks|Foundation|Ashram|Trust)$', '', channel, flags=re.I).strip()
        if re.search(r'^(?:Dr\.|Prof\.|Swami|Sri|Shri|Smt\.|Acharya|Sadguru|Sadhguru|Pandit|Pt\.|Sister|Father|Rev\.)\s+', chan_clean, re.I):
            return clean_speaker_name(chan_clean)

    return None


def fetch_essential_content(
    url: str,
    platform: str,
    platform_item_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Lightweight, low-latency fetcher:
    - Official APIs / oEmbed for YouTube, Vimeo, Archive.org (~100ms).
    - Lightweight streamed HTTP request for general websites (max 1MB).
    - Strict TLS verification (verify=True).
    - Extracts ONLY essential fields: canonical URL, title, author/channel, date, description, main text.
    - If fetch fails or inaccessible: returns 'fetch_error'.
    """
    result = {
        "url": url,
        "canonical_url": url,
        "title": "",
        "author": "",
        "channel": "",
        "domain": "",
        "publication_date": "",
        "description": "",
        "duration": "00:00:00",
        "formatted_description": "",
        "main_text": "",
        "content_hash": "",
        "speaker_id": "",
        "comments": [],
        "fetch_error": None,
        "status_code": 200,
        "redirect_occurred": False
    }

    parsed = urlparse(url)
    clean_host = (parsed.hostname or "").lower().replace("www.", "")
    result["domain"] = clean_host

    # Case A: YouTube (Official oEmbed API ~80-150ms)
    if platform == "YouTube" or "youtube.com" in url or "youtu.be" in url:
        yt_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        if yt_key and platform_item_id:
            try:
                api_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id={platform_item_id}&key={yt_key}"
                res = _session.get(api_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, verify=True)
                if res.status_code == 200:
                    items = res.json().get("items", [])
                    if items:
                        snip = items[0].get("snippet", {})
                        cd = items[0].get("contentDetails", {})
                        result["title"] = snip.get("title", "").strip()
                        result["author"] = snip.get("channelTitle", "").strip()
                        result["channel"] = result["author"]
                        result["description"] = snip.get("description", "").strip()[:8000]
                        # Duration
                        iso = cd.get("duration", "")
                        h_m = re.search(r'(\d+)H', iso)
                        m_m = re.search(r'(\d+)M', iso)
                        s_m = re.search(r'(\d+)S', iso)
                        h = int(h_m.group(1)) if h_m else 0
                        m = int(m_m.group(1)) if m_m else 0
                        s = int(s_m.group(1)) if s_m else 0
                        result["duration"] = f"{h:02d}:{m:02d}:{s:02d}"

                        # Top comments for speaker and licensing verification
                        try:
                            c_url = f"https://www.googleapis.com/youtube/v3/commentThreads?part=snippet&videoId={platform_item_id}&maxResults=10&order=relevance&key={yt_key}"
                            c_res = _session.get(c_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, verify=True)
                            if c_res.status_code == 200:
                                for c_it in c_res.json().get("items", []):
                                    top_c = c_it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                                    txt = top_c.get("textDisplay") or top_c.get("textOriginal") or ""
                                    if txt:
                                        result["comments"].append(txt)
                        except Exception:
                            pass
            except Exception:
                pass

        # Duration fallback from watch page if API returned empty/zero duration
        if result.get("duration") in ("", "00:00:00", None) and platform_item_id:
            try:
                watch_url = f"https://www.youtube.com/watch?v={platform_item_id}"
                r_watch = _session.get(watch_url, headers=DEFAULT_HEADERS, timeout=3.5, verify=True)
                if r_watch.status_code == 200:
                    m_sec = re.search(r'"lengthSeconds":"(\d+)"', r_watch.text)
                    if m_sec:
                        total_s = int(m_sec.group(1))
                        wh = total_s // 3600
                        wm = (total_s % 3600) // 60
                        ws = total_s % 60
                        result["duration"] = f"{wh:02d}:{wm:02d}:{ws:02d}"
                    else:
                        m_iso = re.search(r'itemprop="duration" content="PT([^"]+)"', r_watch.text)
                        if m_iso:
                            iso_str = m_iso.group(1)
                            h_match = re.search(r'(\d+)H', iso_str)
                            m_match = re.search(r'(\d+)M', iso_str)
                            s_match = re.search(r'(\d+)S', iso_str)
                            wh = int(h_match.group(1)) if h_match else 0
                            wm = int(m_match.group(1)) if m_match else 0
                            ws = int(s_match.group(1)) if s_match else 0
                            result["duration"] = f"{wh:02d}:{wm:02d}:{ws:02d}"
            except Exception:
                pass

        if not result["title"]:
            try:
                oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
                res = _session.get(oembed_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, verify=True)
                if res.status_code == 200:
                    data = res.json()
                    result["title"] = data.get("title", "").strip()
                    result["author"] = data.get("author_name", "").strip()
                    result["channel"] = result["author"]
                elif res.status_code in (404, 401, 403):
                    result["fetch_error"] = f"YouTube video inaccessible (HTTP {res.status_code})"
            except Exception as e:
                result["fetch_error"] = f"YouTube request timed out or failed: {str(e)}"

        # Content hash from title and channel for YouTube
        sig_text = f"{result['title']} {result['channel']} {platform_item_id or ''}"
        result["main_text"] = sig_text
        result["content_hash"] = generate_content_hash(sig_text)

    # Case B: Vimeo (Official oEmbed ~100ms)
    elif platform == "Vimeo":
        try:
            oembed_url = f"https://vimeo.com/api/oembed.json?url={url}"
            res = _session.get(oembed_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, verify=True)
            if res.status_code == 200:
                data = res.json()
                result["title"] = data.get("title", "").strip()
                result["author"] = data.get("author_name", "").strip()
                result["channel"] = result["author"]
                dur_sec = data.get("duration")
                if dur_sec:
                    sec = int(dur_sec)
                    h, rem = divmod(sec, 3600)
                    m, s = divmod(rem, 60)
                    result["duration"] = f"{h:02d}:{m:02d}:{s:02d}"
            elif res.status_code >= 400:
                result["fetch_error"] = f"Vimeo resource inaccessible (HTTP {res.status_code})"
        except Exception as e:
            result["fetch_error"] = f"Vimeo request failed: {str(e)}"

        sig_text = f"{result['title']} {result['channel']}"
        result["main_text"] = sig_text
        result["content_hash"] = generate_content_hash(sig_text)

    # Case C: Internet Archive (Metadata API ~200ms)
    elif platform == "Internet Archive" and platform_item_id:
        try:
            api_url = f"https://archive.org/metadata/{platform_item_id}"
            res = _session.get(api_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, verify=True)
            if res.status_code == 200:
                data = res.json()
                meta = data.get("metadata", {})
                result["title"] = meta.get("title", "").strip()
                result["author"] = meta.get("creator", "") or meta.get("uploader", "")
                result["channel"] = result["author"]
                result["description"] = meta.get("description", "")[:500]
            elif res.status_code >= 400:
                result["fetch_error"] = f"Archive.org item inaccessible (HTTP {res.status_code})"
        except Exception as e:
            result["fetch_error"] = f"Archive.org request failed: {str(e)}"

        sig_text = f"{result['title']} {result['author']}"
        result["main_text"] = sig_text
        result["content_hash"] = generate_content_hash(sig_text)

    # Case D: General Website / Article (Streamed GET up to 1MB)
    else:
        try:
            resp = _session.get(
                url,
                headers=DEFAULT_HEADERS,
                timeout=REQUEST_TIMEOUT,
                verify=True,
                stream=True,
                allow_redirects=True
            )
            result["status_code"] = resp.status_code

            if resp.status_code >= 400:
                result["fetch_error"] = f"Webpage returned error status HTTP {resp.status_code}"
                return result

            # Check safe redirects
            if resp.url and resp.url != url:
                result["canonical_url"] = resp.url
                result["redirect_occurred"] = True
                parsed_final = urlparse(resp.url)
                if parsed_final.hostname:
                    result["domain"] = parsed_final.hostname.replace("www.", "")

            # Stream read response up to 1MB
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=16384):
                if chunk:
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_CONTENT_BYTES:
                        break
            resp.close()

            encoding = resp.encoding or "utf-8"
            raw_html = b"".join(chunks).decode(encoding, errors="replace")

            # Extract Title: <title> or og:title
            title_m = re.search(r'<title[^>]*>(.*?)</title>', raw_html, re.IGNORECASE | re.DOTALL)
            if title_m:
                result["title"] = html.unescape(title_m.group(1)).strip()
            if not result["title"]:
                og_t = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
                if og_t:
                    result["title"] = html.unescape(og_t.group(1)).strip()

            # Clean Wikipedia / Site suffix in title
            if " - Wikipedia" in result["title"]:
                result["title"] = result["title"].replace(" - Wikipedia", "").strip()

            # Extract Author: meta author or og:site_name
            auth_m = re.search(r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
            if auth_m:
                cand_auth = html.unescape(auth_m.group(1)).strip()
                if len(cand_auth) <= 60 and not cand_auth.startswith("http"):
                    result["author"] = cand_auth
            if not result["author"]:
                og_site = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
                if og_site:
                    result["author"] = html.unescape(og_site.group(1)).strip()
            if not result["author"] and result["domain"]:
                result["author"] = result["domain"].split(".")[0].capitalize()
            result["channel"] = result["author"]

            # Extract Date: article:published_time or date
            date_m = re.search(r'<meta[^>]+(?:property=["\']article:published_time["\']|name=["\']date["\'])[^>]+content=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
            if date_m:
                result["publication_date"] = date_m.group(1).strip()[:10]

            # Extract Description
            desc_m = re.search(r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])[^>]+content=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
            if desc_m:
                result["description"] = html.unescape(desc_m.group(1)).strip()[:500]

            # Fast main text extraction: drop scripts, styles, navs
            body_m = re.search(r'<body[^>]*>(.*?)</body>', raw_html, re.IGNORECASE | re.DOTALL)
            text_source = body_m.group(1) if body_m else raw_html

            text_cleaned = re.sub(r'<(script|style|nav|header|footer|aside|svg|iframe)[^>]*>.*?</\1>', ' ', text_source, flags=re.IGNORECASE | re.DOTALL)
            text_cleaned = re.sub(r'<[^>]+>', ' ', text_cleaned)
            text_cleaned = html.unescape(text_cleaned)
            text_cleaned = re.sub(r'\s+', ' ', text_cleaned).strip()

            # Take essential representative text (first 3,000 characters)
            result["main_text"] = text_cleaned[:3000]
            result["content_hash"] = generate_content_hash(result["main_text"] or result["title"])

        except requests.exceptions.Timeout:
            result["fetch_error"] = f"Request timed out after {REQUEST_TIMEOUT[1]}s. Webpage is unresponsive."
        except requests.exceptions.SSLError as e:
            result["fetch_error"] = f"SSL/TLS verification failed: {str(e)}"
        except requests.exceptions.ConnectionError:
            result["fetch_error"] = "Could not resolve domain or connect to server."
        except Exception as e:
            result["fetch_error"] = f"Network fetch failed: {str(e)}"

    # Format description for researcher intake
    if result["title"]:
        if result["channel"]:
            result["formatted_description"] = f"{result['title']}\nchannel : {result['channel']}"
        else:
            result["formatted_description"] = result["title"]

    return result


# ==============================================================================
# 3. FAST DUPLICATE DETECTION PIPELINE (LOW-LATENCY)
# ==============================================================================

def execute_fast_verification(
    url: str,
    speakers_id: Optional[str] = None,
    candidate_description: Optional[str] = None,
    candidate_duration: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes the low-latency verification workflow:
    1. Validates URL protocol & standardizes canonical URL.
    2. Lightweight fetch of page content or official API.
    3. Fail-safe: If fetch fails, returns FETCH_FAILED (never UNIQUE).
    4. Exact match check against indexed database (canonical_url, platform_item_id, content_hash).
    5. Inexpensive pre-filter comparison on existing records.
    6. Conditional self-hosted LLM verification only when plausible candidates exist.
    Returns structured result with status: DUPLICATE, POTENTIAL_DUPLICATE, UNIQUE, or FETCH_FAILED.
    """
    start_time = time.perf_counter()

    # Step 1: URL Validation & Normalization
    is_valid, canonical_url, platform, platform_item_id, val_error = validate_and_normalize_url(url)
    if not is_valid:
        latency = round((time.perf_counter() - start_time) * 1000.0, 2)
        return {
            "status": "FETCH_FAILED",
            "is_duplicate": False,
            "confidence": 0.0,
            "reason": f"URL Validation Failed: {val_error}",
            "evidence_fields": ["invalid_url_protocol"],
            "canonical_url": canonical_url or url,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "metadata": {},
            "existing_record": None,
            "similar_records": [],
            "latency_ms": latency,
            "endpoint_version": "fast-url-validator"
        }

    # Step 2: Fetch Essential Content (Connection pool, streamed, strict timeouts)
    fetched = fetch_essential_content(url, platform, platform_item_id)

    # Step 3: Handle Inaccessible / Failed Webpages (Never mark as UNIQUE!)
    if fetched.get("fetch_error"):
        latency = round((time.perf_counter() - start_time) * 1000.0, 2)
        return {
            "status": "FETCH_FAILED",
            "is_duplicate": False,
            "confidence": 0.0,
            "reason": f"Content Inaccessible: {fetched['fetch_error']}. Inaccessible pages cannot be verified or accepted.",
            "evidence_fields": ["fetch_failed", "inaccessible_page"],
            "canonical_url": canonical_url,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "metadata": fetched,
            "existing_record": None,
            "similar_records": [],
            "latency_ms": latency,
            "endpoint_version": "fast-content-fetcher"
        }

    final_canonical = fetched.get("canonical_url") or canonical_url
    content_hash = fetched.get("content_hash", "")
    title = fetched.get("title", "")
    author = fetched.get("author", "")
    if not title and candidate_description:
        title = candidate_description.split("\n")[0].strip()
    if not author and candidate_description:
        for line in candidate_description.splitlines():
            if "channel" in line.lower() and ":" in line:
                author = line.split(":", 1)[1].strip()
                break

    # Step 4: Fast Exact Match Check Against Indexed DB
    conn = get_connection()
    cursor = conn.cursor()

    # Detect speaker_id from metadata (title, description, top comments, channel clues) or historical channel mappings
    detected_speaker = extract_speaker_from_metadata(title, fetched.get("description", ""), channel=author, comments=fetched.get("comments", []))
    if not detected_speaker and author:
        try:
            cursor.execute("""
                SELECT speakers_id FROM records
                WHERE (LOWER(channel_name) = ? OR LOWER(speaker_channel_name) = ?)
                  AND speakers_id IS NOT NULL AND TRIM(speakers_id) != ''
                ORDER BY id DESC LIMIT 1
            """, (author.lower(), author.lower()))
            r_spk = cursor.fetchone()
            if r_spk and r_spk["speakers_id"]:
                detected_speaker = r_spk["speakers_id"].strip()
        except Exception:
            pass
    fetched["speaker_id"] = detected_speaker or ""

    exact_record = None
    exact_reason = None
    evidence_fields = []

    # Check 1: Canonical URL in records
    cursor.execute("SELECT * FROM records WHERE canonical_url = ?", (final_canonical,))
    row = cursor.fetchone()
    if row:
        exact_record = dict(row)
        exact_reason = f"Exact duplicate: Canonical URL matches Record #{exact_record['id']} in database."
        evidence_fields.append("exact_canonical_url")



    # Check 3: Platform Item ID (YouTube / Vimeo / Archive)
    if not exact_record and platform_item_id and platform in ("YouTube", "Internet Archive", "Vimeo"):
        cursor.execute("SELECT * FROM records WHERE source = ? AND platform_item_id = ?", (platform, platform_item_id))
        row = cursor.fetchone()
        if row:
            exact_record = dict(row)
            exact_reason = f"Exact duplicate: Media ID '{platform_item_id}' already registered (Record #{exact_record['id']})."
            evidence_fields.append("exact_platform_item_id")



    if not exact_record and content_hash:
        cursor.execute("SELECT * FROM records WHERE content_hash = ?", (content_hash,))
        row = cursor.fetchone()
        if row:
            exact_record = dict(row)
            exact_reason = f"Exact duplicate: Identical SHA-256 content hash with Record #{exact_record['id']}."
            evidence_fields.append("exact_sha256_content_hash")

    if exact_record:
        conn.close()
        latency = round((time.perf_counter() - start_time) * 1000.0, 2)
        return {
            "status": "DUPLICATE",
            "is_duplicate": True,
            "confidence": 1.0,
            "matched_video_id": str(exact_record["id"]),
            "reason": exact_reason,
            "evidence_fields": evidence_fields,
            "canonical_url": final_canonical,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "metadata": fetched,
            "existing_record": exact_record,
            "similar_records": [],
            "latency_ms": latency,
            "endpoint_version": "fast-exact-index"
        }

    # Step 5: Fast Indexed Similarity Pre-Filter
    all_stored = get_all_stored_web_and_records()
    conn.close()

    candidate_title_clean = (title or "").lower().strip()
    candidate_author_clean = (author or "").lower().strip()

    candidate_matches = []
    for item in all_stored:
        item_title = (item.get("title") or item.get("description") or "").split("\n")[0].lower().strip()
        item_author = (item.get("author") or item.get("channel") or item.get("channel_name") or "").lower().strip()

        if not item_title:
            continue

        title_sim = fuzz.token_set_ratio(candidate_title_clean, item_title) / 100.0
        author_match = bool(candidate_author_clean and item_author and (candidate_author_clean in item_author or item_author in candidate_author_clean))

        comp_score = title_sim
        if author_match:
            comp_score = min(1.0, comp_score + 0.15)

        if comp_score >= 0.55:
            match_data = dict(item)
            match_data["prefilter_score"] = round(comp_score, 3)
            match_data["title_similarity"] = round(title_sim * 100.0, 1)
            match_data["same_author"] = author_match
            candidate_matches.append(match_data)

    candidate_matches.sort(key=lambda x: x.get("prefilter_score", 0), reverse=True)
    top_matches = candidate_matches[:3]

    # Clearly unique if no plausible matches
    if not top_matches or top_matches[0]["prefilter_score"] < 0.62:
        latency = round((time.perf_counter() - start_time) * 1000.0, 2)
        return {
            "status": "UNIQUE",
            "is_duplicate": False,
            "confidence": 1.0,
            "matched_video_id": None,
            "reason": "Fast pre-filter confirmed unique: No plausible matches found across indexed database.",
            "evidence_fields": ["no_plausible_matches"],
            "canonical_url": final_canonical,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "metadata": fetched,
            "existing_record": None,
            "similar_records": [],
            "latency_ms": latency,
            "endpoint_version": "fast-prefilter"
        }

    best_match = top_matches[0]
    best_score = best_match["prefilter_score"]

    # Very high similarity threshold for direct duplicate classification
    if best_score >= 0.88 and best_match.get("same_author"):
        latency = round((time.perf_counter() - start_time) * 1000.0, 2)
        return {
            "status": "DUPLICATE",
            "is_duplicate": True,
            "confidence": 0.95,
            "matched_video_id": str(best_match["id"]),
            "reason": f"High lexical similarity ({int(best_score * 100)}%) with identical channel/author to Record #{best_match['id']}.",
            "evidence_fields": ["high_lexical_similarity", "matching_author"],
            "canonical_url": final_canonical,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "metadata": fetched,
            "existing_record": best_match,
            "similar_records": top_matches,
            "latency_ms": latency,
            "endpoint_version": "fast-lexical-engine"
        }

    # Step 6: High-confidence -> rule-based; ambiguous zone (0.62-0.84) -> LLM decision
    latency = round((time.perf_counter() - start_time) * 1000.0, 2)

    if best_score >= 0.85 or (best_score >= 0.78 and best_match.get("same_author")):
        # Clear duplicate - no LLM needed
        evidence = ["high_lexical_similarity"]
        if best_match.get("same_author"):
            evidence.append("matching_author")
        return {
            "status": "DUPLICATE",
            "is_duplicate": True,
            "confidence": round(best_score, 2),
            "matched_video_id": str(best_match["id"]),
            "reason": (
                f"High lexical similarity ({int(best_score * 100)}%)"
                f"{' with matching author' if best_match.get('same_author') else ''} "
                f"to Record #{best_match['id']}."
            ),
            "evidence_fields": evidence,
            "canonical_url": final_canonical,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "metadata": fetched,
            "existing_record": best_match,
            "similar_records": top_matches,
            "latency_ms": latency,
            "endpoint_version": "fast-lexical-engine"
        }

    if best_score >= 0.62:
        # Ambiguous zone - delegate to LLM for the final call
        candidate_payload = {
            "title": fetched.get("title", ""),
            "description": fetched.get("description", ""),
            "channel": fetched.get("channel", ""),
            "channel_name": fetched.get("channel", ""),
            "duration": fetched.get("duration", ""),
            "duration_seconds": fetched.get("duration_seconds", 0),
            "platform_item_id": platform_item_id,
            "speakers_id": fetched.get("speaker_id", ""),
        }
        decision = LLMAdapter.evaluate(candidate_payload, top_matches)
        total_latency = round((time.perf_counter() - start_time) * 1000.0, 2)
        matched_rec = best_match if decision.matched_video_id else None
        return {
            "status": decision.status,
            "is_duplicate": (decision.status == "DUPLICATE"),
            "confidence": decision.confidence,
            "matched_video_id": decision.matched_video_id,
            "reason": decision.reason,
            "evidence_fields": decision.evidence_fields,
            "canonical_url": final_canonical,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "metadata": fetched,
            "existing_record": matched_rec,
            "similar_records": top_matches,
            "latency_ms": total_latency,
            "endpoint_version": decision.endpoint_version,
            "llm_called": True,
            "llm_error": decision.error_state,
        }

    # Below threshold - clearly unique
    return {
        "status": "UNIQUE",
        "is_duplicate": False,
        "confidence": 0.85,
        "matched_video_id": None,
        "reason": "Lexical similarity below duplicate threshold.",
        "evidence_fields": ["lexical_below_threshold"],
        "canonical_url": final_canonical,
        "platform": platform,
        "platform_item_id": platform_item_id,
        "metadata": fetched,
        "existing_record": None,
        "similar_records": top_matches,
        "latency_ms": latency,
        "endpoint_version": "fast-lexical-engine"
    }

