from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..core.config import EXPORT_SECRET
from ..scheduler import executor as exec_mod
from ..core.exporter import write_jobs_export, copy_sqlite_backup

router = APIRouter()

def _is_admin_session(request: Request) -> bool:
    u = request.session.get("user") or {}
    if isinstance(u, dict):
        return (u.get("role") == "admin")
    return False

def _check_token_or_admin(request: Request):
    # 1) admin session ilə icazə
    if _is_admin_session(request):
        return

    # 2) token ilə icazə (cron job üçün)
    if not EXPORT_SECRET:
        raise HTTPException(403, "export token is not configured")
    token = request.headers.get("x-cronhub-token") or ""
    if token != EXPORT_SECRET:
        raise HTTPException(403, "invalid token")

@router.post("/admin/export/jobs")
def export_jobs(request: Request):
    _check_token_or_admin(request)
    if exec_mod.scheduler is None:
        raise HTTPException(503, "scheduler is not running")
    path = write_jobs_export(exec_mod.scheduler)
    return JSONResponse({"ok": True, "path": path})

@router.post("/admin/export/db")
def export_db(request: Request):
    _check_token_or_admin(request)
    path = copy_sqlite_backup()
    return JSONResponse({"ok": True, "path": path})
