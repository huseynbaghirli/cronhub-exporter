# cronhub/scheduler/history.py

import sqlite3
import json

SQLITE_FILE = "data/jobs.sqlite"
DEFAULT_TENANT = "business"

def _db_conn():
    return sqlite3.connect(SQLITE_FILE, check_same_thread=False)

def _migrate_add_tenant_column():
    # history tenant column
    with _db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("ALTER TABLE cronhub_history ADD COLUMN tenant TEXT DEFAULT 'business'")
        except Exception:
            pass
        try:
            c.execute("UPDATE cronhub_history SET tenant='business' WHERE tenant IS NULL OR tenant=''")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_history_tenant_job_ts ON cronhub_history(tenant, job_id, ts)")
        except Exception:
            pass
        conn.commit()

    # last_results tenant column
    with _db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("ALTER TABLE cronhub_last_results ADD COLUMN tenant TEXT DEFAULT 'business'")
        except Exception:
            pass
        try:
            c.execute("UPDATE cronhub_last_results SET tenant='business' WHERE tenant IS NULL OR tenant=''")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_last_results_tenant_job ON cronhub_last_results(tenant, job_id)")
        except Exception:
            pass
        conn.commit()


def _init_last_results():
    with _db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS cronhub_last_results (
              job_id TEXT PRIMARY KEY,
              ts REAL,
              value REAL,
              metrics_json TEXT,
              error TEXT,
              duration REAL,
              output_preview TEXT,
              tenant TEXT DEFAULT 'business'
            )
        """)
        conn.commit()

def _init_db():
    with _db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS cronhub_history (
              job_id TEXT NOT NULL,
              ts REAL NOT NULL,
              value REAL NOT NULL,
              output TEXT,
              tenant TEXT DEFAULT 'business'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_history_job_ts ON cronhub_history(job_id, ts)")
        conn.commit()

    _init_last_results()
    _migrate_add_tenant_column()


def history_insert(tenant: str, job_id: str, ts: float, value: float, output: str | None):
    tenant = (tenant or DEFAULT_TENANT).strip()
    with _db_conn() as conn:
        conn.execute(
            "INSERT INTO cronhub_history(tenant, job_id, ts, value, output) VALUES (?, ?, ?, ?, ?)",
            (tenant, job_id, ts, float(value), (output or "")[:8192]),
        )
        conn.commit()

def history_prune(tenant: str, job_id: str, cutoff_ts: float):
    tenant = (tenant or DEFAULT_TENANT).strip()
    with _db_conn() as conn:
        conn.execute(
            "DELETE FROM cronhub_history WHERE tenant=? AND job_id=? AND ts<?",
            (tenant, job_id, cutoff_ts),
        )
        conn.commit()

def history_select(tenant: str, job_id: str, since_ts: float | None = None, limit: int = 500):
    tenant = (tenant or DEFAULT_TENANT).strip()
    q = "SELECT ts, value, output FROM cronhub_history WHERE tenant=? AND job_id=?"
    args = [tenant, job_id]
    if since_ts is not None:
        q += " AND ts>=?"
        args.append(since_ts)
    q += " ORDER BY ts ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with _db_conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [{"ts": r[0], "value": r[1], "output": r[2]} for r in rows]


def last_results_upsert(
    tenant: str,
    job_id: str,
    ts: float | None,
    value: float | None,
    metrics: dict | None,
    error: str | None,
    duration: float | None,
    output_preview: str | None,
):
    tenant = (tenant or DEFAULT_TENANT).strip()
    try:
        metrics_json = json.dumps(metrics or {}, ensure_ascii=False)
    except Exception:
        metrics_json = "{}"

    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO cronhub_last_results(job_id, ts, value, metrics_json, error, duration, output_preview, tenant)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              ts=excluded.ts,
              value=excluded.value,
              metrics_json=excluded.metrics_json,
              error=excluded.error,
              duration=excluded.duration,
              output_preview=excluded.output_preview,
              tenant=excluded.tenant
            """,
            (
                job_id,
                ts,
                float(value) if value is not None else None,
                metrics_json,
                (error or ""),
                float(duration) if duration is not None else None,
                (output_preview or "")[:8192],
                tenant,
            ),
        )
        conn.commit()

def last_results_select(tenant: str, job_id: str):
    tenant = (tenant or DEFAULT_TENANT).strip()
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT ts, value, metrics_json, error, duration, output_preview FROM cronhub_last_results WHERE tenant=? AND job_id=?",
            (tenant, job_id),
        ).fetchone()

    if not row:
        return None

    ts, value, metrics_json, error, duration, output_preview = row
    try:
        metrics = json.loads(metrics_json or "{}")
        if not isinstance(metrics, dict):
            metrics = {}
    except Exception:
        metrics = {}

    return {
        "ts": ts,
        "value": value,
        "metrics": metrics,
        "error": error or "",
        "duration": duration,
        "output_preview": output_preview or "",
        "tenant": tenant,
    }


def init_db():
    _init_db()
    from .audit import init_audit_db
    init_audit_db()
