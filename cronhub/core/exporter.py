# cronhub/core/exporter.py

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from ..core.config import EXPORT_DIR, SQLITE_FILE

def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")

def ensure_export_dir() -> Path:
    p = Path(EXPORT_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p

def export_jobs_snapshot(scheduler) -> dict[str, Any]:
    jobs = scheduler.get_jobs() if scheduler else []
    out = []
    for j in jobs:
        cfg = {}
        try:
            cfg = dict((j.kwargs or {}).get("config", {}) or {})
        except Exception:
            cfg = {}
        out.append({
            "id": j.id,
            "next_run_time": (j.next_run_time.isoformat() if getattr(j, "next_run_time", None) else None),
            "trigger": str(getattr(j, "trigger", "")),
            "config": cfg,
        })
    return {"ts": time.time(), "items": out}

def write_jobs_export(scheduler) -> str:
    d = ensure_export_dir()
    name = f"cronhub_jobs_{_ts()}.json"
    path = d / name
    payload = export_jobs_snapshot(scheduler)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # latest link (sadə)
    latest = d / "cronhub_jobs_latest.json"
    try:
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    return str(path)

def copy_sqlite_backup() -> str:
    d = ensure_export_dir()
    name = f"cronhub_db_{_ts()}.sqlite"
    dst = d / name
    src = Path(SQLITE_FILE)
    if not src.exists():
        raise FileNotFoundError(f"sqlite file not found: {SQLITE_FILE}")
    shutil.copy2(str(src), str(dst))
    latest = d / "cronhub_db_latest.sqlite"
    try:
        shutil.copy2(str(src), str(latest))
    except Exception:
        pass
    return str(dst)
