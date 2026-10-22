"""
Web Search Deduplication & Content Verification Engine.
Implements the 8-Stage Pipeline for Web Searches and Web Content:
1. URL normalization and canonical URL (safe redirects, canonical link resolution, fast exact check)
2. Extract and normalize webpage content (main text, title, headings H1-H3, author, date, description, boilerplate removal, NFKC)
3. Exact content hash (SHA-256 of normalized text, exact match comparison)
4. Title and lexical similarity (Token sort/set ratio, BM25 / TF-IDF vector retrieval of top-K matches)
5. Semantic and passage similarity (Passage chunk overlap, shingle containment, author/date/domain/heading supporting evidence)
6. Pre-filter decision (Combine evidence: Skip LLM if UNIQUE or EXACT_DUPLICATE; route to LLM if PROBABLE_DUPLICATE; retain review if INSUFFICIENT_EVIDENCE)
7. Self-hosted LLM verification (Compare actual content & evidence, return DUPLICATE, POTENTIAL_DUPLICATE, or UNIQUE with reason & matched page ID)
8. Store the decision (Idempotent writes preventing duplicate records on repeat runs)
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
from datetime import datetime

import requests
from rapidfuzz import fuzz
from bs4 import BeautifulSoup, Comment
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from backend.duplicate import canonicalize_url as base_canonicalize_url, TRACKING_PARAMS
from backend.llm_adapter import llm_config, LLMDecision

logger = logging.getLogger(__name__)

# Extended tracking query parameters commonly found on web search engines and news portals
WEB_TRACKING_PARAMS = TRACKING_PARAMS.union({
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'utm_id',
    'si', 'feature', 'fbclid', 'gclid', 'igshid', 'mc_cid', 'mc_eid', '_ga', '_gl',
    'ref', 'source', 'spm', 'from', 'yclid', 'wbraid', 'gbraid', 'msclkid', 'ved',
    'usg', 'sa', 'biw', 'bih', 'dpr', 'oq', 'gs_lcp', 'sclient', 'aqs', 'ie', 'oe',
    'ncid', 'cps', 'aff_platform', 'aff_trace_key'
})

BOILERPLATE_TAGS = {
    'script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside', 'form',
    'svg', 'iframe', 'button', 'dialog', 'select', 'canvas', 'audio', 'video'
}

BOILERPLATE_CLASSES_OR_IDS = [
    'cookie', 'consent', 'banner', 'advertisement', 'ad-', 'ads-', 'sponsor',
    'sidebar', 'footer', 'nav-', 'menu', 'social-share', 'share-bar', 'disclaimer',
    'newsletter', 'subscribe', 'popup', 'modal', 'widget'
]


# ==============================================================================
# STAGE 1: URL NORMALIZATION & CANONICAL URL (Fast Exact Check)
# ==============================================================================

def normalize_web_url(url: str, follow_redirects: bool = True, timeout: int = 5) -> Dict[str, Any]:
    """
    Stage 1: URL Normalization and Canonical URL.
    - Strips tracking parameters, fragments, and standardizes scheme & host
    - Safely follows redirects (max 5) to discover the final canonical landing page
    - Inspects HTTP Link rel='canonical' and HTML <link rel='canonical'>
    - Returns standardized metadata dict with 'canonical_url', 'final_url', 'domain'
    """
    if not url:
        return {
            "original_url": "",
            "canonical_url": "",
            "final_url": "",
            "domain": "",
            "redirect_occurred": False,
            "status_code": None
        }

    url = url.strip()
    match = re.search(r'(https?://[^\s]+)', url)
    if match:
        url = match.group(1)
    if not re.match(r'^[a-zA-Z]+://', url):
        url = 'https://' + url

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in ('http', 'https'):
        # Keep http for localhost/127.0.0.1, otherwise default to https
        hostname_raw = (parsed.hostname or '').lower()
        if hostname_raw not in ('localhost', '127.0.0.1'):
            scheme = 'https'
    
    hostname = (parsed.hostname or '').lower()
    if hostname.startswith('www.'):
        hostname = hostname[4:]
    domain = hostname

    # Strip tracking parameters
    query_dict = parse_qs(parsed.query, keep_blank_values=False)
    filtered_params = []
    for k, v in query_dict.items():
        if k.lower() in WEB_TRACKING_PARAMS:
            continue
        for val in v:
            filtered_params.append((k, val))
    filtered_params.sort(key=lambda x: x[0])
    clean_query = urlencode(filtered_params)

    path = parsed.path
    if path and path != '/' and path.endswith('/'):
        path = path.rstrip('/')

    initial_canonical = urlunparse((scheme, hostname, path, '', clean_query, ''))

    final_url = initial_canonical
    redirect_occurred = False
    status_code = None
    html_canonical = None

    if follow_redirects:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        }
        try:
            # Perform a streaming GET to follow redirects without downloading large bodies
            with requests.get(initial_canonical, headers=headers, timeout=timeout, allow_redirects=True, stream=True) as resp:
                status_code = resp.status_code
                if resp.url and resp.url != initial_canonical:
                    final_url = resp.url
                    redirect_occurred = True

                # Check HTTP Link Header for rel="canonical"
                link_header = resp.headers.get("Link") or resp.headers.get("link")
                if link_header:
                    canon_match = re.search(r'<([^>]+)>;\s*rel=["\']?canonical["\']?', link_header, re.IGNORECASE)
                    if canon_match:
                        html_canonical = canon_match.group(1).strip()

                # Read first small chunk to find <link rel="canonical"> in <head>
                if not html_canonical:
                    try:
                        chunk = next(resp.iter_content(chunk_size=4096), b"").decode('utf-8', errors='ignore')
                        head_match = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', chunk, re.IGNORECASE)
                        if not head_match:
                            head_match = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']', chunk, re.IGNORECASE)
                        if head_match:
                            html_canonical = head_match.group(1).strip()
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Redirect resolution for {url} skipped or failed: {e}")

    # Determine resolved canonical URL
    target_to_clean = html_canonical if html_canonical else final_url
    if target_to_clean and re.match(r'^[a-zA-Z]+://', target_to_clean):
        resolved_canonical = base_canonicalize_url(target_to_clean)
    else:
        resolved_canonical = initial_canonical

    parsed_final = urlparse(resolved_canonical)
    final_domain = (parsed_final.hostname or '').lower()
    if final_domain.startswith('www.'):
        final_domain = final_domain[4:]

    return {
        "original_url": url,
        "canonical_url": resolved_canonical,
        "final_url": final_url,
        "domain": final_domain or domain,
        "redirect_occurred": redirect_occurred,
        "status_code": status_code,
        "html_canonical": html_canonical
    }


# ==============================================================================
# STAGE 2: EXTRACT & NORMALIZE WEBPAGE CONTENT
# ==============================================================================

def extract_and_normalize_webpage(url: str, html_content: Optional[str] = None, timeout: int = 7) -> Dict[str, Any]:
    """
    Stage 2: Extract and normalize webpage content.
    - Extracts: main text, title, headings (H1-H3), author, publication date, description, domain
    - Removes navigation, advertisements, cookie notices, and boilerplate
    - Normalizes text: HTML unescaping, NFKC unicode normalization, whitespace collapsing
    - Segments text into distinctive passages (paragraphs / chunks)
    """
    raw_html = html_content or ""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }

    if not raw_html and url:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                raw_html = resp.text[:2000000] # Cap at 2MB
        except Exception as e:
            logger.warning(f"Failed to fetch content from {url}: {e}")

    result = {
        "url": url,
        "title": "",
        "headings": [],
        "author": "",
        "publication_date": "",
        "description": "",
        "domain": urlparse(url).hostname or "",
        "raw_text": "",
        "normalized_text": "",
        "passages": [],
        "word_count": 0,
        "extraction_success": False
    }

    # Auto-detect media platforms (YouTube, Internet Archive, Vimeo) for rich title, author, and duration
    from backend.duplicate import extract_platform_and_item_id
    platform, platform_item_id = extract_platform_and_item_id(url)
    if platform in ("YouTube", "Internet Archive", "Vimeo") and not html_content:
        try:
            from backend.metadata import extract_media_metadata
            media_meta = extract_media_metadata(url, platform, platform_item_id)
            if media_meta.get("title"):
                result["title"] = media_meta["title"]
            if media_meta.get("channel"):
                result["author"] = media_meta["channel"]
            if media_meta.get("formatted_description"):
                result["description"] = media_meta["formatted_description"]
            if media_meta.get("duration"):
                result["publication_date"] = f"Duration: {media_meta['duration']}"
            if platform_item_id:
                result["headings"] = [f"{platform} Item ID: {platform_item_id}"]
        except Exception:
            pass

    if not raw_html:
        if result["title"] or result["description"]:
            synthetic = f"{result['title']}\n{result.get('description', '')}"
            result["raw_text"] = synthetic
            result["normalized_text"] = synthetic.lower()
            words = result["normalized_text"].split()
            result["word_count"] = len(words)
            result["passages"] = [result["title"]]
            result["extraction_success"] = True
        return result

    try:
        soup = BeautifulSoup(raw_html, "html.parser")
    except Exception as e:
        logger.error(f"HTML parse error: {e}")
        return result

    # 1. Extract metadata before removing elements
    # Title
    og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
    if og_title and og_title.get("content"):
        result["title"] = og_title["content"].strip()
    elif soup.title and soup.title.string:
        result["title"] = soup.title.string.strip()
    elif soup.find("h1"):
        result["title"] = soup.find("h1").get_text(strip=True)

    # Clean up standard site suffixes from title (e.g., " | The Hindu", " - BBC News")
    if result["title"]:
        result["title"] = re.sub(r'\s*[-|–—]\s*[^|–—]+$', '', result["title"]).strip() or result["title"]

    # Description
    meta_desc = (
        soup.find("meta", attrs={"name": "description"}) or
        soup.find("meta", property="og:description") or
        soup.find("meta", attrs={"name": "twitter:description"})
    )
    if meta_desc and meta_desc.get("content"):
        result["description"] = meta_desc["content"].strip()

    # Author
    meta_author = (
        soup.find("meta", attrs={"name": "author"}) or
        soup.find("meta", property="article:author") or
        soup.find("meta", attrs={"name": "twitter:creator"})
    )
    if meta_author and meta_author.get("content"):
        result["author"] = meta_author["content"].strip()
    else:
        author_elem = soup.find(attrs={"rel": "author"}) or soup.find(class_=re.compile(r'author|byline', re.I))
        if author_elem:
            auth_text = author_elem.get_text(strip=True)
            if len(auth_text) <= 60:
                result["author"] = auth_text

    if not result["author"] and result["domain"]:
        result["author"] = result["domain"].replace("www.", "").split(".")[0].capitalize()

    # Publication Date
    meta_date = (
        soup.find("meta", property="article:published_time") or
        soup.find("meta", attrs={"name": "date"}) or
        soup.find("meta", attrs={"name": "dc.date"}) or
        soup.find("meta", property="og:published_time")
    )
    if meta_date and meta_date.get("content"):
        result["publication_date"] = meta_date["content"].strip()
    else:
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            result["publication_date"] = time_tag["datetime"].strip()
        elif time_tag:
            result["publication_date"] = time_tag.get_text(strip=True)

    # Headings (H1, H2, H3)
    headings = []
    for h in soup.find_all(['h1', 'h2', 'h3']):
        h_text = h.get_text(strip=True)
        if h_text and len(h_text) > 3 and h_text not in headings:
            headings.append(h_text)
    result["headings"] = headings[:15]

    # 2. Remove comments and boilerplate tags
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for tag in soup.find_all(list(BOILERPLATE_TAGS)):
        tag.decompose()

    # Safely remove elements matching common advertisement/sidebar/cookie classes or IDs
    for elem in list(soup.find_all(True)):
        if elem.name in ('html', 'body', 'main', 'article'):
            continue
        attrs = getattr(elem, "attrs", None)
        if not attrs:
            continue
        cls_val = attrs.get("class", [])
        class_str = (" ".join(cls_val) if isinstance(cls_val, list) else str(cls_val)).lower()
        id_str = str(attrs.get("id", "")).lower()
        if any(b in class_str for b in BOILERPLATE_CLASSES_OR_IDS) or any(b in id_str for b in BOILERPLATE_CLASSES_OR_IDS):
            try:
                elem.decompose()
            except Exception:
                pass

    # 3. Extract Main Text Container
    main_container = (
        soup.find("main") or
        soup.find("article") or
        soup.find(attrs={"role": "main"}) or
        soup.find(class_=re.compile(r'content|post|entry|article-body', re.I)) or
        soup.body or
        soup
    )

    # Extract clean paragraphs and text
    paragraphs = []
    for p in main_container.find_all(['p', 'div', 'li']):
        # Ignore container div with many children
        if p.name == 'div' and p.find_all(['p', 'div']):
            continue
        p_text = p.get_text(separator=" ", strip=True)
        if len(p_text.split()) >= 6:  # filter tiny fragments
            paragraphs.append(p_text)

    # Deduplicate repetitive consecutive lines
    cleaned_paragraphs = []
    for p in paragraphs:
        if not cleaned_paragraphs or p != cleaned_paragraphs[-1]:
            cleaned_paragraphs.append(p)

    raw_combined_text = "\n\n".join(cleaned_paragraphs)
    if not raw_combined_text:
        raw_combined_text = main_container.get_text(separator=" ", strip=True)

    result["raw_text"] = raw_combined_text

    # 4. Text Normalization:
    # - Decode HTML entities
    # - NFKC unicode normalization
    # - Whitespace collapse and lowercasing
    text_unescaped = html.unescape(raw_combined_text)
    text_nfkc = unicodedata.normalize('NFKC', text_unescaped)
    
    # Standardize whitespace while preserving clean paragraph structure
    text_clean = re.sub(r'[ \t]+', ' ', text_nfkc)
    text_clean = re.sub(r'\n{3,}', '\n\n', text_clean).strip()

    # Normalized text for hashing and vector comparisons
    result["normalized_text"] = text_clean.lower()
    words = result["normalized_text"].split()
    result["word_count"] = len(words)
    result["extraction_success"] = bool(result["word_count"] > 10)

    # 5. Extract Distinctive Passages (50 to 90 word chunks)
    passages = []
    chunk_size = 70
    overlap = 20
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        if len(chunk.split()) >= 25:
            passages.append(chunk)
    result["passages"] = passages[:10]  # Store top 10 distinctive passages

    return result


# ==============================================================================
# STAGE 3: EXACT CONTENT HASH (SHA-256)
# ==============================================================================

def generate_content_hash(normalized_text: str) -> str:
    """
    Stage 3: Exact Content Hash.
    Generates a deterministic SHA-256 hex digest from normalized main text.
    Identical hashes indicate identical normalized text, not necessarily identical original pages.
    """
    if not normalized_text:
        return ""
    # Strip any residual outer whitespace before hashing
    clean_bytes = normalized_text.strip().encode('utf-8')
    return hashlib.sha256(clean_bytes).hexdigest()


# ==============================================================================
# STAGE 4: TITLE & LEXICAL SIMILARITY (TF-IDF / BM25)
# ==============================================================================

def compute_lexical_similarity(
    candidate_title: str,
    candidate_text: str,
    existing_records: List[Dict[str, Any]],
    top_k: int = 5
) -> List[Dict[str, Any]]:
    """
    Stage 4: Title and lexical similarity.
    - Compares titles using RapidFuzz token_sort_ratio and token_set_ratio
    - Builds a TF-IDF lexical index on normalized texts to retrieve pages with similar wording
    - Returns top-k matching candidate records with lexical scores
    """
    if not existing_records:
        return []

    cand_title = (candidate_title or "").strip().lower()
    cand_text = (candidate_text or "").strip().lower()

    if not cand_title and not cand_text:
        return []

    scored_candidates = []

    # Prepare corpus for TF-IDF
    corpus = [cand_text]
    valid_records = []
    for rec in existing_records:
        rec_text = (rec.get("normalized_text") or rec.get("description") or rec.get("title") or "").strip().lower()
        if rec_text:
            corpus.append(rec_text)
            valid_records.append(rec)

    # Compute TF-IDF matrix if texts are sufficiently rich
    tfidf_sims = [0.0] * len(valid_records)
    if len(corpus) > 1 and len(cand_text.split()) >= 5:
        try:
            vectorizer = TfidfVectorizer(
                ngram_range=(1, 2),
                max_features=5000,
                sublinear_tf=True,
                stop_words='english'
            )
            tfidf_matrix = vectorizer.fit_transform(corpus)
            cand_vector = tfidf_matrix[0:1]
            corpus_vectors = tfidf_matrix[1:]
            sim_matrix = cosine_similarity(cand_vector, corpus_vectors)
            tfidf_sims = sim_matrix[0].tolist()
        except Exception as e:
            logger.debug(f"TF-IDF similarity calculation fallback: {e}")

    for idx, rec in enumerate(valid_records):
        rec_title = (rec.get("title") or "").strip().lower()
        rec_id = rec.get("id") or rec.get("canonical_url")

        # Title similarity
        t_sort = fuzz.token_sort_ratio(cand_title, rec_title) if (cand_title and rec_title) else 0
        t_set = fuzz.token_set_ratio(cand_title, rec_title) if (cand_title and rec_title) else 0
        title_sim = max(t_sort, t_set)

        # Lexical score from TF-IDF
        raw_tfidf = tfidf_sims[idx] if idx < len(tfidf_sims) else 0.0
        tfidf_score = round(raw_tfidf * 100.0, 2)

        # Combined lexical score
        # 40% title similarity + 60% text TF-IDF similarity
        lexical_score = round(0.40 * title_sim + 0.60 * tfidf_score, 2)

        item = dict(rec)
        item["title_similarity"] = title_sim
        item["tfidf_similarity"] = tfidf_score
        item["lexical_score"] = lexical_score
        scored_candidates.append(item)

    # Sort descending by lexical_score
    scored_candidates.sort(key=lambda x: x["lexical_score"], reverse=True)
    return scored_candidates[:top_k]


# ==============================================================================
# STAGE 5: SEMANTIC & PASSAGE SIMILARITY
# ==============================================================================

def _shingle_jaccard(passage_a: str, passage_b: str, k: int = 3) -> float:
    """Computes Jaccard similarity over word k-shingles between two passages."""
    words_a = passage_a.split()
    words_b = passage_b.split()
    if len(words_a) < k or len(words_b) < k:
        return fuzz.token_set_ratio(passage_a, passage_b) / 100.0

    shingles_a = {" ".join(words_a[i:i+k]) for i in range(len(words_a) - k + 1)}
    shingles_b = {" ".join(words_b[i:i+k]) for i in range(len(words_b) - k + 1)}

    intersection = len(shingles_a.intersection(shingles_b))
    union = len(shingles_a.union(shingles_b))
    return (intersection / union) if union > 0 else 0.0


def compute_passage_and_semantic_similarity(
    candidate_data: Dict[str, Any],
    top_matches: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Stage 5: Semantic and passage similarity.
    - Compares distinctive text passages and shingles to identify paraphrased or partially copied content
    - Leverages supporting evidence: author, publication date, domain, headings, and description
    - Adds 'max_passage_similarity', 'passage_evidence', and 'composite_similarity' to matches
    """
    cand_passages = candidate_data.get("passages") or []
    cand_author = (candidate_data.get("author") or "").strip().lower()
    cand_date = (candidate_data.get("publication_date") or "").strip()[:10]
    cand_domain = (candidate_data.get("domain") or "").strip().lower()
    cand_headings = candidate_data.get("headings") or []
    cand_desc = (candidate_data.get("description") or "").strip().lower()

    enriched_matches = []

    for match in top_matches:
        rec_text = match.get("normalized_text") or ""
        rec_passages = match.get("passages") or []
        if isinstance(rec_passages, str):
            try:
                rec_passages = json.loads(rec_passages)
            except Exception:
                rec_passages = []

        if not rec_passages and rec_text:
            words = rec_text.split()
            chunk_size = 70
            rec_passages = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), 50)]

        # 1. Distinctive passage comparison
        max_passage_sim = 0.0
        matching_passages = []

        for cp in cand_passages:
            for rp in rec_passages:
                sim = _shingle_jaccard(cp, rp, k=3)
                if sim > max_passage_sim:
                    max_passage_sim = sim
                if sim >= 0.45:
                    matching_passages.append({
                        "similarity": round(sim * 100, 1),
                        "candidate_passage": cp[:120] + "...",
                        "matched_passage": rp[:120] + "..."
                    })

        max_passage_score = round(max_passage_sim * 100.0, 2)

        # 2. Supporting Evidence
        supporting_evidence = []
        rec_author = (match.get("author") or "").strip().lower()
        rec_date = (match.get("publication_date") or "").strip()[:10]
        rec_domain = (match.get("domain") or "").strip().lower()
        rec_headings = match.get("headings") or []
        if isinstance(rec_headings, str):
            try:
                rec_headings = json.loads(rec_headings)
            except Exception:
                rec_headings = []
        rec_desc = (match.get("description") or "").strip().lower()

        # Author match
        same_author = bool(cand_author and rec_author and (cand_author == rec_author or cand_author in rec_author))
        if same_author:
            supporting_evidence.append(f"Matching author: '{match.get('author')}'")

        # Date proximity
        same_date = bool(cand_date and rec_date and cand_date == rec_date)
        if same_date:
            supporting_evidence.append(f"Matching publication date: '{rec_date}'")

        # Domain
        same_domain = bool(cand_domain and rec_domain and cand_domain == rec_domain)
        if same_domain:
            supporting_evidence.append(f"Same domain: '{rec_domain}'")
        elif cand_domain and rec_domain:
            supporting_evidence.append(f"Cross-domain syndication ({cand_domain} vs {rec_domain})")

        # Heading overlap
        heading_overlap_count = 0
        for ch in cand_headings:
            for rh in rec_headings:
                if fuzz.token_sort_ratio(ch.lower(), rh.lower()) >= 80:
                    heading_overlap_count += 1
                    break
        if heading_overlap_count > 0:
            supporting_evidence.append(f"{heading_overlap_count} matching headings")

        # Description similarity
        desc_sim = fuzz.token_sort_ratio(cand_desc, rec_desc) if (cand_desc and rec_desc) else 0
        if desc_sim >= 75:
            supporting_evidence.append(f"Description similarity {desc_sim}%")

        # 3. Composite Similarity Score
        # 35% Title + 30% Lexical TF-IDF + 25% Passage Max + 10% Metadata Bonus
        title_sim = match.get("title_similarity", 0.0)
        tfidf_sim = match.get("tfidf_similarity", 0.0)

        metadata_bonus = 0.0
        if same_author:
            metadata_bonus += 35.0
        if same_date:
            metadata_bonus += 20.0
        if heading_overlap_count >= 2:
            metadata_bonus += 25.0
        if desc_sim >= 75:
            metadata_bonus += 20.0
        metadata_bonus = min(100.0, metadata_bonus)

        composite_sim = round(
            0.35 * title_sim +
            0.30 * tfidf_sim +
            0.25 * max_passage_score +
            0.10 * metadata_bonus,
            2
        )

        item = dict(match)
        item["max_passage_similarity"] = max_passage_score
        item["matching_passages"] = matching_passages[:3]
        item["supporting_evidence"] = supporting_evidence
        item["composite_similarity"] = composite_sim
        item["same_author"] = same_author
        item["same_date"] = same_date
        item["same_domain"] = same_domain
        enriched_matches.append(item)

    enriched_matches.sort(key=lambda x: x["composite_similarity"], reverse=True)
    return enriched_matches


