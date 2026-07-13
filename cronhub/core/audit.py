# cronhub/core/audit.py
import os
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AUDIT_DIR = os.getenv("CRONHUB_AUDIT_DIR", "/data/audit")

def _path(tenant: str) -> Path:
    t = (tenant or "system").replace("/", "_")
    return Path(AUDIT_DIR) / f"audit_{t}.jsonl"

def audit_log(
    *,
    tenant: str,
    actor: str,
    action: str,
    target_type: str = "job",
    target_id: str = "",
    ok: bool = True,
    message: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    Path(AUDIT_DIR).mkdir(parents=True, exist_ok=True)
    ev = {
        "ts": int(time.time()),
        "tenant": tenant,
        "actor": actor or "",
        "action": action or "",
        "target_type": target_type or "",
        "target_id": target_id or "",
        "ok": bool(ok),
        "message": message or "",
        "extra": extra or {},
    }
    with _path(tenant).open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

def audit_list(*, tenant: str, limit: int = 200) -> List[Dict[str, Any]]:
    p = _path(tenant)
    if not p.exists():
        return []
    items: List[Dict[str, Any]] = []
    # sadə oxu: çox böyük olsa belə limit qədər saxlayırıq
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return items[: max(1, min(int(limit or 200), 2000))]
