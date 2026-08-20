"""GitOps endpoints: push CronHub's jobs into GitLab, and pull them back.

Each tenant is a branch. Pull walks every tenant branch, registers the tenant
locally, and applies that branch's job files.

Pull writes straight to the scheduler rather than going through the job routes,
which is deliberate: it keeps an incoming change from triggering another push
back to GitLab.
"""

import logging

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Query, Request

from ..core import gitlab
from ..core.config import (
    GITLAB_BRANCH,
    GITLAB_BRANCH_PREFIX,
    GITLAB_PATH_PREFIX,
    GITLAB_PROJECT_ID,
    GITLAB_URL,
    TZ,
)
from ..scheduler import executor as exec_mod
from ..scheduler.job_seq import next_job_seq
from ..scheduler.tenants import tenant_register
from .jobs import _actor_info, _active_tenant, _audit, _require_admin

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


@router.get("/admin/gitlab/status")
def gitlab_status(request: Request):
    _require_admin(request)
    out = {
        "enabled": gitlab.is_enabled(),
        "url": GITLAB_URL,
        "project_id": GITLAB_PROJECT_ID,
        "base_branch": GITLAB_BRANCH,
        "branch_prefix": GITLAB_BRANCH_PREFIX,
        "path_prefix": GITLAB_PATH_PREFIX,
    }
    if gitlab.is_enabled():
        try:
            out["tenant_branches"] = gitlab.list_tenants()
        except Exception as e:
            out["tenant_branches_error"] = str(e)
    return out


@router.post("/admin/gitlab/push")
def gitlab_push_all(request: Request):
    """Mirrors every job into its tenant's branch - one commit per tenant.
    Used to seed a repo, or to re-align it after editing outside CronHub."""
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
    if not res.get("ok") and not res.get("pushed"):
        raise HTTPException(502, f"GitLab push failed: {res.get('errors') or res.get('error')}")

    _audit(
        request, "gitlab.push", tenant=_active_tenant(request), target_type="gitlab",
        target_id="", ok=True,
        msg=f"pushed={res.get('pushed')} branches={len(res.get('branches') or {})}",
    )
    return res


@router.post("/admin/gitlab/pull")
def gitlab_pull(request: Request, prune: str = Query(None)):
    """Applies every tenant branch to the scheduler.

    Jobs present in CronHub but absent from the repo are left alone unless
    `prune` is set - deleting a live job because a file is missing is too
    destructive to do by default.
    """
    _require_admin(request)
    if not gitlab.is_enabled():
        raise HTTPException(400, "GitLab sync is not configured")

    actor, _atype, _email, _ip, _ua = _actor_info(request)
    do_prune = str(prune or "").strip().lower() in ("1", "true", "on", "yes")

    try:
        tenant_branches = gitlab.list_tenants()
    except Exception as e:
        raise HTTPException(502, f"GitLab read failed: {e}")

    created, updated, unchanged, errors = 0, 0, 0, []
    new_tenants: list[str] = []
    seen: set[str] = set()

    for tenant, branch in sorted(tenant_branches.items()):
        # A branch is enough to make the tenant real, even with no jobs on it.
        if tenant_register(tenant, source="gitlab"):
            new_tenants.append(tenant)

        try:
            incoming = gitlab.pull_tenant(tenant, branch)
        except Exception as e:
            errors.append(f"{branch}: {e}")
            continue

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
            # omits them, keep what the job already has instead of
            # re-allocating - otherwise every pull would hand out a fresh
            # short_id and report the job as changed forever.
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
    if do_prune:
        for j in list(exec_mod.scheduler.get_jobs()):
            cfg = (j.kwargs or {}).get("config") or {}
            if not cfg.get("id") or j.id in seen:
                continue
            # Only prune within tenants the repo actually knows about, so a
            # tenant that was simply never pushed doesn't get wiped.
            if (cfg.get("tenant") or "business") not in tenant_branches:
                continue
            try:
                exec_mod.scheduler.remove_job(j.id)
                pruned += 1
            except Exception as e:
                errors.append(f"{j.id}: prune failed ({e})")

    _audit(
        request, "gitlab.pull", tenant=_active_tenant(request), target_type="gitlab",
        target_id="", ok=True,
        msg=(f"tenants={len(tenant_branches)} created={created} updated={updated} "
             f"unchanged={unchanged} pruned={pruned}"),
    )
    logger.info(
        "[cronhub] gitlab pull by %s: tenants=%s created=%s updated=%s unchanged=%s pruned=%s errors=%s",
        actor, len(tenant_branches), created, updated, unchanged, pruned, len(errors),
    )
    return {
        "ok": True,
        "tenants": len(tenant_branches),
        "new_tenants": new_tenants,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "pruned": pruned,
        "errors": errors[:50],
    }
