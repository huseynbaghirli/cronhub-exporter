# main.py

from fastapi import FastAPI

from .core.config import middleware
from .routes import auth, jobs, ui
from .routes import admin_export  # yeni
from .routes import users       # əgər users route da əlavə etmisənsə

from .scheduler.history import init_db
from .scheduler.executor import init_scheduler

app = FastAPI(middleware=middleware)

# router-lər APP yaradıldıqdan sonra
app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(ui.router)
app.include_router(admin_export.router)
app.include_router(users.router)

@app.on_event("startup")
async def on_startup():
    init_db()
    init_scheduler()