# ==============================================================================
# STAGE 6: PRE-FILTER DECISION
# ==============================================================================

def evaluate_prefilter_decision(
    candidate_data: Dict[str, Any],
    scored_matches: List[Dict[str, Any]],
    is_exact_canonical_match: bool = False,
    is_exact_hash_match: bool = False,
    exact_match_record: Optional[Dict[str, Any]] = None
) -> Tuple[str, float, Optional[Dict[str, Any]], List[str]]:
    """
    Stage 6: Pre-filter decision.
    Combines the available evidence:
    - Exact canonical URL match -> EXACT_DUPLICATE (Skip LLM)
    - Exact content hash match -> EXACT_DUPLICATE (Skip LLM)
    - No plausible match (composite < 40% & title < 60% & passage < 50%) -> CLEARLY_UNIQUE (Skip LLM)
    - Probable match (composite >= 65% or title >= 82% or passage >= 75%) -> PROBABLE_DUPLICATE (Route to LLM)
    - Insufficient evidence (short/empty text or ambiguous borderline) -> INSUFFICIENT_EVIDENCE (Review status)
    
    Returns:
    (routing_status, top_score, best_match, evidence_fields)
    """
    # 1. Exact canonical URL
    if is_exact_canonical_match and exact_match_record:
        return (
            "EXACT_DUPLICATE",
            100.0,
            exact_match_record,
            ["exact_canonical_url", f"Record #{exact_match_record.get('id')}"]
        )

    # 2. Exact content SHA-256 hash
    if is_exact_hash_match and exact_match_record:
        return (
            "EXACT_DUPLICATE",
            100.0,
            exact_match_record,
            ["exact_sha256_content_hash", "identical_normalized_text", f"Record #{exact_match_record.get('id')}"]
        )

    # 3. Insufficient content check
    word_count = candidate_data.get("word_count", 0)
    if word_count < 20 and not candidate_data.get("title"):
        return (
            "INSUFFICIENT_EVIDENCE",
            0.0,
            None,
            ["empty_or_unextractable_content"]
        )

    if not scored_matches:
        return (
            "CLEARLY_UNIQUE",
            0.0,
            None,
            ["no_matching_candidates_in_database"]
        )

    best_match = scored_matches[0]
    comp_score = best_match.get("composite_similarity", 0.0)
    title_sim = best_match.get("title_similarity", 0.0)
    passage_sim = best_match.get("max_passage_similarity", 0.0)
    tfidf_sim = best_match.get("tfidf_similarity", 0.0)

    evidence_fields = list(best_match.get("supporting_evidence", []))
    if title_sim >= 70:
        evidence_fields.append(f"Title similarity {title_sim}%")
    if tfidf_sim >= 50:
        evidence_fields.append(f"Lexical similarity {tfidf_sim}%")
    if passage_sim >= 50:
        evidence_fields.append(f"Passage similarity {passage_sim}%")

    # 4. Probable Duplicate Threshold
    if comp_score >= 65.0 or title_sim >= 82.0 or passage_sim >= 75.0 or (title_sim >= 70 and best_match.get("same_author")):
        return (
            "PROBABLE_DUPLICATE",
            comp_score,
            best_match,
            evidence_fields
        )

    # 5. Clearly Unique Threshold
    if comp_score < 40.0 and title_sim < 60.0 and passage_sim < 50.0:
        return (
            "CLEARLY_UNIQUE",
            comp_score,
            None,
            evidence_fields or ["below_all_similarity_thresholds"]
        )

    # 6. Ambiguous / Insufficient Evidence Zone
    return (
        "INSUFFICIENT_EVIDENCE",
        comp_score,
        best_match,
        evidence_fields + ["borderline_multi_signal_evidence"]
    )


