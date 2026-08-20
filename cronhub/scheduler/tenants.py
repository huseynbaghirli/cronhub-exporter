"""Persistent tenant registry.

Tenants used to exist only implicitly - a tenant was "real" if some job
carried it, or if it happened to be in the current session. That works until a
tenant has no jobs yet, which is exactly what a freshly created GitLab tenant
branch is. This table gives a tenant somewhere to live on its own.
"""

import sqlite3
import time

from ..core.config import SQLITE_FILE


def _db_conn():
    return sqlite3.connect(SQLITE_FILE, check_same_thread=False)


def init_tenants_db():
    with _db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cronhub_tenants (
              name       TEXT PRIMARY KEY,
              created_at REAL,
              source     TEXT
            )
        """)
        conn.commit()


def tenant_register(name: str, source: str = "cronhub") -> bool:
    """Adds a tenant if it isn't known yet. Returns True when it was new."""
    name = (name or "").strip()
    if not name:
        return False
    init_tenants_db()
    with _db_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO cronhub_tenants (name, created_at, source) VALUES (?,?,?)",
            (name, time.time(), source),
        )
        conn.commit()
        return cur.rowcount > 0


def tenant_list() -> list[str]:
    init_tenants_db()
    with _db_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT name FROM cronhub_tenants ORDER BY name"
        )]


def tenant_delete(name: str) -> int:
    name = (name or "").strip()
    if not name:
        return 0
    init_tenants_db()
    with _db_conn() as conn:
        cur = conn.execute("DELETE FROM cronhub_tenants WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount
