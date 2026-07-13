from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse

from ..scheduler.users import users_list

router = APIRouter()

def _require_admin(request: Request):
    u = request.session.get("user") or {}
    if not (isinstance(u, dict) and u.get("role") == "admin"):
        raise HTTPException(403, "admin only")

@router.get("/users")
def list_users(request: Request, limit: int = Query(default=200, ge=1, le=2000)):
    _require_admin(request)
    return JSONResponse({"items": users_list(limit=limit)})
