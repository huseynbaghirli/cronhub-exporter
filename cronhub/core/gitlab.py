"""GitLab mirror for job definitions.

Every job is stored in the repo as one JSON file at

    <prefix>/<tenant>/<folder>/<name>.json

so a job can be reviewed and edited from either side: change it in CronHub and
a commit lands in GitLab; change it in GitLab and `pull_jobs()` brings it back.
One file per job (rather than a single big export) keeps diffs readable and
lets two people touch different jobs without conflicting.

Nothing here raises into the request path - a GitLab outage must never stop
someone from creating a job in CronHub, so push failures are reported, not
fatal.
"""

import json
import logging
import re
import urllib.parse

import requests

from .config import (
    GITLAB_BRANCH,
    GITLAB_ENABLED,
    GITLAB_PATH_PREFIX,
    GITLAB_PROJECT_ID,
    GITLAB_TIMEOUT,
    GITLAB_TOKEN,
    GITLAB_URL,
)

logger = logging.getLogger("uvicorn.error")

# Config keys that describe the job itself. Anything else (runtime state,
# counters) stays out of git so the files diff cleanly.
_SYNCED_KEYS = (
    "id", "short_id", "created_at", "tenant", "folder", "name", "description",
    "type", "cron", "timeout", "value_regex", "retention_days",
    "metrics_enabled", "extra_labels", "threshold_red", "threshold_yellow",
    "threshold_direction", "command", "method", "url", "headers", "body",
)

_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def is_enabled() -> bool:
    return bool(GITLAB_ENABLED and GITLAB_PROJECT_ID and GITLAB_TOKEN)


def _api(path: str) -> str:
    pid = urllib.parse.quote(str(GITLAB_PROJECT_ID), safe="")
    return f"{GITLAB_URL}/api/v4/projects/{pid}{path}"


def _headers() -> dict:
    return {"PRIVATE-TOKEN": GITLAB_TOKEN}


def _slug(part: str) -> str:
    """Makes one path segment safe without mangling it beyond recognition -
    these paths are meant to be browsed and edited by humans in GitLab."""
    s = _UNSAFE.sub("-", (part or "").strip()).strip(". ")
    return s or "_"


def job_path(cfg: dict) -> str:
    """Repo path for a job. Folders may be nested ('Payments/Reports'), which
    maps onto real directories."""
    tenant = _slug(cfg.get("tenant") or "business")
    folder = "/".join(_slug(p) for p in (cfg.get("folder") or "").split("/") if p.strip())
    name = _slug(cfg.get("name") or cfg.get("id") or "job")
    parts = [GITLAB_PATH_PREFIX, tenant] if GITLAB_PATH_PREFIX else [tenant]
    if folder:
        parts.append(folder)
    parts.append(f"{name}.json")
    return "/".join(p for p in parts if p)


def serialize(cfg: dict) -> str:
    out = {k: cfg[k] for k in _SYNCED_KEYS if k in cfg}
    return json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def commit_message(action: str, cfg: dict, actor: str) -> str:
    """The commit line the user asked for: who did what, to which job, where."""
    tenant = cfg.get("tenant") or "business"
    folder = cfg.get("folder") or "(no folder)"
    name = cfg.get("name") or cfg.get("id")
    short = f" #{cfg['short_id']}" if cfg.get("short_id") else ""
    head = f"{action}: {tenant}/{folder}/{name}{short} by {actor}"
    body = (
        f"\n\nAction: {action}\nActor:  {actor}\nTenant: {tenant}\n"
        f"Folder: {folder}\nJob:    {name}{short}\n"
    )
    return head + body


def _request(method: str, url: str, **kw):
    return requests.request(
        method, url, headers=_headers(), timeout=GITLAB_TIMEOUT, **kw
    )


def file_exists(path: str) -> bool:
    url = _api(f"/repository/files/{urllib.parse.quote(path, safe='')}")
    r = _request("GET", url, params={"ref": GITLAB_BRANCH})
    return r.status_code == 200


