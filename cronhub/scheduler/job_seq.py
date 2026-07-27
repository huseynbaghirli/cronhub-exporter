import sqlite3

from ..core.config import SQLITE_FILE


def _db_conn():
    return sqlite3.connect(SQLITE_FILE, check_same_thread=False)


def init_job_seq_db():
    with _db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cronhub_job_seq (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT
            )
        """)
        conn.commit()


def next_job_seq(job_id: str) -> int:
    """Hands out the next short, human-friendly sequential job number
    (#1, #2, ...) - unique and stable for the lifetime of the job, distinct
    from its internal UUID."""
    init_job_seq_db()
    with _db_conn() as conn:
        cur = conn.execute("INSERT INTO cronhub_job_seq (job_id) VALUES (?)", (job_id,))
        conn.commit()
        return cur.lastrowid