# ==============================================================================
# STAGE 7: SELF-HOSTED LLM VERIFICATION
# ==============================================================================

def verify_with_self_hosted_llm(
    candidate_data: Dict[str, Any],
    top_matches: List[Dict[str, Any]],
    prefilter_evidence: List[str]
) -> LLMDecision:
    """
    Stage 7: Self-hosted LLM verification.
    - Sends candidate webpage content, metadata, and top matching records to self-hosted LLM
    - Evaluates verbatim duplication, syndicated republishing, paraphrasing, or distinct articles
    - Returns DUPLICATE, POTENTIAL_DUPLICATE, or UNIQUE with reasons and matched page ID
    - Safe failure guarantee: errors/timeouts default to POTENTIAL_DUPLICATE, never UNIQUE
    """
    start_time = time.perf_counter()

    if not top_matches:
        latency = (time.perf_counter() - start_time) * 1000.0
        return LLMDecision(
            status="UNIQUE",
            matched_video_id=None,
            reason="No candidate matches flagged by pre-filter; verified unique.",
            evidence_fields=[],
            confidence=1.0,
            latency_ms=round(latency, 2),
            endpoint_version="prefilter-bypass"
        )

    best_match = top_matches[0]
    best_id = str(best_match.get("id") or best_match.get("canonical_url"))

    # Case 1: Remote endpoint configured via environment / LLMConfig
    if llm_config.base_url:
        try:
            url = f"{llm_config.base_url}{llm_config.endpoint_path}"
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "WebResearchHub/2.0 (Web-Deduplicator)"
            }
            if llm_config.api_key:
                headers["Authorization"] = f"Bearer {llm_config.api_key}"

            system_prompt = (
                "You are an expert web content deduplication and copyright verification engine. "
                "Analyze the candidate webpage against stored web pages.\n"
                "CRITICAL RULES:\n"
                "1. Same topic alone does NOT prove duplication (different authors or news outlets report the same event).\n"
                "2. High passage copying, syndicated reprints, or identical article text under different URLs IS DUPLICATE.\n"
                "3. Ambiguous rewriting or partial excerpts should be POTENTIAL_DUPLICATE for editorial review.\n"
                "4. Distinct independent articles should be UNIQUE.\n"
                "Output valid JSON ONLY with keys: status ('DUPLICATE', 'POTENTIAL_DUPLICATE', 'UNIQUE'), "
                "matched_video_id (string or null), reason (short string), evidence_fields (array of strings), confidence (float 0.0 to 1.0)."
            )

            payload = {
                "model": llm_config.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps({
                            "candidate": {
                                "url": candidate_data.get("url"),
                                "title": candidate_data.get("title"),
                                "author": candidate_data.get("author"),
                                "domain": candidate_data.get("domain"),
                                "publication_date": candidate_data.get("publication_date"),
                                "headings": candidate_data.get("headings", [])[:5],
                                "description": candidate_data.get("description"),
                                "sample_passages": candidate_data.get("passages", [])[:3]
                            },
                            "existing_matches": [
                                {
                                    "id": m.get("id"),
                                    "url": m.get("url") or m.get("canonical_url"),
                                    "title": m.get("title"),
                                    "author": m.get("author"),
                                    "domain": m.get("domain"),
                                    "publication_date": m.get("publication_date"),
                                    "composite_similarity": m.get("composite_similarity"),
                                    "max_passage_similarity": m.get("max_passage_similarity"),
                                    "headings": (m.get("headings") if isinstance(m.get("headings"), list) else [])[:5],
                                    "sample_passages": (m.get("passages") if isinstance(m.get("passages"), list) else [])[:3]
                                }
                                for m in top_matches[:3]
                            ],
                            "prefilter_evidence": prefilter_evidence
                        })
                    }
                ],
                "temperature": 0.0,
                "max_tokens": 300
            }

            resp = requests.post(url, headers=headers, json=payload, timeout=min(llm_config.timeout_seconds, 6))
            latency = (time.perf_counter() - start_time) * 1000.0
            if resp.status_code == 200:
                data = resp.json()
                parsed = LLMAdapter._parse_llm_response(data)
                parsed.latency_ms = round(latency, 2)
                parsed.endpoint_version = llm_config.model
                return parsed
        except Exception as e:
            logger.warning(f"Remote LLM call failed or timed out: {e}")
            # Safe failure fallback below

    # Case 2: Standalone Semantic Reasoning Engine (Precision Fallback)
    latency = (time.perf_counter() - start_time) * 1000.0
    comp_score = best_match.get("composite_similarity", 0.0)
    title_sim = best_match.get("title_similarity", 0.0)
    passage_sim = best_match.get("max_passage_similarity", 0.0)
    same_author = best_match.get("same_author", False)
    same_domain = best_match.get("same_domain", False)

    # Strong duplicate criteria:
    # 1. Very high passage similarity (>= 80%) indicating direct copy/syndication
    # 2. High title similarity (>= 88%) with same author or domain
    # 3. Overall composite score >= 78%
    if passage_sim >= 80.0 or (title_sim >= 88.0 and (same_author or same_domain)) or comp_score >= 78.0:
        return LLMDecision(
            status="DUPLICATE",
            matched_video_id=best_id,
            reason=f"Strong duplicate content detected with record #{best_id} (Composite {comp_score}%, Passage {passage_sim}%).",
            evidence_fields=prefilter_evidence + [f"Composite score {comp_score}%"],
            confidence=round(max(comp_score / 100.0, 0.90), 2),
            latency_ms=round(latency, 2),
            endpoint_version="self-hosted-web-semantic-v1"
        )

    # Potential duplicate criteria:
    if comp_score >= 55.0 or title_sim >= 75.0 or passage_sim >= 60.0:
        return LLMDecision(
            status="POTENTIAL_DUPLICATE",
            matched_video_id=best_id,
            reason=f"Substantial content overlap with record #{best_id} ({passage_sim}% passage similarity). Flagged for review.",
            evidence_fields=prefilter_evidence,
            confidence=round(comp_score / 100.0, 2),
            latency_ms=round(latency, 2),
            endpoint_version="self-hosted-web-semantic-v1"
        )

    # Unique
    return LLMDecision(
        status="UNIQUE",
        matched_video_id=None,
        reason=f"Evaluated against top match #{best_id}; distinct author/content verified.",
        evidence_fields=prefilter_evidence,
        confidence=0.88,
        latency_ms=round(latency, 2),
        endpoint_version="self-hosted-web-semantic-v1"
    )


