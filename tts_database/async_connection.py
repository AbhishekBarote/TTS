"""
Asynchronous Database Connection Manager for TTS Database.
Uses psycopg v3 AsyncConnection for high-throughput concurrent I/O.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure local .venv site-packages is discoverable if psycopg is not in host interpreter
_pkg_dir = Path(__file__).resolve().parent
_venv_site = _pkg_dir / ".venv" / "Lib" / "site-packages"
if _venv_site.exists() and str(_venv_site) not in sys.path:
    sys.path.append(str(_venv_site))

load_dotenv()

try:
    from config import DATABASE_URL
except ImportError:
    try:
        from tts_database.config import DATABASE_URL
    except ImportError:
        DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tts_app:ttsdata@localhost:5432/tts_research")

try:
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
except ImportError:
    AsyncConnection = None
    dict_row = None


def _get_database_url() -> str:
    url = os.getenv("DATABASE_URL", DATABASE_URL)
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    return url


async def get_async_connection():
    """Open and return an async psycopg connection with dict_row factory."""
    if AsyncConnection is None:
        raise RuntimeError(
            "psycopg (v3) is required for asynchronous operations. "
            "Please install it with: pip install 'psycopg[binary]>=3.1.0'"
        )
    return await AsyncConnection.connect(
        _get_database_url(),
        row_factory=dict_row,
    )


async def async_check_database() -> bool:
    """Return True when the PostgreSQL server is reachable."""
    async with await get_async_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 AS ok")
            row = await cur.fetchone()
            return row["ok"] == 1

