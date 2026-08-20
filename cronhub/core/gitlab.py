"""GitLab mirror for job definitions, one branch per tenant.

Each tenant maps to its own branch (`tenant/<slug>` by default), and inside
that branch jobs are stored as

    <prefix>/<folder>/<job name>.json

The tenant is the branch, so it isn't repeated as a directory. Branch names
have to be slugs, which is lossy ("Ops Team" and "ops-team" collapse together),
so every tenant branch also carries `.cronhub/tenant.json` holding the exact
tenant name. That manifest is what pull reads back - never the branch name.

Nothing here raises into the request path: a GitLab outage must never stop
someone from creating a job in CronHub, so failures are reported, not fatal.
"""

import json
import logging
import re
import unicodedata
import urllib.parse

import requests

from .config import (
    GITLAB_BRANCH,
    GITLAB_BRANCH_PREFIX,
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

TENANT_MANIFEST = ".cronhub/tenant.json"

_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def is_enabled() -> bool:
    return bool(GITLAB_ENABLED and GITLAB_PROJECT_ID and GITLAB_TOKEN)


def _api(path: str) -> str:
    pid = urllib.parse.quote(str(GITLAB_PROJECT_ID), safe="")
    return f"{GITLAB_URL}/api/v4/projects/{pid}{path}"


def _request(method: str, url: str, **kw):
    return requests.request(
        method, url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
        timeout=GITLAB_TIMEOUT, **kw
    )


# ---------------------------------------------------------------- naming ---

def tenant_slug(tenant: str) -> str:
    """Git refs can't contain spaces, '~^:?*[', backslashes, '..' or '@{',
    and can't end in '.' or '.lock'. Fold to a conservative ascii slug."""
    s = unicodedata.normalize("NFKD", (tenant or "").strip())
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    if s.endswith(".lock"):
        s = s[: -len(".lock")].rstrip("-._")
    return s or "tenant"


def branch_for_tenant(tenant: str) -> str:
    return f"{GITLAB_BRANCH_PREFIX}{tenant_slug(tenant)}"


def is_tenant_branch(branch: str) -> bool:
    return bool(GITLAB_BRANCH_PREFIX) and str(branch).startswith(GITLAB_BRANCH_PREFIX)


def _slug_path(part: str) -> str:
    """Keeps a path segment readable - these are browsed and edited by hand in
    GitLab - while stripping what a repo path can't hold."""
    s = _UNSAFE.sub("-", (part or "").strip()).strip(". ")
    return s or "_"


def job_path(cfg: dict) -> str:
    """Path inside the tenant's branch. Folders may be nested
    ('Payments/Reports'), which maps onto real directories."""
    folder = "/".join(_slug_path(p) for p in (cfg.get("folder") or "").split("/") if p.strip())
    name = _slug_path(cfg.get("name") or cfg.get("id") or "job")
    parts = [GITLAB_PATH_PREFIX] if GITLAB_PATH_PREFIX else []
    if folder:
        parts.append(folder)
    parts.append(f"{name}.json")
    return "/".join(p for p in parts if p)


def serialize(cfg: dict) -> str:
    out = {k: cfg[k] for k in _SYNCED_KEYS if k in cfg}
    return json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def commit_message(action: str, cfg: dict, actor: str) -> str:
    """Who did what, to which job, where."""
    tenant = cfg.get("tenant") or "business"
    folder = cfg.get("folder") or "(no folder)"
    name = cfg.get("name") or cfg.get("id")
    short = f" #{cfg['short_id']}" if cfg.get("short_id") else ""
    return (
        f"{action}: {tenant}/{folder}/{name}{short} by {actor}"
        f"\n\nAction: {action}\nActor:  {actor}\nTenant: {tenant}\n"
        f"Folder: {folder}\nJob:    {name}{short}\n"
    )


# --------------------------------------------------------------- plumbing ---

def file_exists(path: str, branch: str) -> bool:
    url = _api(f"/repository/files/{urllib.parse.quote(path, safe='')}")
    return _request("GET", url, params={"ref": branch}).status_code == 200


def read_file(path: str, branch: str) -> str | None:
    url = _api(f"/repository/files/{urllib.parse.quote(path, safe='')}/raw")
    r = _request("GET", url, params={"ref": branch})
    return r.text if r.status_code == 200 else None


def _commit(actions: list[dict], message: str, actor: str, actor_email: str,
            branch: str, start_branch: str | None = None) -> dict:
    payload = {
        "branch": branch,
        "commit_message": message,
        "actions": actions,
        "author_name": actor or "cronhub",
        "author_email": actor_email or "cronhub@localhost",
    }
    if start_branch:
        # lets GitLab create the branch as part of the commit
        payload["start_branch"] = start_branch
    r = _request("POST", _api("/repository/commits"), json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitLab commit failed ({r.status_code}): {r.text[:400]}")
    return r.json()


# ---------------------------------------------------------------- tenants ---

def list_branches() -> list[str]:
    out, page = [], 1
    while True:
        r = _request("GET", _api("/repository/branches"),
                     params={"per_page": 100, "page": page})
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise RuntimeError(f"GitLab branches failed ({r.status_code}): {r.text[:300]}")
        batch = r.json()
        if not batch:
            break
        out.extend(b["name"] for b in batch if b.get("name"))
        nxt = r.headers.get("X-Next-Page")
        if not nxt:
            break
        page = int(nxt)
    return out


def branch_exists(branch: str) -> bool:
    url = _api(f"/repository/branches/{urllib.parse.quote(branch, safe='')}")
    return _request("GET", url).status_code == 200


def tenant_of_branch(branch: str) -> str | None:
    """The exact tenant name, from the branch's manifest. Falls back to the
    branch name only when the manifest is missing, since the slug is lossy."""
    raw = read_file(TENANT_MANIFEST, branch)
    if raw:
        try:
            data = json.loads(raw)
            name = str(data.get("tenant") or "").strip()
            if name:
                return name
        except Exception:
            logger.warning("[cronhub] gitlab %s on %s is not valid JSON", TENANT_MANIFEST, branch)
    if is_tenant_branch(branch):
        return branch[len(GITLAB_BRANCH_PREFIX):] or None
    return None


def list_tenants() -> dict[str, str]:
    """{tenant name -> branch} for every tenant branch in the repo."""
    if not is_enabled():
        return {}
    out = {}
    for br in list_branches():
        if not is_tenant_branch(br):
            continue
        name = tenant_of_branch(br)
        if name:
            out[name] = br
    return out


def ensure_tenant_branch(tenant: str, actor: str = "cronhub",
                         actor_email: str = "") -> dict:
    """Creates the tenant's branch (off the base branch) with its manifest, if
    it isn't there yet."""
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}
    try:
        branch = branch_for_tenant(tenant)
        if branch_exists(branch):
            return {"ok": True, "branch": branch, "created": False}

        manifest = json.dumps({"tenant": tenant}, ensure_ascii=False, indent=2) + "\n"
        _commit(
            [{"action": "create", "file_path": TENANT_MANIFEST, "content": manifest}],
            f"tenant: create {tenant} by {actor}\n\nActor:  {actor}\nTenant: {tenant}\n",
            actor, actor_email, branch, start_branch=GITLAB_BRANCH,
        )
        logger.info("[cronhub] gitlab created tenant branch %s", branch)
        return {"ok": True, "branch": branch, "created": True}
    except Exception as e:
        logger.warning("[cronhub] gitlab ensure_tenant_branch(%s) failed: %s", tenant, e)
        return {"ok": False, "error": str(e)}


def delete_tenant_branch(tenant: str) -> dict:
    """Only called when an admin explicitly asks - a branch carries history,
    so it is never removed as a side effect."""
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}
    try:
        branch = branch_for_tenant(tenant)
        if not branch_exists(branch):
            return {"ok": True, "skipped": "no such branch"}
        r = _request("DELETE", _api(
            f"/repository/branches/{urllib.parse.quote(branch, safe='')}"))
        if r.status_code not in (200, 204):
            raise RuntimeError(f"({r.status_code}): {r.text[:300]}")
        return {"ok": True, "branch": branch, "deleted": True}
    except Exception as e:
        logger.warning("[cronhub] gitlab delete_tenant_branch(%s) failed: %s", tenant, e)
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------- jobs ---

