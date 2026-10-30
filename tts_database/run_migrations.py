
import hashlib
from pathlib import Path

from connection import get_connection
from contextlib import contextmanager


@contextmanager
def _transaction(conn):
    """Transaction context manager compatible with both psycopg v3 and psycopg2."""
    if hasattr(conn, "transaction"):
        with _transaction(conn):
            yield
    else:
        with conn:
            yield


PROJECT_ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
MIGRATION_LOCK_KEY = 741852963


def calculate_checksum(sql_text):
    """Return the SHA-256 checksum of a migration file's contents."""
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


def ensure_migration_table(conn):
    """Ensure the migration tracking table has the required columns."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(100) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                checksum TEXT
            );
        """)
        cur.execute("""
            ALTER TABLE schema_migrations
            ADD COLUMN IF NOT EXISTS checksum TEXT;
        """)


def get_migration_history(conn):
    """Return migration versions and their stored checksums."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT version, checksum
            FROM schema_migrations;
        """)
        return dict(cur.fetchall())


def acquire_migration_lock(conn):
    """Prevent concurrent instances of this runner from applying migrations."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_lock(%s);",
            (MIGRATION_LOCK_KEY,),
        )


def release_migration_lock(conn):
    """Release the advisory lock."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(%s);",
            (MIGRATION_LOCK_KEY,),
        )
        result = cur.fetchone()

        if not result or not result[0]:
            raise RuntimeError("Migration lock was not held by this connection.")


def run_migrations():
    """Validate recorded migrations and apply pending migrations."""

    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(
            f"Migrations directory not found: {MIGRATIONS_DIR}"
        )

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    if not migration_files:
        print("No migration files found.")
        return

    with get_connection() as conn:
        acquire_migration_lock(conn)

        try:
            with _transaction(conn):
                ensure_migration_table(conn)

            history = get_migration_history(conn)

            # A NULL checksum means the migration predates checksum tracking.
            # Baseline its current file contents without executing its SQL.
            for migration_file in migration_files:
                version = migration_file.stem

                if version not in history:
                    continue

                sql_text = migration_file.read_text(encoding="utf-8")
                current_checksum = calculate_checksum(sql_text)
                stored_checksum = history[version]

                if stored_checksum is None:
                    with _transaction(conn):
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE schema_migrations
                                SET checksum = %s
                                WHERE version = %s
                                  AND checksum IS NULL;
                                """,
                                (current_checksum, version),
                            )

                    history[version] = current_checksum
                    print(f"BASELINED: {version} (checksum recorded)")
                elif stored_checksum != current_checksum:
                    raise RuntimeError(
                        f"Checksum mismatch for {version}. "
                        "The migration file changed after being recorded. "
                        "Restore the original file or create a new migration."
                    )

            # Refuse to proceed if a tracked migration file is missing.
            file_versions = {path.stem for path in migration_files}
            missing_files = set(history) - file_versions

            if missing_files:
                missing = ", ".join(sorted(missing_files))
                raise RuntimeError(
                    f"Tracked migration file(s) missing: {missing}"
                )

            for migration_file in migration_files:
                version = migration_file.stem

                if version in history:
                    print(f"SKIPPED: {version} (already applied)")
                    continue

                sql_text = migration_file.read_text(encoding="utf-8")

                if not sql_text.strip():
                    raise ValueError(
                        f"Migration file is empty: {migration_file.name}"
                    )

                checksum = calculate_checksum(sql_text)
                print(f"Applying: {version}")

                # The schema change and history entry commit together.
                with _transaction(conn):
                    with conn.cursor() as cur:
                        cur.execute(sql_text)
                        cur.execute(
                            """
                            INSERT INTO schema_migrations (version, checksum)
                            VALUES (%s, %s);
                            """,
                            (version, checksum),
                        )

                history[version] = checksum
                print(f"APPLIED: {version}")

        finally:
            release_migration_lock(conn)

    print("Migration process completed.")


if __name__ == "__main__":
    try:
        run_migrations()
    except Exception as exc:
        print(f"Migration failed: {exc}")
        raise
