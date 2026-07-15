from fastapi import APIRouter, Form, HTTPException, Request, Query
from fastapi.responses import JSONResponse

from ..scheduler.users import users_list
from ..scheduler.tenant_access import (
    VALID_ROLES,
    tenant_access_delete,
    tenant_access_list_all,
    tenant_access_upsert,
)

router = APIRouter()

def _require_admin(request: Request):
    u = request.session.get("user") or {}
    if not (isinstance(u, dict) and u.get("role") == "admin"):
        raise HTTPException(403, "admin only")

@router.get("/users")
def list_users(request: Request, limit: int = Query(default=200, ge=1, le=2000)):
    _require_admin(request)
    return JSONResponse({"items": users_list(limit=limit)})


@router.get("/tenant-access")
def list_tenant_access(request: Request):
    _require_admin(request)
    grants = tenant_access_list_all()

    by_sub = {u["sub"]: u for u in users_list(limit=2000)}
    for g in grants:
        u = by_sub.get(g["user_sub"])
        g["display_name"] = (u or {}).get("display_name") or g["user_sub"]
        g["email"] = (u or {}).get("email") or ""

    return JSONResponse({"items": grants})


@router.post("/tenant-access")
def grant_tenant_access(
    request: Request,
    user_sub: str = Form(...),
    tenant: str = Form(...),
    role: str = Form(...),
):
    _require_admin(request)

    user_sub = user_sub.strip()
    tenant = tenant.strip()
    role = role.strip().lower()

    if not user_sub or not tenant:
        raise HTTPException(400, "user_sub and tenant are required")
    if role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(VALID_ROLES)}")

    tenant_access_upsert(user_sub, tenant, role)
    return JSONResponse({"ok": True})


@router.delete("/tenant-access")
def revoke_tenant_access(request: Request, user_sub: str, tenant: str):
    _require_admin(request)
    tenant_access_delete(user_sub.strip(), tenant.strip())
    return JSONResponse({"ok": True})
