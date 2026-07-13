# cronhub/routes/ui.py

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..templates.loader import INDEX_HTML

router = APIRouter()

@router.get("/", name="index", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)