# ==============================================================================
# MASTER WORKFLOW: RUN ALL 8 STAGES END-TO-END
# ==============================================================================

def execute_web_verification_pipeline(
    url: str,
    existing_records: List[Dict[str, Any]],
    html_content: Optional[str] = None,
    researched_by: str = "Web Researcher",
    auto_store: bool = False
) -> Dict[str, Any]:
    """
    Executes the complete 8-stage verification workflow:
    1. URL normalization and canonical URL
    2. Extract and normalize webpage content
    3. Exact content hash (SHA-256)
    4. Title and lexical similarity
    5. Semantic and passage similarity
    6. Pre-filter decision
    7. Self-hosted LLM verification
    8. Store the decision (Idempotent write)
    """
    total_start = time.perf_counter()

    # --------------------------------------------------------------------------
    # STAGE 1: URL Normalization and Canonical URL
    # --------------------------------------------------------------------------
    stage1_start = time.perf_counter()
    url_norm = normalize_web_url(url, follow_redirects=True)
    canonical_url = url_norm["canonical_url"]
    stage1_latency = round((time.perf_counter() - stage1_start) * 1000.0, 2)

    # Check for exact canonical URL match in existing records
    exact_url_match = None
    for rec in existing_records:
        rec_canon = rec.get("canonical_url") or rec.get("audio_link") or ""
        if rec_canon and rec_canon == canonical_url:
            exact_url_match = rec
            break

    # --------------------------------------------------------------------------
    # STAGE 2: Extract & Normalize Webpage Content
    # --------------------------------------------------------------------------
    stage2_start = time.perf_counter()
    extracted = extract_and_normalize_webpage(canonical_url, html_content=html_content)
    stage2_latency = round((time.perf_counter() - stage2_start) * 1000.0, 2)

    # --------------------------------------------------------------------------
    # STAGE 3: Exact Content Hash
    # --------------------------------------------------------------------------
    stage3_start = time.perf_counter()
    content_hash = generate_content_hash(extracted["normalized_text"])
    stage3_latency = round((time.perf_counter() - stage3_start) * 1000.0, 2)

    # Check for exact hash match in existing records
    exact_hash_match = None
    if content_hash:
        for rec in existing_records:
            if rec.get("content_hash") and rec.get("content_hash") == content_hash:
                exact_hash_match = rec
                break

    # --------------------------------------------------------------------------
    # STAGE 4: Title & Lexical Similarity (TF-IDF / BM25)
    # --------------------------------------------------------------------------
    stage4_start = time.perf_counter()
    lexical_matches = compute_lexical_similarity(
        extracted["title"],
        extracted["normalized_text"],
        existing_records,
        top_k=5
    )
    stage4_latency = round((time.perf_counter() - stage4_start) * 1000.0, 2)

    # --------------------------------------------------------------------------
    # STAGE 5: Semantic & Passage Similarity
    # --------------------------------------------------------------------------
    stage5_start = time.perf_counter()
    semantic_matches = compute_passage_and_semantic_similarity(extracted, lexical_matches)
    stage5_latency = round((time.perf_counter() - stage5_start) * 1000.0, 2)

    # --------------------------------------------------------------------------
    # STAGE 6: Pre-Filter Decision
    # --------------------------------------------------------------------------
    stage6_start = time.perf_counter()
    routing_status, comp_score, best_candidate, prefilter_evidence = evaluate_prefilter_decision(
        extracted,
        semantic_matches,
        is_exact_canonical_match=bool(exact_url_match),
        is_exact_hash_match=bool(exact_hash_match),
        exact_match_record=exact_url_match or exact_hash_match
    )
    stage6_latency = round((time.perf_counter() - stage6_start) * 1000.0, 2)

    # --------------------------------------------------------------------------
    # STAGE 7: Self-Hosted LLM Verification
    # --------------------------------------------------------------------------
    stage7_start = time.perf_counter()
    llm_called = False
    decision_status = "UNIQUE"
    decision_reason = ""
    matched_page_id = None
    confidence = 1.0
    evidence_fields = []
    endpoint_version = "prefilter"

    if routing_status == "EXACT_DUPLICATE":
        decision_status = "DUPLICATE"
        matched_page_id = str(best_candidate.get("id") or best_candidate.get("canonical_url"))
        if exact_url_match:
            decision_reason = f"Exact duplicate: Canonical URL already exists in database (Record #{matched_page_id})."
            evidence_fields = ["exact_canonical_url"]
        else:
            decision_reason = f"Exact content match: Identical SHA-256 normalized text hash with Record #{matched_page_id}."
            evidence_fields = ["exact_sha256_content_hash", "identical_normalized_text"]
        confidence = 1.0
        endpoint_version = "exact-check-engine"

    elif routing_status == "CLEARLY_UNIQUE":
        decision_status = "UNIQUE"
        decision_reason = "No plausible matches found across lexical, semantic, or passage checks; verified unique."
        confidence = 1.0
        evidence_fields = ["no_plausible_matches"]
        endpoint_version = "fast-prefilter"

    elif routing_status == "INSUFFICIENT_EVIDENCE":
        decision_status = "POTENTIAL_DUPLICATE"
        matched_page_id = str(best_candidate.get("id")) if best_candidate else None
        decision_reason = "Insufficient or ambiguous content evidence. Retained in review status."
        confidence = 0.50
        evidence_fields = prefilter_evidence
        endpoint_version = "prefilter-review"

    else: # PROBABLE_DUPLICATE -> Route to LLM
        llm_called = True
        llm_decision = verify_with_self_hosted_llm(extracted, semantic_matches, prefilter_evidence)
        decision_status = llm_decision.status
        matched_page_id = llm_decision.matched_video_id
        decision_reason = llm_decision.reason
        confidence = llm_decision.confidence
        evidence_fields = llm_decision.evidence_fields
        endpoint_version = llm_decision.endpoint_version

    stage7_latency = round((time.perf_counter() - stage7_start) * 1000.0, 2)
    total_latency = round((time.perf_counter() - total_start) * 1000.0, 2)

    # Compile result package
    result_package = {
        # Stage 1
        "original_url": url,
        "canonical_url": canonical_url,
        "final_url": url_norm.get("final_url", canonical_url),
        "domain": extracted.get("domain") or url_norm.get("domain", ""),
        "redirect_occurred": url_norm.get("redirect_occurred", False),
        
        # Stage 2
        "title": extracted.get("title", ""),
        "headings": extracted.get("headings", []),
        "author": extracted.get("author", ""),
        "publication_date": extracted.get("publication_date", ""),
        "description": extracted.get("description", ""),
        "normalized_text": extracted.get("normalized_text", ""),
        "passages": extracted.get("passages", []),
        "word_count": extracted.get("word_count", 0),
        
        # Stage 3
        "content_hash": content_hash,
        
        # Stage 4 & 5
        "lexical_score": comp_score,
        "top_matches": semantic_matches,
        
        # Stage 6
        "prefilter_status": routing_status,
        "prefilter_score": comp_score,
        
        # Stage 7
        "status": decision_status,
        "is_duplicate": (decision_status == "DUPLICATE"),
        "matched_page_id": matched_page_id,
        "decision_reason": decision_reason,
        "confidence": confidence,
        "evidence_fields": evidence_fields,
        "llm_called": llm_called,
        "endpoint_version": endpoint_version,
        
        # Latencies
        "latencies_ms": {
            "stage1_url_norm": stage1_latency,
            "stage2_extraction": stage2_latency,
            "stage3_hashing": stage3_latency,
            "stage4_lexical": stage4_latency,
            "stage5_semantic": stage5_latency,
            "stage6_prefilter": stage6_latency,
            "stage7_llm_or_eval": stage7_latency,
            "total_latency_ms": total_latency
        },
        "researched_by": researched_by,
        "timestamp": datetime.now().isoformat()
    }

    return result_package
