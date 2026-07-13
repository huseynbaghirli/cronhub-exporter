import json
import sqlite3
import time

from ..core.config import SQLITE_FILE

def _db_conn():
    return sqlite3.connect(SQLITE_FILE, check_same_thread=False)

def init_users_db():
    with _db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cronhub_users (
              sub TEXT PRIMARY KEY,
              user_type TEXT,
              preferred_username TEXT,
              email TEXT,
              display_name TEXT,
              groups_json TEXT,
              role TEXT,
              allowed_tenants_json TEXT,
              created_ts REAL,
              last_login_ts REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_last_login ON cronhub_users(last_login_ts)")
        conn.commit()

def users_upsert(
    *,
    sub: str,
    user_type: str,
    preferred_username: str | None,
    email: str | None,
    display_name: str | None,
    groups: list[str] | None,
    role: str | None,
    allowed_tenants: list[str] | None,
):
    init_users_db()
    now = time.time()
    g = json.dumps(groups or [], ensure_ascii=False)
    t = json.dumps(allowed_tenants or [], ensure_ascii=False)

    with _db_conn() as conn:
        # created_ts: ilk dəfə insert olanda qoyulsun
        conn.execute(
            """
            INSERT INTO cronhub_users
              (sub, user_type, preferred_username, email, display_name, groups_json, role, allowed_tenants_json, created_ts, last_login_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sub) DO UPDATE SET
              user_type=excluded.user_type,
              preferred_username=excluded.preferred_username,
              email=excluded.email,
              display_name=excluded.display_name,
              groups_json=excluded.groups_json,
              role=excluded.role,
              allowed_tenants_json=excluded.allowed_tenants_json,
              last_login_ts=excluded.last_login_ts
            """,
            (
                sub,
                user_type or "",
                preferred_username or "",
                email or "",
                display_name or "",
                g,
                role or "",
                t,
                now,
                now,
            ),
        )
        # created_ts overwrite olmasın deyə:
        conn.execute(
            "UPDATE cronhub_users SET created_ts=COALESCE(created_ts, ?) WHERE sub=?",
            (now, sub),
        )
        conn.commit()

def users_list(limit: int = 200):
    init_users_db()
    limit = max(1, min(int(limit), 2000))
    with _db_conn() as conn:
        rows = conn.execute(
            """
            SELECT sub, user_type, preferred_username, email, display_name, role, groups_json, allowed_tenants_json, created_ts, last_login_ts
            FROM cronhub_users
            ORDER BY last_login_ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    out = []
    for r in rows:
        out.append({
            "sub": r[0],
            "type": r[1],
            "preferred_username": r[2],
            "email": r[3],
            "display_name": r[4],
            "role": r[5],
            "groups": json.loads(r[6] or "[]"),
            "allowed_tenants": json.loads(r[7] or "[]"),
            "created_ts": r[8],
            "last_login_ts": r[9],
        })
    return out
