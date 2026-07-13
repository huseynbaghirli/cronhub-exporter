#cronhub/templates/loader.py

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def load_template(name: str) -> str:
    path = BASE_DIR / name
    return path.read_text(encoding="utf-8")

INDEX_HTML = load_template("index.html")
LOGIN_HTML = load_template("login.html")
JOB_VIEW_HTML = load_template("job_view.html")