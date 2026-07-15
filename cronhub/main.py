# main.py

from fastapi import FastAPI

from .core.config import middleware
from .routes import auth, jobs, ui
from .routes import admin_export  # yeni
from .routes import users       # əgər users route da əlavə etmisənsə
from .routes import ssh_key

from .scheduler.history import init_db
from .scheduler.executor import init_scheduler
from .core.ssh_keys import ensure_ssh_key

app = FastAPI(middleware=middleware)

# router-lər APP yaradıldıqdan sonra
app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(ui.router)
app.include_router(admin_export.router)
app.include_router(users.router)
app.include_router(ssh_key.router)

@app.on_event("startup")
async def on_startup():
    init_db()
    init_scheduler()
    ensure_ssh_key()