def _commit(actions: list[dict], message: str, actor: str, actor_email: str) -> dict:
    payload = {
        "branch": GITLAB_BRANCH,
        "commit_message": message,
        "actions": actions,
        "author_name": actor or "cronhub",
        "author_email": actor_email or "cronhub@localhost",
    }
    r = _request("POST", _api("/repository/commits"), json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitLab commit failed ({r.status_code}): {r.text[:400]}")
    return r.json()


def push_job(cfg: dict, action: str, actor: str, actor_email: str = "",
             old_cfg: dict | None = None) -> dict:
    """Mirrors one job into GitLab. Returns {'ok': bool, ...} - never raises,
    so a GitLab problem can't fail the CronHub operation that triggered it."""
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}

    try:
        path = job_path(cfg)
        actions = [{
            "action": "update" if file_exists(path) else "create",
            "file_path": path,
            "content": serialize(cfg),
        }]

        # A rename or a move between folders changes the path - drop the old
        # file in the same commit so the repo doesn't accumulate ghosts.
        if old_cfg:
            old_path = job_path(old_cfg)
            if old_path != path and file_exists(old_path):
                actions.append({"action": "delete", "file_path": old_path})

        res = _commit(actions, commit_message(action, cfg, actor), actor, actor_email)
        logger.info("[cronhub] gitlab %s %s -> %s", action, path, res.get("id", "")[:8])
        return {"ok": True, "path": path, "commit": res.get("id")}
    except Exception as e:
        logger.warning("[cronhub] gitlab push failed for %s: %s", cfg.get("name"), e)
        return {"ok": False, "error": str(e)}


def delete_job(cfg: dict, actor: str, actor_email: str = "") -> dict:
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}
    try:
        path = job_path(cfg)
        if not file_exists(path):
            return {"ok": True, "skipped": "not in repo"}
        res = _commit(
            [{"action": "delete", "file_path": path}],
            commit_message("delete", cfg, actor), actor, actor_email,
        )
        logger.info("[cronhub] gitlab delete %s -> %s", path, res.get("id", "")[:8])
        return {"ok": True, "path": path, "commit": res.get("id")}
    except Exception as e:
        logger.warning("[cronhub] gitlab delete failed for %s: %s", cfg.get("name"), e)
        return {"ok": False, "error": str(e)}


def push_many(cfgs: list[dict], actor: str, actor_email: str = "",
              message: str | None = None) -> dict:
    """One commit for many jobs - used by 'Push all' to seed the repo."""
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}
    if not cfgs:
        return {"ok": True, "pushed": 0}
    try:
        actions = []
        for cfg in cfgs:
            path = job_path(cfg)
            actions.append({
                "action": "update" if file_exists(path) else "create",
                "file_path": path,
                "content": serialize(cfg),
            })
        msg = message or f"sync: push {len(cfgs)} job(s) from CronHub by {actor}"
        res = _commit(actions, msg, actor, actor_email)
        return {"ok": True, "pushed": len(actions), "commit": res.get("id")}
    except Exception as e:
        logger.warning("[cronhub] gitlab push_many failed: %s", e)
        return {"ok": False, "error": str(e)}


def list_job_files() -> list[str]:
    """Every job JSON under the configured prefix, following pagination."""
    paths, page = [], 1
    while True:
        r = _request("GET", _api("/repository/tree"), params={
            "ref": GITLAB_BRANCH,
            "path": GITLAB_PATH_PREFIX or "",
            "recursive": True,
            "per_page": 100,
            "page": page,
        })
        if r.status_code == 404:
            return []          # prefix doesn't exist yet - nothing synced so far
        if r.status_code != 200:
            raise RuntimeError(f"GitLab tree failed ({r.status_code}): {r.text[:300]}")
        batch = r.json()
        if not batch:
            break
        for it in batch:
            if it.get("type") == "blob" and str(it.get("path", "")).endswith(".json"):
                paths.append(it["path"])
        nxt = r.headers.get("X-Next-Page")
        if not nxt:
            break
        page = int(nxt)
    return paths


def read_job_file(path: str) -> dict | None:
    url = _api(f"/repository/files/{urllib.parse.quote(path, safe='')}/raw")
    r = _request("GET", url, params={"ref": GITLAB_BRANCH})
    if r.status_code != 200:
        logger.warning("[cronhub] gitlab read %s failed (%s)", path, r.status_code)
        return None
    try:
        cfg = json.loads(r.text)
    except Exception as e:
        logger.warning("[cronhub] gitlab %s is not valid JSON: %s", path, e)
        return None
    return cfg if isinstance(cfg, dict) else None


def pull_jobs() -> list[dict]:
    """Reads every job definition currently in the repo."""
    if not is_enabled():
        return []
    out = []
    for path in list_job_files():
        cfg = read_job_file(path)
        if cfg and cfg.get("id"):
            out.append(cfg)
        elif cfg:
            logger.warning("[cronhub] gitlab %s has no job id, skipped", path)
    return out
