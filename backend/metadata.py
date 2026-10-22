"""
Metadata extraction engine for YouTube, Internet Archive, Vimeo, and web media.
Fetches title, channel/uploader name, and duration automatically.
"""

import re
import os
import requests
from typing import Dict, Any, Optional
from urllib.parse import urlparse

def extract_media_metadata(url: str, platform: str, platform_item_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Extracts title, channel/author name, and duration string (HH:MM:SS) from the URL.
    Returns:
    {
        "title": str,
        "channel": str,
        "duration": str,
        "formatted_description": str
    }
    """
    url = (url or "").strip()
    result = {
        "title": "",
        "channel": "",
        "duration": "",
        "formatted_description": ""
    }
    if not url:
        return result

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }

    # 1. YouTube
    if platform == "YouTube" or "youtube.com" in url or "youtu.be" in url:
        yt_api_key = os.getenv("YOUTUBE_API_KEY", "").strip()

        # A. Official YouTube Data API v3 if API key is configured
        if yt_api_key and platform_item_id:
            try:
                api_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id={platform_item_id}&key={yt_api_key}"
                res = requests.get(api_url, headers=headers, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("items", [])
                    if items:
                        snippet = items[0].get("snippet", {})
                        content_details = items[0].get("contentDetails", {})
                        result["title"] = snippet.get("title", "").strip()
                        result["channel"] = snippet.get("channelTitle", "").strip()
                        
                        # ISO 8601 duration e.g. PT1H2M3S or PT3M33S
                        iso = content_details.get("duration", "")
                        h_m = re.search(r'(\d+)H', iso)
                        m_m = re.search(r'(\d+)M', iso)
                        s_m = re.search(r'(\d+)S', iso)
                        h = int(h_m.group(1)) if h_m else 0
                        m_rem = int(m_m.group(1)) if m_m else 0
                        s_rem = int(s_m.group(1)) if s_m else 0
                        result["duration"] = f"{h:02d}:{m_rem:02d}:{s_rem:02d}"
            except Exception:
                pass

        # B. Fallback: Title & Channel via oEmbed if not already extracted
        if not result["title"] or not result["channel"]:
            try:
                oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
                res = requests.get(oembed_url, headers=headers, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    if not result["title"]:
                        result["title"] = data.get("title", "").strip()
                    if not result["channel"]:
                        result["channel"] = data.get("author_name", "").strip()
            except Exception:
                pass

        # C. Fallback: Duration and metadata via watch page if still missing
        if not result["duration"] or not result["title"] or not result["channel"]:
            try:
                watch_url = f"https://www.youtube.com/watch?v={platform_item_id}" if platform_item_id else url
                r = requests.get(watch_url, headers=headers, timeout=5)
                if r.status_code == 200:
                    if not result["duration"]:
                        # Try lengthSeconds
                        m = re.search(r'"lengthSeconds":"(\d+)"', r.text)
                        if m:
                            sec = int(m.group(1))
                            h = sec // 3600
                            m_rem = (sec % 3600) // 60
                            s_rem = sec % 60
                            result["duration"] = f"{h:02d}:{m_rem:02d}:{s_rem:02d}"
                        else:
                            # Try itemprop duration (PT1H2M3S)
                            m2 = re.search(r'itemprop="duration" content="PT([^"]+)"', r.text)
                            if m2:
                                iso = m2.group(1)
                                h_m = re.search(r'(\d+)H', iso)
                                m_m = re.search(r'(\d+)M', iso)
                                s_m = re.search(r'(\d+)S', iso)
                                h = int(h_m.group(1)) if h_m else 0
                                m_rem = int(m_m.group(1)) if m_m else 0
                                s_rem = int(s_m.group(1)) if s_m else 0
                                result["duration"] = f"{h:02d}:{m_rem:02d}:{s_rem:02d}"

                    # Fallback for title/channel if still missing
                    if not result["title"]:
                        t_match = re.search(r'<title>(.*?)</title>', r.text)
                        if t_match:
                            raw_t = t_match.group(1).replace(" - YouTube", "").strip()
                            result["title"] = raw_t
                    if not result["channel"]:
                        c_match = re.search(r'"ownerChannelName":"([^"]+)"', r.text)
                        if c_match:
                            result["channel"] = c_match.group(1).strip()
            except Exception:
                pass

    # 2. Internet Archive
    elif platform == "Internet Archive" and platform_item_id:
        try:
            api_url = f"https://archive.org/metadata/{platform_item_id}"
            res = requests.get(api_url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                meta = data.get("metadata", {})
                result["title"] = meta.get("title", "")
                result["channel"] = meta.get("creator", "") or meta.get("uploader", "")
                
                # Try duration from metadata
                length_sec = None
                if "length" in meta:
                    try:
                        length_sec = int(float(meta["length"]))
                    except Exception:
                        pass
                
                if length_sec is None:
                    # check files list
                    for f in data.get("files", []):
                        if "length" in f:
                            try:
                                length_sec = int(float(f["length"]))
                                break
                            except Exception:
                                pass
                
                if length_sec:
                    h = length_sec // 3600
                    m_rem = (length_sec % 3600) // 60
                    s_rem = length_sec % 60
                    result["duration"] = f"{h:02d}:{m_rem:02d}:{s_rem:02d}"
        except Exception:
            pass

    # 3. Vimeo
    elif platform == "Vimeo":
        try:
            oembed_url = f"https://vimeo.com/api/oembed.json?url={url}"
            res = requests.get(oembed_url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                result["title"] = data.get("title", "")
                result["channel"] = data.get("author_name", "")
                dur_sec = data.get("duration")
                if dur_sec:
                    sec = int(dur_sec)
                    h = sec // 3600
                    m_rem = (sec % 3600) // 60
                    s_rem = sec % 60
                    result["duration"] = f"{h:02d}:{m_rem:02d}:{s_rem:02d}"
        except Exception:
            pass

    # 4. General Websites / Articles / Web Search Results
    else:
        try:
            from backend.web_verifier import extract_and_normalize_webpage, generate_content_hash
            extracted = extract_and_normalize_webpage(url, timeout=5)
            if extracted.get("title"):
                result["title"] = extracted["title"]
            if extracted.get("author"):
                result["channel"] = extracted["author"]
            elif extracted.get("domain"):
                raw_domain = extracted["domain"].replace("www.", "")
                result["channel"] = raw_domain.split(".")[0].capitalize()
            
            result["content_hash"] = generate_content_hash(extracted.get("normalized_text", ""))
            result["headings"] = extracted.get("headings", [])
            result["word_count"] = extracted.get("word_count", 0)
            result["normalized_text"] = extracted.get("normalized_text", "")
            result["publication_date"] = extracted.get("publication_date", "")
            if not result.get("duration"):
                result["duration"] = "00:00:00"
        except Exception as e:
            pass

    # Format description according to user specification:
    # Title
    # channel : <channel_name>
    if result["title"]:
        if result["channel"]:
            result["formatted_description"] = f"{result['title']}\nchannel : {result['channel']}"
        else:
            result["formatted_description"] = result["title"]

    return result
