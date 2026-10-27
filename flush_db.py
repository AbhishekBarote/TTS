from backend.database import get_connection

def flush_database():
    tables_to_flush = [
        "audio_segments",
        "pipeline_jobs",
        "records",
        "audit_logs",
        "decision_audit",
        "speaker_duration_ledger",
        "speaker_ledger",
        "web_verifications",
        "revalidation_queue",
        "duplicate_cache",
        "research_records"
    ]
    
    conn = get_connection()
    cur = conn.cursor()
    
    # Check which tables actually exist
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    existing_tables = set(r[0] for r in cur.fetchall())
    
    valid_tables = [t for t in tables_to_flush if t in existing_tables]
    if valid_tables:
        tables_clause = ", ".join(valid_tables)
        cur.execute(f"TRUNCATE TABLE {tables_clause} RESTART IDENTITY CASCADE;")
        cur.execute("ALTER TABLE records DROP CONSTRAINT IF EXISTS records_canonical_url_key;")
        conn.commit()
        print(f"Successfully truncated tables: {valid_tables}")
    
    # Check row counts after truncation
    for t in valid_tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        cnt = cur.fetchone()[0]
        print(f"{t}: {cnt}")
        
    conn.close()

if __name__ == "__main__":
    flush_database()
