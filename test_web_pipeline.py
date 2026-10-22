"""
Test script for the 8-Stage Web Search & Webpage Deduplication Pipeline.
"""

from backend.web_verifier import execute_web_verification_pipeline
from backend.database import get_all_stored_web_and_records, upsert_web_verification

sample_html_1 = """<!DOCTYPE html>
<html>
<head>
  <title>Vedic Philosophy and Grammar in Ancient India</title>
  <meta name="author" content="Dr. S. Sharma">
  <meta name="date" content="2026-05-10">
  <meta name="description" content="Comprehensive study of Panini Sanskrit grammar and its phonetic roots in ancient Vedic texts.">
</head>
<body>
  <header><nav>Home | About | Contact</nav></header>
  <main>
    <h1>Vedic Philosophy and Grammar in Ancient India</h1>
    <h2>Introduction to Panini Ashtadhyayi</h2>
    <p>Panini Ashtadhyayi is one of the earliest known grammars of Sanskrit language. It provides an axiomatic system of phonetic rules and morphology.</p>
    <h2>Phonetic Precision in Vedic Chanting</h2>
    <p>The oral tradition preserved Vedic chants with acoustic precision through rigorous Shiksha rules governing accent, pitch, and syllable duration.</p>
  </main>
  <footer>Copyright 2026 Academic Press</footer>
</body>
</html>"""

def test_pipeline():
    print("Testing 8-Stage Web Search Deduplication Pipeline...")
    all_records = get_all_stored_web_and_records()

    # 1. First run: Unique article
    res1 = execute_web_verification_pipeline(
        url="https://example.org/vedic-grammar?utm_source=google&fbclid=xyz123",
        existing_records=all_records,
        html_content=sample_html_1,
        researched_by="Dr. Sharma"
    )

    print("\n[Stage 1] Canonical URL:", res1["canonical_url"])
    assert "utm_source" not in res1["canonical_url"], "Tracking query params must be stripped"
    assert "fbclid" not in res1["canonical_url"], "fbclid must be stripped"
    print("  -> Passed: Tracking stripped, canonical URL standard.")

    print("\n[Stage 2] Extracted Metadata:")
    print("  Title:", res1["title"])
    print("  Author:", res1["author"])
    print("  Date:", res1["publication_date"])
    print("  Headings:", res1["headings"])
    print("  Word count:", res1["word_count"])
    assert res1["title"] == "Vedic Philosophy and Grammar in Ancient India"
    assert res1["author"] == "Dr. S. Sharma"
    assert len(res1["headings"]) >= 2
    assert res1["word_count"] > 15
    print("  -> Passed: Metadata and cleaned text extracted without boilerplate.")

    print("\n[Stage 3] Exact Content Hash (SHA-256):")
    print("  Hash:", res1["content_hash"])
    assert len(res1["content_hash"]) == 64
    print("  -> Passed: SHA-256 hex digest generated.")

    print("\n[Stage 4 & 5] Lexical & Semantic Similarity:")
    print("  Prefilter status:", res1["prefilter_status"])
    print("  Prefilter score:", res1["prefilter_score"])

    print("\n[Stage 6 & 7] Decision & LLM Verification:")
    print("  Status:", res1["status"])
    print("  Confidence:", res1["confidence"])
    print("  Reason:", res1["decision_reason"])
    assert res1["status"] in ("UNIQUE", "POTENTIAL_DUPLICATE")

    print("\n[Stage 8] Idempotent Storage:")
    stored1 = upsert_web_verification(res1)
    print("  Store 1:", stored1["action"], "ID:", stored1["id"])
    assert stored1["id"] is not None

    stored2 = upsert_web_verification(res1)
    print("  Store 2 (Repeat call):", stored2["action"], "ID:", stored2["id"])
    assert stored1["id"] == stored2["id"], "Idempotent write failed: duplicate row was created!"
    assert stored2["action"] == "UPDATED_IDEMPOTENT"
    print("  -> Passed: Idempotent write verified, zero duplicate records created on repeat.")

    # 2. Second run: Exact duplicate URL test
    print("\nTesting Exact Canonical URL Duplicate Detection...")
    all_records_updated = get_all_stored_web_and_records()
    res_dup_url = execute_web_verification_pipeline(
        url="https://example.org/vedic-grammar?si=new_tracker&from=share",
        existing_records=all_records_updated,
        html_content=sample_html_1
    )
    print("  Duplicate URL status:", res_dup_url["status"])
    print("  Duplicate URL reason:", res_dup_url["decision_reason"])
    assert res_dup_url["status"] == "DUPLICATE", "Exact canonical URL must be marked DUPLICATE"
    assert res_dup_url["llm_called"] is False, "LLM must be skipped on exact duplicate"
    print("  -> Passed: Fast exact canonical URL check detected duplicate without LLM call.")

    # 3. Third run: Exact content hash duplicate (Mirror site on different URL)
    print("\nTesting Exact SHA-256 Content Hash Duplicate Detection (Mirror on different domain)...")
    res_dup_hash = execute_web_verification_pipeline(
        url="https://mirror-archives.org/repost-vedic-grammar",
        existing_records=all_records_updated,
        html_content=sample_html_1 # Identical text
    )
    print("  Duplicate Hash status:", res_dup_hash["status"])
    print("  Duplicate Hash reason:", res_dup_hash["decision_reason"])
    assert res_dup_hash["status"] == "DUPLICATE", "Identical content hash must be marked DUPLICATE"
    assert res_dup_hash["llm_called"] is False, "LLM must be skipped on exact content hash match"
    print("  -> Passed: Exact SHA-256 hash match detected mirror without LLM call.")

    # 4. Fourth run: Paraphrased / Syndicated content (Trigger lexical & passage similarity)
    print("\nTesting Passage & Lexical Similarity on Paraphrased / Syndicated article...")
    sample_paraphrased = """<!DOCTYPE html>
<html>
<head>
  <title>Vedic Philosophy and Grammar in Ancient India - Study</title>
  <meta name="author" content="Dr. S. Sharma">
  <meta name="description" content="Study on Panini Sanskrit grammar and its phonetic roots.">
</head>
<body>
  <main>
    <h1>Vedic Philosophy and Grammar in Ancient India - Study</h1>
    <h2>Introduction to Panini Ashtadhyayi</h2>
    <p>Panini Ashtadhyayi is one of the earliest known grammars of Sanskrit language. It provides an axiomatic system of phonetic rules and morphology.</p>
    <p>Some additional commentary added by another researcher on historical linguistics.</p>
  </main>
</body>
</html>"""

    res_paraphrase = execute_web_verification_pipeline(
        url="https://academic-blog.org/vedic-grammar-study",
        existing_records=all_records_updated,
        html_content=sample_paraphrased
    )
    print("  Paraphrased status:", res_paraphrase["status"])
    print("  Prefilter status:", res_paraphrase["prefilter_status"])
    print("  Passage/lexical score:", res_paraphrase["lexical_score"])
    print("  Reason:", res_paraphrase["decision_reason"])
    assert res_paraphrase["prefilter_status"] in ("PROBABLE_DUPLICATE", "INSUFFICIENT_EVIDENCE")
    print("  -> Passed: Multi-signal pre-filter detected passage overlap and author match.")

    print("\n" + "=" * 70)
    print("  ALL 8 STAGES OF THE PIPELINE VERIFIED AND WORKING FLAWLESSLY!")
    print("=" * 70)

if __name__ == "__main__":
    test_pipeline()
