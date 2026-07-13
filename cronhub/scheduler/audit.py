# cronhub/scheduler/audit.py
import json
import sqlite3
import time
from datetime import datetime

SQLITE_FILE = "data/jobs.sqlite"

SENSITIVE_KEYS = {
    "password", "pass", "secret", "token", "authorization", "auth",
    "api_key", "apikey", "client_secret"
}

def _db_conn():
    return sqlite3.connect(SQLITE_FILE, check_same_thread=False)

def init_audit_db():
    with _db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS cronhub_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              iso TEXT NOT NULL,
              actor TEXT,
              actor_type TEXT,
              actor_email TEXT,
              ip TEXT,
              user_agent TEXT,
              tenant TEXT,
              action TEXT NOT NULL,
              target_type TEXT,
              target_id TEXT,
              ok INTEGER NOT NULL,
              message TEXT,
              before_json TEXT,
              after_json TEXT,
              meta_json TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON cronhub_audit(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON cronhub_audit(tenant, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON cronhub_audit(action, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_target ON cronhub_audit(target_type, target_id)")
        conn.commit()

def _mask_value(v):
    if v is None:
        return None
    s = str(v)
    if len(s) <= 4:
        return "***"
    return s[:2] + "***" + s[-2:]

def _mask_obj(obj):
    # dict/list içində sensitive key-ləri maskala
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k).lower()
            if any(sk in ks for sk in SENSITIVE_KEYS):
                out[k] = _mask_value(v)
            else:
                out[k] = _mask_obj(v)
        return out
    if isinstance(obj, list):
        return [_mask_obj(x) for x in obj]
    return obj

def _safe_json(x, limit=5000):
    try:
        # böyük string-ləri kəsmək
        if isinstance(x, str) and len(x) > limit:
            x = x[:limit] + "…"
        x = _mask_obj(x)
        s = json.dumps(x, ensure_ascii=False)
        if len(s) > limit:
            s = s[:limit] + "…"
        return s
    except Exception:
        return None

def audit_insert(
    *,
    ts: float,
    actor: str | None,
    actor_type: str | None,
    actor_email: str | None,
    ip: str | None,
    user_agent: str | None,
    tenant: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    ok: bool = True,
    message: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    meta: dict | None = None,
):
    # ✅ 1) tenant switch heç loglanmasın
    if action == "tenant.switch":
        return

    # ✅ 2) job action-larda ID görünməsin, amma job adı qalsın
    if (target_type or "") == "job" and (action or "").startswith("job."):
        job_name = None

        # meta -> job_name (əgər varsa)
        if isinstance(meta, dict):
            job_name = (meta.get("job_name") or "").strip() or None

        # after/before -> name
        if not job_name and isinstance(after, dict):
            job_name = (after.get("name") or "").strip() or None
        if not job_name and isinstance(before, dict):
            job_name = (before.get("name") or "").strip() or None

        # meta-da job_id saxla (UI göstərmir)
        meta = dict(meta or {})
        if target_id:
            meta.setdefault("job_id", target_id)

        # Target sütununda ID yox, adı görünsün
        target_id = job_name or "job"

    init_audit_db()  # təhlükəsiz: table yoxdursa yaradır
    iso = datetime.utcfromtimestamp(ts).isoformat() + "Z"

    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO cronhub_audit
              (ts, iso, actor, actor_type, actor_email, ip, user_agent, tenant, action,
               target_type, target_id, ok, message, before_json, after_json, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(ts),
                iso,
                actor or "",
                actor_type or "",
                actor_email or "",
                ip or "",
                user_agent or "",
                tenant or "",
                action,
                target_type or "",
                target_id or "",
                1 if ok else 0,
                (message or "")[:1000],
                _safe_json(before),
                _safe_json(after),
                _safe_json(meta),
            ),
        )
        conn.commit()

def audit_list(tenant: str | None = None, limit: int = 200):
    init_audit_db()
    limit = max(1, min(int(limit), 2000))
    with _db_conn() as conn:
        if tenant:
            rows = conn.execute(
                """
                SELECT ts, iso, actor, actor_type, actor_email, ip, user_agent, tenant, action,
                       target_type, target_id, ok, message
                FROM cronhub_audit
                WHERE tenant=?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (tenant, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT ts, iso, actor, actor_type, actor_email, ip, user_agent, tenant, action,
                       target_type, target_id, ok, message
                FROM cronhub_audit
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    out = []
    for r in rows:
        out.append({
            "ts": r[0], "iso": r[1],
            "actor": r[2], "actor_type": r[3], "actor_email": r[4],
            "ip": r[5], "user_agent": r[6],
            "tenant": r[7],
            "action": r[8],
            "target_type": r[9], "target_id": r[10],
            "ok": bool(r[11]),
            "message": r[12] or "",
        })
    return out

def audit_prune(retention_days: float, tenant: str | None = None) -> int:
    """
    retention_days qədər saxla, köhnələri sil.
    tenant verilsə yalnız o tenant üçün silər.
    """
    init_audit_db()
    try:
        days = float(retention_days)
    except Exception:
        days = 30.0
    if days < 0:
        days = 0.0

    cutoff = time.time() - days * 86400.0

    with _db_conn() as conn:
        if tenant:
            cur = conn.execute(
                "DELETE FROM cronhub_audit WHERE tenant=? AND ts<?",
                (tenant, float(cutoff)),
            )
        else:
            cur = conn.execute(
                "DELETE FROM cronhub_audit WHERE ts<?",
                (float(cutoff),),
            )
        conn.commit()
        return int(cur.rowcount or 0)
