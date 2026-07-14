# cronhub/routes/auth.py
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from authlib.integrations.starlette_client import OAuth
from authlib.integrations.base_client.errors import OAuthError

from ..scheduler.audit import audit_insert
from ..scheduler.users import users_upsert

from ..core.config import (
    ADMIN_USER,
    ADMIN_PASS,
    KEYCLOAK_ENABLED,
    KEYCLOAK_BASE_URL,
    KEYCLOAK_REALM,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_SCOPES,
    APP_BASE_URL,
)
from ..templates.loader import LOGIN_HTML

router = APIRouter()

OIDC_DISCOVERY_URL = (
    f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}/.well-known/openid-configuration"
)

oauth = OAuth()
oauth.register(
    name="keycloak",
    client_id=KEYCLOAK_CLIENT_ID,
    client_secret=KEYCLOAK_CLIENT_SECRET,
    server_metadata_url=OIDC_DISCOVERY_URL,
    client_kwargs={"scope": KEYCLOAK_SCOPES},
)

# -------------------------
# TENANT + ROLE CONFIG
# -------------------------
DEFAULT_TENANT = "business"

# hansı qrup hansı tenant-ları görə bilər
TENANT_GROUP_MAP = {
    "DevOps": ["system", "business", "monitoring"],
    "BA": ["business", "monitoring"],
    "Cronhub-RO": ["business", "monitoring"],
}

# login icazəsi olan qruplar
ALLOWED_LOGIN_GROUPS = {"DevOps", "BA", "Cronhub-RO"}


def _norm_groups(groups) -> set[str]:
    if not groups:
        return set()
    out: set[str] = set()
    for g in groups:
        if not isinstance(g, str):
            continue
        gg = g.strip().lstrip("/").split("/")[-1].strip()
        if gg:
            out.add(gg)
    return out


def _calc_allowed_tenants(groups_norm: set[str]) -> list[str]:
    allowed: list[str] = []
    for g in groups_norm:
        v = TENANT_GROUP_MAP.get(g)
        if not v:
            continue
        if isinstance(v, str):
            v = [v]
        for t in v:
            if t not in allowed:
                allowed.append(t)
    return allowed


def _calc_role(groups_norm: set[str]) -> str:
    # tələb etdiyin rule:
    # DevOps -> admin (edit + audit)
    # BA -> editor (yalnız business, edit var, audit yox)
    # Cronhub-RO -> ro (read-only, audit yox)
    if "DevOps" in groups_norm:
        return "admin"
    if "BA" in groups_norm:
        return "editor"
    if "Cronhub-RO" in groups_norm:
        return "ro"
    return "user"


def render_login(error_block: str = "") -> str:
    html = LOGIN_HTML.replace("{ERROR_BLOCK}", error_block or "")
    if not KEYCLOAK_ENABLED:
        html = html.replace(
            "{SSO_BLOCK}",
            '<div class="hint">SSO disabled (KEYCLOAK_ENABLED=false)</div>',
        )
    else:
        html = html.replace("{SSO_BLOCK}", "")
    return html


