"""GitOps endpoints: push CronHub's jobs into GitLab, and pull them back.

Pull applies job files straight to the scheduler rather than going through the
job routes, which is deliberate: it keeps the incoming change from triggering
another push back to GitLab.
"""

import logging

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Query, Request

from ..core import gitlab
from ..core.config import (
    GITLAB_BRANCH,
    GITLAB_PATH_PREFIX,
    GITLAB_PROJECT_ID,
    GITLAB_URL,
    TZ,
)
from ..scheduler import executor as exec_mod
from ..scheduler.job_seq import next_job_seq
from .jobs import _actor_info, _active_tenant, _audit, _require_admin

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


@router.get("/admin/gitlab/status")
def gitlab_status(request: Request):
    _require_admin(request)
    return {
        "enabled": gitlab.is_enabled(),
        "url": GITLAB_URL,
        "project_id": GITLAB_PROJECT_ID,
        "branch": GITLAB_BRANCH,
        "path_prefix": GITLAB_PATH_PREFIX,
    }


@router.post("/admin/gitlab/push")
def gitlab_push_all(request: Request):
    """Mirrors every job into the repo in a single commit - used to seed a new
    repo, or to re-align it after editing outside CronHub."""
    _require_admin(request)
    if not gitlab.is_enabled():
        raise HTTPException(400, "GitLab sync is not configured")

    actor, _atype, actor_email, _ip, _ua = _actor_info(request)
    jobs = exec_mod.scheduler.get_jobs() if exec_mod.scheduler else []
    cfgs = [
        dict(j.kwargs.get("config", {}))
        for j in jobs
        if j.kwargs and (j.kwargs.get("config") or {}).get("id")
    ]

    res = gitlab.push_many(cfgs, actor, actor_email)
    if not res.get("ok"):
        raise HTTPException(502, f"GitLab push failed: {res.get('error')}")

    _audit(
        request, "gitlab.push", tenant=_active_tenant(request), target_type="gitlab",
        target_id=res.get("commit", ""), ok=True,
        msg=f"pushed={res.get('pushed')}",
    )
    return res


@router.post("/admin/gitlab/pull")
def gitlab_pull(request: Request, prune: str = Query(None)):
    """Applies the repo's job files to the scheduler.

    Jobs present in CronHub but absent from the repo are left alone unless
    `prune` is set - deleting a live job because a file is missing is too
    destructive to do by default.
    """
    _require_admin(request)
    if not gitlab.is_enabled():
        raise HTTPException(400, "GitLab sync is not configured")

    actor, _atype, _email, _ip, _ua = _actor_info(request)

    try:
        incoming = gitlab.pull_jobs()
    except Exception as e:
        raise HTTPException(502, f"GitLab read failed: {e}")

    created, updated, unchanged, errors = 0, 0, 0, []
    seen: set[str] = set()

    for cfg in incoming:
        job_id = str(cfg.get("id") or "").strip()
        if not job_id:
            continue
        seen.add(job_id)

        if cfg.get("type") not in ("shell", "http"):
            errors.append(f"{job_id}: type must be shell|http")
            continue

        try:
            trigger = CronTrigger.from_crontab(cfg.get("cron") or "* * * * *", timezone=TZ)
        except Exception as e:
            errors.append(f"{job_id}: invalid cron ({e})")
            continue

        existing = exec_mod.scheduler.get_job(job_id)
        current = dict(existing.kwargs.get("config", {})) if (existing and existing.kwargs) else {}

        # Fields CronHub owns rather than the repo. If a hand-written file
        # omits them, keep what the job already has instead of re-allocating -
        # otherwise every pull would hand out a fresh short_id and report the
        # job as changed forever.
        if not cfg.get("short_id"):
            cfg["short_id"] = current.get("short_id") or next_job_seq(job_id)
        if not cfg.get("created_at") and current.get("created_at"):
            cfg["created_at"] = current["created_at"]

        try:
            if existing:
                if current == cfg:
                    unchanged += 1
                    continue
                exec_mod.scheduler.modify_job(job_id, trigger=trigger, kwargs={"config": cfg})
                updated += 1
            else:
                exec_mod.scheduler.add_job(
                    exec_mod.execute_job,
                    trigger=trigger,
                    id=job_id,
                    args=[job_id],
                    kwargs={"config": cfg},
                    replace_existing=True,
                )
                created += 1
        except Exception as e:
            errors.append(f"{job_id}: {e}")

    pruned = 0
    if str(prune or "").strip().lower() in ("1", "true", "on", "yes"):
        for j in list(exec_mod.scheduler.get_jobs()):
            cfg = (j.kwargs or {}).get("config") or {}
            if not cfg.get("id") or j.id in seen:
                continue
            try:
                exec_mod.scheduler.remove_job(j.id)
                pruned += 1
            except Exception as e:
                errors.append(f"{j.id}: prune failed ({e})")

    _audit(
        request, "gitlab.pull", tenant=_active_tenant(request), target_type="gitlab",
        target_id="", ok=True,
        msg=f"created={created} updated={updated} unchanged={unchanged} pruned={pruned}",
    )
    logger.info(
        "[cronhub] gitlab pull by %s: created=%s updated=%s unchanged=%s pruned=%s errors=%s",
        actor, created, updated, unchanged, pruned, len(errors),
    )
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "pruned": pruned,
        "errors": errors[:50],
    }
