"""
Connection provider for TTS Database.
Supports both psycopg (v3, modern primary) and psycopg2 (fallback) for seamless server deployment.
"""
import os

try:
    from config import DATABASE_URL
except ImportError:
    from tts_database.config import DATABASE_URL

try:
    import psycopg
    from psycopg.rows import dict_row
    _USE_PSYCOPG3 = True
except ImportError:
    import psycopg2
    from psycopg2.extras import DictCursor
    _USE_PSYCOPG3 = False


def get_connection(url: str | None = None, pooled: bool | None = None):
    """
    Open and return a PostgreSQL connection.
    Uses modern psycopg (v3) if available, falling back to psycopg2.
    """
    target_url = url or DATABASE_URL
    if _USE_PSYCOPG3:
        return psycopg.connect(target_url)
    return psycopg2.connect(target_url)


if __name__ == "__main__":
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user;")
                database, user = cur.fetchone()
                print("Connection successful!")
                print(f"Driver: {'psycopg v3' if _USE_PSYCOPG3 else 'psycopg2'}")
                print(f"Database: {database}")
                print(f"User: {user}")
    except Exception as exc:
        print(f"Connection failed: {exc}")