@router.get("/login", name="login_page", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(render_login(""))


@router.post("/login")
async def login(
    request: Request,
    action: str = Form(...),  # "sso" | "local"
    username: str | None = Form(None),
    password: str | None = Form(None),
):
    # 1) Keycloak SSO
    if action == "sso":
        if not KEYCLOAK_ENABLED:
            return HTMLResponse(
                render_login('<div class="err">SSO is disabled</div>'),
                status_code=400,
            )

        redirect_uri = f"{APP_BASE_URL}/auth/callback"
        return await oauth.keycloak.authorize_redirect(request, redirect_uri)

    # 2) Local admin fallback
    if action == "local":
        ok_user = (username == ADMIN_USER)
        ok_pass = (password == ADMIN_PASS)
        if not (ok_user and ok_pass):
            return HTMLResponse(
                render_login('<div class="err">Invalid local admin credentials</div>'),
                status_code=401,
            )
 
        request.session["user"] = {
            "type": "local",
            "sub": ADMIN_USER,
            "preferred_username": ADMIN_USER,
            "email": "",
            "name": ADMIN_USER,
            "display_name": ADMIN_USER,
            "role": "admin",
            "groups": ["local-admin"],
            "allowed_tenants": ["business", "system" , "monitoring"],
        }
        request.session["active_tenant"] = DEFAULT_TENANT

        users_upsert(
            sub=str(ADMIN_USER),
            user_type="local",
            preferred_username=ADMIN_USER,
            email="",
            display_name=ADMIN_USER,
            groups=["local-admin"],
            role="admin",
            allowed_tenants=["business", "system" , "monitoring"],
        )

        return RedirectResponse(url=request.app.url_path_for("index"), status_code=303)

    return HTMLResponse(render_login('<div class="err">Invalid action</div>'), status_code=400)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url=request.app.url_path_for("index"), status_code=303)

    try:
        token = await oauth.keycloak.authorize_access_token(request)
        userinfo = await oauth.keycloak.userinfo(token=token)
    except OAuthError as e:
        request.session.clear()
        msg = (
            f'<div class="err">SSO login failed: {e.error}. '
            f'Please try again or use Local Admin.</div>'
        )
        return HTMLResponse(render_login(msg), status_code=401)

    # -------------------------
    # 1) group norm + login icazəsi
    # -------------------------
    groups = userinfo.get("groups") or []
    groups_norm = _norm_groups(groups)

    if not (groups_norm & ALLOWED_LOGIN_GROUPS):
        request.session.clear()
        msg = '<div class="err">Access denied: only DevOps, BA, or Cronhub-RO users can login.</div>'
        return HTMLResponse(render_login(msg), status_code=403)

    # -------------------------
    # 2) tenant icazəsi
    # -------------------------
    allowed_tenants = _calc_allowed_tenants(groups_norm)
    if not allowed_tenants:
        request.session.clear()
        msg = '<div class="err">Access denied: you have no tenant mapping.</div>'
        return HTMLResponse(render_login(msg), status_code=403)

    active_tenant = allowed_tenants[0] if allowed_tenants else DEFAULT_TENANT

    # -------------------------
    # 3) display name
    # -------------------------
    full_name = (userinfo.get("name") or "").strip()
    if not full_name:
        gn = (userinfo.get("given_name") or "").strip()
        fn = (userinfo.get("family_name") or "").strip()
        full_name = (f"{gn} {fn}").strip()
    if not full_name:
        full_name = (
            (userinfo.get("preferred_username") or "").strip()
            or (userinfo.get("email") or "").strip()
            or "user"
        )

    # -------------------------
    # 4) role (MÜTLƏQ təyin olunur)
    # -------------------------
    role = _calc_role(groups_norm)

    session_user = {
        "type": "sso",
        "sub": userinfo.get("sub"),
        "preferred_username": userinfo.get("preferred_username"),
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "display_name": full_name,
        "role": role,
        "groups": sorted(list(groups_norm)),
        "allowed_tenants": allowed_tenants,
    }
    request.session["user"] = session_user
    request.session["active_tenant"] = active_tenant

    users_upsert(
        sub=str(userinfo.get("sub") or userinfo.get("preferred_username") or full_name),
        user_type="sso",
        preferred_username=userinfo.get("preferred_username"),
        email=userinfo.get("email"),
        display_name=full_name,
        groups=sorted(list(groups_norm)),
        role=role,
        allowed_tenants=allowed_tenants,
    )

    # audit login (hamıya yazıla bilər — bu RO üçün problem deyil)
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "") or "")
    ua = request.headers.get("user-agent") or ""
    audit_insert(
        ts=time.time(),
        actor=full_name,
        actor_type="sso",
        actor_email=(userinfo.get("email") or ""),
        ip=ip,
        user_agent=ua,
        tenant=active_tenant,
        action="auth.login",
        target_type="user",
        target_id=str(userinfo.get("preferred_username") or userinfo.get("sub") or ""),
        ok=True,
        message=f"role={role} groups={sorted(list(groups_norm))}",
        before={},
        after={},
        meta={"allowed_tenants": allowed_tenants},
    )

    return RedirectResponse(url=request.app.url_path_for("index"), status_code=303)


@router.post("/tenant")
async def set_tenant(request: Request, tenant: str = Form(...)):
    user = request.session.get("user") or {}
    allowed = (user or {}).get("allowed_tenants") or []
    role = user.get("role") if isinstance(user, dict) else None

    tenant = (tenant or "").strip()
    if not tenant:
        return JSONResponse({"ok": False, "error": "tenant is required"}, status_code=400)

    is_admin = (role == "admin")
    if not is_admin:
        if not allowed:
            return JSONResponse({"ok": False, "error": "no allowed tenants"}, status_code=403)
        if tenant not in allowed:
            return JSONResponse({"ok": False, "error": "tenant not allowed"}, status_code=403)
    elif tenant not in allowed:
        # Admins can create a brand-new tenant simply by switching to it.
        allowed = allowed + [tenant]
        user["allowed_tenants"] = allowed
        request.session["user"] = user

    old = request.session.get("active_tenant") or DEFAULT_TENANT
    request.session["active_tenant"] = tenant

    actor = ""
    actor_type = ""
    actor_email = ""
    if isinstance(user, dict):
        actor = user.get("display_name") or user.get("preferred_username") or user.get("email") or "user"
        actor_type = user.get("type") or "sso"
        actor_email = user.get("email") or ""
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "") or "")
    ua = request.headers.get("user-agent") or ""

    audit_insert(
        ts=time.time(),
        actor=actor,
        actor_type=actor_type,
        actor_email=actor_email,
        ip=ip,
        user_agent=ua,
        tenant=tenant,
        action="tenant.switch",
        target_type="tenant",
        target_id=tenant,
        ok=True,
        message=f"{old} -> {tenant}",
        before={"active_tenant": old},
        after={"active_tenant": tenant},
        meta={"allowed": allowed},
    )

    return {"ok": True, "active_tenant": tenant}


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()

    if KEYCLOAK_ENABLED:
        post_logout_redirect_uri = f"{APP_BASE_URL}/login"
        url = (
            f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
            f"/protocol/openid-connect/logout"
            f"?client_id={KEYCLOAK_CLIENT_ID}"
            f"&post_logout_redirect_uri={post_logout_redirect_uri}"
        )
        return RedirectResponse(url=url, status_code=303)

    return RedirectResponse(url=request.app.url_path_for("login_page"), status_code=303)


@router.get("/whoami")
def whoami(request: Request):
    user = request.session.get("user") or {}
    return {
        "user": user,
        "active_tenant": request.session.get("active_tenant") or DEFAULT_TENANT,
        "allowed_tenants": (user or {}).get("allowed_tenants") or [],
    }