def push_job(cfg: dict, action: str, actor: str, actor_email: str = "",
             old_cfg: dict | None = None) -> dict:
    """Mirrors one job into its tenant's branch. Never raises, so a GitLab
    problem can't fail the CronHub operation that triggered it."""
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}

    try:
        tenant = cfg.get("tenant") or "business"
        branch = branch_for_tenant(tenant)

        ens = ensure_tenant_branch(tenant, actor, actor_email)
        if not ens.get("ok"):
            return ens

        path = job_path(cfg)
        actions = [{
            "action": "update" if file_exists(path, branch) else "create",
            "file_path": path,
            "content": serialize(cfg),
        }]

        # A rename or a folder move changes the path; drop the old file in the
        # same commit so the branch doesn't accumulate ghosts.
        old_branch = None
        if old_cfg:
            old_tenant = old_cfg.get("tenant") or "business"
            old_branch = branch_for_tenant(old_tenant)
            old_path = job_path(old_cfg)
            if old_branch == branch and old_path != path and file_exists(old_path, branch):
                actions.append({"action": "delete", "file_path": old_path})

        res = _commit(actions, commit_message(action, cfg, actor), actor, actor_email, branch)

        # Moved to another tenant: the job left the old branch entirely, so it
        # needs its own commit over there.
        moved = None
        if old_branch and old_branch != branch:
            old_path = job_path(old_cfg)
            if branch_exists(old_branch) and file_exists(old_path, old_branch):
                _commit(
                    [{"action": "delete", "file_path": old_path}],
                    commit_message("move-out", old_cfg, actor),
                    actor, actor_email, old_branch,
                )
                moved = old_branch

        logger.info("[cronhub] gitlab %s %s@%s -> %s",
                    action, path, branch, str(res.get("id", ""))[:8])
        out = {"ok": True, "branch": branch, "path": path, "commit": res.get("id")}
        if moved:
            out["removed_from_branch"] = moved
        return out
    except Exception as e:
        logger.warning("[cronhub] gitlab push failed for %s: %s", cfg.get("name"), e)
        return {"ok": False, "error": str(e)}


