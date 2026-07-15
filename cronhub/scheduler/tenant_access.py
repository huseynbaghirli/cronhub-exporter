import sqlite3
import time

from ..core.config import SQLITE_FILE

VALID_ROLES = {"editor", "ro"}


def _db_conn():
    return sqlite3.connect(SQLITE_FILE, check_same_thread=False)


def init_tenant_access_db():
    with _db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cronhub_tenant_access (
              user_sub TEXT NOT NULL,
              tenant TEXT NOT NULL,
              role TEXT NOT NULL,
              updated_ts REAL,
              PRIMARY KEY (user_sub, tenant)
            )
        """)
        conn.commit()


def tenant_access_upsert(user_sub: str, tenant: str, role: str):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
    init_tenant_access_db()
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO cronhub_tenant_access (user_sub, tenant, role, updated_ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_sub, tenant) DO UPDATE SET
              role=excluded.role,
              updated_ts=excluded.updated_ts
            """,
            (user_sub, tenant, role, time.time()),
        )
        conn.commit()


def tenant_access_delete(user_sub: str, tenant: str):
    init_tenant_access_db()
    with _db_conn() as conn:
        conn.execute(
            "DELETE FROM cronhub_tenant_access WHERE user_sub=? AND tenant=?",
            (user_sub, tenant),
        )
        conn.commit()


def tenant_access_delete_tenant(tenant: str):
    """Removes every grant for a tenant (regardless of user) - used when the
    tenant itself is deleted."""
    init_tenant_access_db()
    with _db_conn() as conn:
        conn.execute("DELETE FROM cronhub_tenant_access WHERE tenant=?", (tenant,))
        conn.commit()


def tenant_access_list_for_user(user_sub: str) -> dict[str, str]:
    init_tenant_access_db()
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT tenant, role FROM cronhub_tenant_access WHERE user_sub=?",
            (user_sub,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def tenant_access_list_all() -> list[dict]:
    init_tenant_access_db()
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT user_sub, tenant, role, updated_ts FROM cronhub_tenant_access ORDER BY user_sub, tenant"
        ).fetchall()
    return [
        {"user_sub": r[0], "tenant": r[1], "role": r[2], "updated_ts": r[3]}
        for r in rows
    ]
