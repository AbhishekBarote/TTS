"""
TTS Database Package - Centralized Data Layer for Audio Research & Revalidation.
Provides versioned schema migrations, asynchronous and synchronous connection pools,
and repository abstractions for the TTS platform.
"""
import sys
from pathlib import Path

_pkg_dir = Path(__file__).resolve().parent

# Ensure local .venv site-packages is discoverable if psycopg is not in host interpreter
_venv_site = _pkg_dir / ".venv" / "Lib" / "site-packages"
if _venv_site.exists() and str(_venv_site) not in sys.path:
    sys.path.append(str(_venv_site))

# Ensure tts_database directory is in sys.path so internal flat imports work in any context
if str(_pkg_dir) not in sys.path:
    sys.path.insert(0, str(_pkg_dir))

from tts_database.config import DATABASE_URL
from tts_database.connection import get_connection
from tts_database.async_connection import get_async_connection, async_check_database
from tts_database.run_migrations import run_migrations

__all__ = [
    "DATABASE_URL",
    "get_connection",
    "get_async_connection",
    "async_check_database",
    "run_migrations",
]
