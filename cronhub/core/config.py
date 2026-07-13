# cronhub/core/config.py

import os
from datetime import datetime

import pytz
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

ADMIN_USER = os.getenv("CRONHUB_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("CRONHUB_ADMIN_PASS", "admin")
SECRET_KEY = os.getenv("CRONHUB_SECRET_KEY", "dev-secret-change-me")

TZ = pytz.timezone("Asia/Baku")
DB_URL = "sqlite:///data/jobs.sqlite"
jobstores = {"default": SQLAlchemyJobStore(url=DB_URL)}
job_defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}

SQLITE_FILE = "data/jobs.sqlite"

# --- Keycloak / OIDC ---
KEYCLOAK_ENABLED = os.getenv("KEYCLOAK_ENABLED", "true").lower() == "true"

KEYCLOAK_BASE_URL = os.getenv("KEYCLOAK_BASE_URL", "https://auth.msolution.az")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "MSolution")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "cronhub")
KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
KEYCLOAK_SCOPES = os.getenv("KEYCLOAK_SCOPES", "openid profile email")

# CronHub public URL (callback üçün)
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://cronhub.msolution.az")



# --- RBAC / Users / Export / Audit retention ---
AUDIT_RETENTION_DAYS = int(os.getenv("CRONHUB_AUDIT_RETENTION_DAYS", "30"))

# Readonly group-lar (Keycloak group name-ləri)
# Məs: "CronHubReadOnly,BI_ReadOnly"
READONLY_GROUPS = {
    g.strip() for g in os.getenv("CRONHUB_READONLY_GROUPS", "CronHubReadOnly").split(",") if g.strip()
}

# Write/Admin icazəsi olan group-lar
# Səndə login üçün DevOps/BA var idi, bunu write üçün də istifadə edirik
WRITE_GROUPS = {
    g.strip() for g in os.getenv("CRONHUB_WRITE_GROUPS", "DevOps,BA").split(",") if g.strip()
}

EXPORT_DIR = os.getenv("CRONHUB_EXPORT_DIR", "/data/exports")
EXPORT_SECRET = os.getenv("CRONHUB_EXPORT_SECRET", "")  # boş olsa token-lu çağırışı söndürər

USERS_DB_FILE = os.getenv("CRONHUB_USERS_DB_FILE", "data/jobs.sqlite")  # eyni sqlite içində saxlayırıq


# Auth middleware (AFTER SessionMiddleware)
class AuthRequiredMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.allow = (
            "/login",
            "/auth/callback",
            "/favicon.ico",
            "/metrics",
            "/admin/export",
        #    "/admin/import",
        )



    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self.allow):
            return await call_next(request)
        try:
            user = request.session.get("user")
        except AssertionError:
            user = None
        if not user:
            return RedirectResponse(url=request.app.url_path_for("login_page"), status_code=303)
        return await call_next(request)

middleware = [
    Middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax"),
    Middleware(AuthRequiredMiddleware),
]