def delete_job(cfg: dict, actor: str, actor_email: str = "") -> dict:
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}
    try:
        branch = branch_for_tenant(cfg.get("tenant") or "business")
        path = job_path(cfg)
        if not branch_exists(branch) or not file_exists(path, branch):
            return {"ok": True, "skipped": "not in repo"}
        res = _commit(
            [{"action": "delete", "file_path": path}],
            commit_message("delete", cfg, actor), actor, actor_email, branch,
        )
        logger.info("[cronhub] gitlab delete %s@%s", path, branch)
        return {"ok": True, "branch": branch, "path": path, "commit": res.get("id")}
    except Exception as e:
        logger.warning("[cronhub] gitlab delete failed for %s: %s", cfg.get("name"), e)
        return {"ok": False, "error": str(e)}


def push_many(cfgs: list[dict], actor: str, actor_email: str = "") -> dict:
    """Seeds/reconciles the repo: jobs are grouped by tenant and each tenant's
    branch gets one commit."""
    if not is_enabled():
        return {"ok": False, "skipped": "gitlab disabled"}
    if not cfgs:
        return {"ok": True, "pushed": 0, "branches": {}}

    by_tenant: dict[str, list[dict]] = {}
    for cfg in cfgs:
        by_tenant.setdefault(cfg.get("tenant") or "business", []).append(cfg)

    pushed, branches, errors = 0, {}, []
    for tenant, items in by_tenant.items():
        try:
            ens = ensure_tenant_branch(tenant, actor, actor_email)
            if not ens.get("ok"):
                errors.append(f"{tenant}: {ens.get('error')}")
                continue
            branch = ens["branch"]
            actions = [{
                "action": "update" if file_exists(job_path(c), branch) else "create",
                "file_path": job_path(c),
                "content": serialize(c),
            } for c in items]
            _commit(
                actions,
                f"sync: push {len(actions)} job(s) of {tenant} from CronHub by {actor}",
                actor, actor_email, branch,
            )
            pushed += len(actions)
            branches[tenant] = branch
        except Exception as e:
            logger.warning("[cronhub] gitlab push_many(%s) failed: %s", tenant, e)
            errors.append(f"{tenant}: {e}")

    return {"ok": not errors, "pushed": pushed, "branches": branches, "errors": errors}


def list_job_files(branch: str) -> list[str]:
    """Every job JSON under the prefix on one branch, following pagination."""
    paths, page = [], 1
    while True:
        r = _request("GET", _api("/repository/tree"), params={
            "ref": branch,
            "path": GITLAB_PATH_PREFIX or "",
            "recursive": True,
            "per_page": 100,
            "page": page,
        })
        if r.status_code == 404:
            return []          # prefix doesn't exist on this branch yet
        if r.status_code != 200:
            raise RuntimeError(f"GitLab tree failed ({r.status_code}): {r.text[:300]}")
        batch = r.json()
        if not batch:
            break
        for it in batch:
            p = str(it.get("path", ""))
            if it.get("type") == "blob" and p.endswith(".json") and not p.startswith(".cronhub/"):
                paths.append(p)
        nxt = r.headers.get("X-Next-Page")
        if not nxt:
            break
        page = int(nxt)
    return paths


def pull_tenant(tenant: str, branch: str) -> list[dict]:
    """Job definitions on one tenant's branch. The branch decides the tenant,
    so a file claiming a different one is corrected rather than trusted."""
    out = []
    for path in list_job_files(branch):
        raw = read_file(path, branch)
        if raw is None:
            continue
        try:
            cfg = json.loads(raw)
        except Exception as e:
            logger.warning("[cronhub] gitlab %s@%s is not valid JSON: %s", path, branch, e)
            continue
        if not isinstance(cfg, dict) or not cfg.get("id"):
            logger.warning("[cronhub] gitlab %s@%s has no job id, skipped", path, branch)
            continue
        cfg["tenant"] = tenant
        out.append(cfg)
    return out
