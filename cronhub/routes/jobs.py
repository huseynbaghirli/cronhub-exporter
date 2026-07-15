# cronhub/routes/jobs.py
from datetime import datetime
import uuid
import time
import html
import json
import re

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
from apscheduler.triggers.cron import CronTrigger

from ..core.config import TZ
from ..core.rbac import effective_role_for_tenant
from ..scheduler import executor as exec_mod
from ..scheduler.history import history_select, last_results_select
from ..scheduler.audit import audit_insert, audit_list
from ..scheduler.tenant_access import tenant_access_delete_tenant

router = APIRouter()

DEFAULT_TENANT = "business"

LABEL_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
RESERVED_LABEL_KEYS = {"job_id", "job_name", "tenant", "folder", "subjob", "list"}


def _parse_extra_labels(text: str | None) -> dict[str, str]:
    """Parses 'key=value' lines (or a JSON object) into Prometheus-safe label names."""
    raw: dict[str, str] = {}

    if text and text.strip():
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            raw = {str(k): str(v) for k, v in obj.items()}
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                elif ":" in line:
                    k, v = line.split(":", 1)
                else:
                    continue
                raw[k.strip()] = v.strip()

    out: dict[str, str] = {}
    for k, v in raw.items():
        key = k.strip()
        if not key:
            continue
        if not LABEL_KEY_RE.match(key):
            key = re.sub(r"[^a-zA-Z0-9_]", "_", key)
            if not key or key[0].isdigit():
                key = "_" + key
        if key in RESERVED_LABEL_KEYS:
            continue
        out[key] = v
    return out


def _actor_info(request: Request):
    u = request.session.get("user") or {}
    if isinstance(u, dict):
        actor = (u.get("display_name") or u.get("preferred_username") or u.get("email") or "user")
        actor_type = u.get("type") or "sso"
        actor_email = u.get("email") or ""
    else:
        actor = str(u) if u else "user"
        actor_type = "unknown"
        actor_email = ""
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "") or "")
    ua = request.headers.get("user-agent") or ""
    return actor, actor_type, actor_email, ip, ua


def _role(request: Request) -> str:
    u = request.session.get("user") or {}
    return effective_role_for_tenant(u, _active_tenant(request))


def _is_admin(request: Request) -> bool:
    return _role(request) == "admin"


def _can_write(request: Request) -> bool:
    # DevOps(admin) + BA(editor) yaza bilər
    return _role(request) in ("admin", "editor")


def _require_admin(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Admin only")


def _require_write(request: Request):
    if not _can_write(request):
        raise HTTPException(403, "Read-only user")


IMPORTANT_AUDIT_ACTIONS = {
    "job.create",
    "job.update",
    "job.delete",
    "job.pause",
    "job.resume",
    "job.run_now",
    "job.metrics_toggle",
    "job.duplicate",
    "tenant.delete",
}


def _audit(
    request: Request,
    action: str,
    *,
    tenant: str,
    ok: bool = True,
    msg: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    meta: dict | None = None,
):
    if action not in IMPORTANT_AUDIT_ACTIONS:
        return

    actor, actor_type, actor_email, ip, ua = _actor_info(request)
    audit_insert(
        ts=time.time(),
        actor=actor,
        actor_type=actor_type,
        actor_email=actor_email,
        ip=ip,
        user_agent=ua,
        tenant=tenant,
        action=action,
        target_type=target_type,
        target_id=target_id,
        ok=ok,
        message=msg,
        before=before,
        after=after,
        meta=meta,
    )


def _active_tenant(request: Request) -> str:
    t = request.session.get("active_tenant")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return DEFAULT_TENANT


def _job_tenant(job) -> str:
    cfg = job.kwargs.get("config", {}) if job.kwargs else {}
    t = cfg.get("tenant")
    return t if isinstance(t, str) and t.strip() else DEFAULT_TENANT


def _ensure_job_tenant(request: Request, job):
    t = _active_tenant(request)
    if _job_tenant(job) != t:
        raise HTTPException(404, "Job not found")


def _parse_bool(v) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "on", "yes", "y")


def _get_last(tenant: str, job_id: str):
    last = exec_mod.LAST_RESULTS.get(job_id)
    if last and (last.get("tenant") == tenant):
        return last

    it = last_results_select(tenant, job_id)
    if it:
        return it

    items = history_select(tenant, job_id, since_ts=None, limit=1)
    if items:
        it2 = items[-1]
        return {"value": it2.get("value"), "ts": it2.get("ts"), "metrics": {}, "error": "", "duration": None}
    return None


def _job_public(job, tenant: str):
    cfg = job.kwargs.get("config", {}) if job.kwargs else {}
    cron = cfg.get("cron", "")
    last = _get_last(tenant, job.id)

    return {
        "id": job.id,
        "tenant": cfg.get("tenant", DEFAULT_TENANT),
        "folder": cfg.get("folder", "") or "",
        "name": cfg.get("name", job.id),
        "description": cfg.get("description"),
        "type": cfg.get("type"),
        "cron": cron,
        "timeout": cfg.get("timeout"),
        "value_regex": cfg.get("value_regex"),
        "retention_days": cfg.get("retention_days", 1),
        "metrics_enabled": bool(cfg.get("metrics_enabled", False)),
        "extra_labels": cfg.get("extra_labels", {}) or {},
        "command": cfg.get("command"),
        "method": cfg.get("method"),
        "url": cfg.get("url"),
        "headers": cfg.get("headers"),
        "body": cfg.get("body"),
        "paused": job.next_run_time is None,
        "last": {"value": last.get("value") if last else None, "ts": last.get("ts") if last else None},
    }


@router.get("/jobs")
def list_jobs(request: Request):
    tenant = _active_tenant(request)
    jobs = exec_mod.scheduler.get_jobs() if exec_mod.scheduler else []
    out = []
    for j in jobs:
        if _job_tenant(j) != tenant:
            continue
        out.append(_job_public(j, tenant))
    return JSONResponse(out)


@router.get("/tenants")
def list_tenants(request: Request):
    tenants = {DEFAULT_TENANT}

    jobs = exec_mod.scheduler.get_jobs() if exec_mod.scheduler else []
    for j in jobs:
        t = _job_tenant(j)
        if t:
            tenants.add(t)

    user = request.session.get("user") or {}
    for t in (user.get("allowed_tenants") or []):
        if isinstance(t, str) and t.strip():
            tenants.add(t.strip())

    return {"tenants": sorted(tenants)}


@router.delete("/tenants/{tenant}")
def delete_tenant(request: Request, tenant: str):
    _require_admin(request)

    tenant = (tenant or "").strip()
    if not tenant:
        raise HTTPException(400, "tenant is required")
    if tenant == DEFAULT_TENANT:
        raise HTTPException(400, f"cannot delete the default tenant ({DEFAULT_TENANT})")

    jobs = exec_mod.scheduler.get_jobs() if exec_mod.scheduler else []
    to_remove = [j.id for j in jobs if _job_tenant(j) == tenant]
    for job_id in to_remove:
        exec_mod.scheduler.remove_job(job_id)
        exec_mod.LAST_RESULTS.pop(job_id, None)

    tenant_access_delete_tenant(tenant)

    user = request.session.get("user") or {}
    if isinstance(user, dict):
        allowed = [t for t in (user.get("allowed_tenants") or []) if t != tenant]
        user["allowed_tenants"] = allowed
        tenant_roles = dict(user.get("tenant_roles") or {})
        tenant_roles.pop(tenant, None)
        user["tenant_roles"] = tenant_roles
        request.session["user"] = user
    else:
        allowed = []

    new_active = request.session.get("active_tenant") or DEFAULT_TENANT
    if new_active == tenant:
        new_active = allowed[0] if allowed else DEFAULT_TENANT
        request.session["active_tenant"] = new_active

    _audit(
        request,
        "tenant.delete",
        tenant=tenant,
        target_type="tenant",
        target_id=tenant,
        ok=True,
        meta={"deleted_job_count": len(to_remove)},
    )

    return {"ok": True, "deleted_jobs": len(to_remove), "active_tenant": new_active}


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    sched = exec_mod.scheduler
    j = sched.get_job(job_id) if sched else None
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)
    tenant = _active_tenant(request)
    return JSONResponse(_job_public(j, tenant))


@router.get("/jobs/{job_id}/view")
def view_job(request: Request, job_id: str):
    """
    Readonly HTML view (RO userlər üçün ideal)
    Dark-mode uyğundur + Back düyməsi işləyir.
    """
    sched = exec_mod.scheduler
    j = sched.get_job(job_id) if sched else None
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    pj = _job_public(j, tenant)

    def esc(x):
        return html.escape("" if x is None else str(x))

    t = esc(pj.get("tenant"))
    folder = esc(pj.get("folder") or "-")
    name = esc(pj.get("name") or "-")
    desc = esc(pj.get("description") or "")
    typ = esc(pj.get("type") or "")
    cron = esc(pj.get("cron") or "")
    timeout = esc(pj.get("timeout") or "")
    regex = esc(pj.get("value_regex") or "")
    retention = esc(pj.get("retention_days") or "")
    paused = "OFF" if pj.get("paused") else "ON"
    metrics = "ON" if pj.get("metrics_enabled") else "OFF"
    lastv = esc((pj.get("last") or {}).get("value"))
    lastts = (pj.get("last") or {}).get("ts")
    lastts = esc(datetime.fromtimestamp(lastts, TZ).isoformat() if lastts else "")

    cmd = esc(pj.get("command") or "")
    method = esc(pj.get("method") or "")
    url = esc(pj.get("url") or "")
    headers = esc(pj.get("headers") or "")
    body = esc(pj.get("body") or "")
    extra_labels_dict = pj.get("extra_labels") or {}
    extra_labels = esc(", ".join(f"{k}={v}" for k, v in extra_labels_dict.items()) or "-")

    command_block = ""
    if typ == "shell":
        copy_btn = (
            "<button class='iconbtn' type='button' "
            "onclick=\"copyText('cmdPre', this)\">📋 Copy</button>"
        )
        command_block = (
            "<div class='k' style='display:flex;justify-content:space-between;align-items:center'>"
            f"<span>Command</span>{copy_btn}</div><pre id='cmdPre'>{cmd}</pre>"
        )

    html_body = f"""<!doctype html>
<html data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CronHub — View Job</title>
  <style>
    :root{{--bg:#fff;--fg:#111827;--muted:#6b7280;--card:#fff;--line:#e5e7eb;--btn:#111827;--btnfg:#fff;--chipbg:#eef2ff;--chipfg:#4338ca}}
    :root[data-theme="dark"]{{--bg:#0b1220;--fg:#e5e7eb;--muted:#9ca3af;--card:#0f172a;--line:#1f2937;--btn:#2563eb;--btnfg:#e5e7eb;--chipbg:#1e293b;--chipfg:#93c5fd}}
    *{{box-sizing:border-box}}
    body{{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial;margin:24px;background:var(--bg);color:var(--fg)}}
    .card{{max-width:980px;border:1px solid var(--line);background:var(--card);border-radius:12px;padding:16px}}
    .row{{display:flex;gap:12px;flex-wrap:wrap}}
    .kv{{min-width:220px;flex:1}}
    .k{{font-size:12px;color:var(--muted);margin-bottom:4px}}
    .v{{font-size:14px;word-break:break-word}}
    pre{{background:var(--bg);border:1px solid var(--line);color:var(--fg);padding:12px;border-radius:10px;overflow:auto}}
    .pill{{display:inline-block;padding:2px 10px;border-radius:999px;background:var(--chipbg);color:var(--chipfg);font-size:12px}}
    .iconbtn{{background:transparent;border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:8px 12px;cursor:pointer;text-decoration:none}}
    .top{{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}}
  </style>
</head>
<body>
  <div class="card">
    <div class="top">
      <h2 style="margin:0">View Job</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <a class="iconbtn" href="/" title="Back">← Back</a>
        <button class="iconbtn" type="button" onclick="toggleTheme()" title="Toggle theme">🌓</button>
      </div>
    </div>

    <div class="row" style="margin-top:12px">
      <div class="kv"><div class="k">Tenant</div><div class="v">{t}</div></div>
      <div class="kv"><div class="k">Folder</div><div class="v">{folder}</div></div>
      <div class="kv"><div class="k">Name</div><div class="v"><strong>{name}</strong></div></div>
    </div>

    <div class="row" style="margin-top:10px">
      <div class="kv"><div class="k">Type</div><div class="v"><span class="pill">{typ}</span></div></div>
      <div class="kv"><div class="k">Cron</div><div class="v">{cron}</div></div>
      <div class="kv"><div class="k">Retention (days)</div><div class="v">{retention}</div></div>
    </div>

    <div class="row" style="margin-top:10px">
      <div class="kv"><div class="k">Timeout</div><div class="v">{timeout}</div></div>
      <div class="kv"><div class="k">Value Regex</div><div class="v">{regex}</div></div>
      <div class="kv"><div class="k">On/Off</div><div class="v">{paused}</div></div>
      <div class="kv"><div class="k">Metrics</div><div class="v">{metrics}</div></div>
      <div class="kv"><div class="k">Extra Labels</div><div class="v">{extra_labels}</div></div>
    </div>

    <div class="row" style="margin-top:10px">
      <div class="kv"><div class="k">Last Value</div><div class="v">{lastv}</div></div>
      <div class="kv"><div class="k">Last Time</div><div class="v">{lastts}</div></div>
    </div>

    <div style="margin-top:12px">
      <div class="k">Description</div>
      <div class="v">{desc}</div>
    </div>

    <div style="margin-top:14px">
      <h3 style="margin:0 0 8px 0">Details</h3>
      {command_block}
      {("<div class='k'>HTTP</div><div class='v'><b>"+method+"</b> "+url+"</div>") if typ=="http" else ""}
      {("<div class='k'>Headers</div><pre>"+headers+"</pre>") if typ=="http" and headers else ""}
      {("<div class='k'>Body</div><pre>"+body+"</pre>") if typ=="http" and body else ""}
    </div>
  </div>

  <script>
    const root = document.documentElement;
    function applyTheme(t) {{
      root.setAttribute('data-theme', t);
      localStorage.setItem('theme', t);
    }}
    function toggleTheme() {{
      const cur = root.getAttribute('data-theme') || 'light';
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    }}
    applyTheme(localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));

    function copyText(id, btn) {{
      const el = document.getElementById(id);
      if (!el) return;
      const text = el.innerText || el.textContent || '';

      function showCopied() {{
        if (!btn) return;
        const orig = btn.textContent;
        btn.textContent = '✅ Copied';
        setTimeout(() => {{ btn.textContent = orig; }}, 1200);
      }}

      function fallbackCopy() {{
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try {{
          document.execCommand('copy');
          showCopied();
        }} catch (e) {{
          alert('Copy failed: ' + e);
        }}
        document.body.removeChild(ta);
      }}

      if (navigator.clipboard && window.isSecureContext) {{
        navigator.clipboard.writeText(text).then(showCopied).catch(fallbackCopy);
      }} else {{
        fallbackCopy();
      }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(html_body)


@router.post("/jobs")
def create_job(
    request: Request,
    name: str = Form(...),
    description: str = Form(None),
    folder: str = Form(None),
    cron: str = Form(...),
    type: str = Form(...),
    command: str = Form(None),
    method: str = Form(None),
    url: str = Form(None),
    headers: str = Form(None),
    body: str = Form(None),
    timeout: str = Form(None),
    value_regex: str = Form(None),
    retention_days: str = Form("1"),
    metrics_enabled: str = Form(None),
    extra_labels: str = Form(None),
):
    _require_write(request)

    tenant = _active_tenant(request)
    t = (type or "").lower()
    if t not in ("shell", "http"):
        raise HTTPException(400, "type must be shell|http")

    try:
        trigger = CronTrigger.from_crontab(cron, timezone=TZ)
    except Exception as e:
        raise HTTPException(400, f"Invalid cron: {e}")

    if description is not None and len(description) > 100:
        raise HTTPException(400, "description must be at most 100 characters")

    try:
        rd = float(retention_days or "1")
        if rd < 0:
            raise ValueError()
    except Exception:
        raise HTTPException(400, "retention_days must be a non-negative number")

    job_id = uuid.uuid4().hex
    cfg = {
        "id": job_id,
        "tenant": tenant,
        "folder": (folder or "").strip(),
        "name": name,
        "description": description or "",
        "type": t,
        "cron": cron,
        "timeout": timeout,
        "value_regex": value_regex,
        "retention_days": rd,
        "metrics_enabled": _parse_bool(metrics_enabled),
        "extra_labels": _parse_extra_labels(extra_labels),
    }

    if t == "shell":
        if command is None:
            raise HTTPException(400, "command is required (shell)")
        cfg["command"] = command
    else:
        if not method or not url:
            raise HTTPException(400, "method and url are required (http)")
        cfg.update({"method": method, "url": url, "headers": headers, "body": body})

    exec_mod.scheduler.add_job(
        exec_mod.execute_job,
        trigger=trigger,
        id=job_id,
        args=[job_id],
        kwargs={"config": cfg},
        replace_existing=False,
    )

    _audit(request, "job.create", tenant=tenant, target_type="job", target_id=job_id, ok=True, after=cfg)
    return {"ok": True, "id": job_id}


@router.put("/jobs/{job_id}")
def update_job(
    request: Request,
    job_id: str,
    name: str = Form(None),
    description: str = Form(None),
    folder: str = Form(None),
    cron: str = Form(None),
    type: str = Form(None),
    command: str = Form(None),
    method: str = Form(None),
    url: str = Form(None),
    headers: str = Form(None),
    body: str = Form(None),
    timeout: str = Form(None),
    value_regex: str = Form(None),
    retention_days: str = Form(None),
    metrics_enabled: str = Form(None),
    extra_labels: str = Form(None),
):
    _require_write(request)

    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    before_cfg = dict(j.kwargs.get("config", {}))
    cfg = dict(j.kwargs.get("config", {}))

    if name is not None:
        cfg["name"] = name

    if description is not None:
        if len(description) > 100:
            raise HTTPException(400, "description must be at most 100 characters")
        cfg["description"] = description

    if folder is not None:
        cfg["folder"] = (folder or "").strip()

    if cron is not None:
        cfg["cron"] = cron

    if type is not None:
        cfg["type"] = type.lower()

    if timeout is not None:
        cfg["timeout"] = timeout

    if value_regex is not None:
        cfg["value_regex"] = value_regex

    if retention_days is not None:
        try:
            rd = float(retention_days)
            if rd < 0:
                raise ValueError()
            cfg["retention_days"] = rd
        except Exception:
            raise HTTPException(400, "retention_days must be a non-negative number")

    if metrics_enabled is not None:
        cfg["metrics_enabled"] = _parse_bool(metrics_enabled)

    if extra_labels is not None:
        cfg["extra_labels"] = _parse_extra_labels(extra_labels)

    t = cfg.get("type")
    if t not in ("shell", "http"):
        raise HTTPException(400, "type must be shell|http")

    if t == "shell":
        if command is not None:
            cfg["command"] = command
        if not cfg.get("command", "").strip():
            raise HTTPException(400, "command is required (shell)")
        for k in ("method", "url", "headers", "body"):
            cfg.pop(k, None)
    else:
        if method is not None:
            cfg["method"] = method
        if url is not None:
            cfg["url"] = url
        if headers is not None:
            cfg["headers"] = headers
        if body is not None:
            cfg["body"] = body
        if not cfg.get("method") or not cfg.get("url"):
            raise HTTPException(400, "method and url are required (http)")
        cfg.pop("command", None)

    trigger = None
    if cron is not None:
        try:
            trigger = CronTrigger.from_crontab(cfg["cron"], timezone=TZ)
        except Exception as e:
            raise HTTPException(400, f"Invalid cron: {e}")

    if trigger:
        exec_mod.scheduler.modify_job(job_id, trigger=trigger, kwargs={"config": cfg})
    else:
        exec_mod.scheduler.modify_job(job_id, kwargs={"config": cfg})

    _audit(
        request,
        "job.update",
        tenant=_active_tenant(request),
        target_type="job",
        target_id=job_id,
        ok=True,
        before=before_cfg,
        after=cfg,
    )

    return {"ok": True, "id": job_id, "name": cfg.get("name"), "metrics_enabled": bool(cfg.get("metrics_enabled", False))}


@router.post("/jobs/{job_id}/metrics/{state}")
def set_job_metrics(request: Request, job_id: str, state: str):
    _require_write(request)

    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    cfg = dict((j.kwargs or {}).get("config", {}))
    before_enabled = bool(cfg.get("metrics_enabled", False))

    s = (state or "").lower()
    if s not in ("on", "off"):
        raise HTTPException(400, "state must be on|off")

    cfg["metrics_enabled"] = (s == "on")
    exec_mod.scheduler.modify_job(job_id, kwargs={"config": cfg})

    state_msg = "enabled" if s == "on" else "disabled"
    _audit(
        request,
        "job.metrics_toggle",
        tenant=tenant,
        target_type="job",
        target_id=job_id,
        ok=True,
        msg=f"metrics {state_msg}",
        before={"metrics_enabled": before_enabled},
        after={"metrics_enabled": bool(cfg["metrics_enabled"])},
        meta={"state": s},
    )

    return {"ok": True, "id": job_id, "metrics_enabled": cfg["metrics_enabled"]}


@router.post("/jobs/{job_id}/run")
def run_now(request: Request, job_id: str):
    _require_write(request)

    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    cfg = j.kwargs.get("config", {}) if j.kwargs else {}

    exec_mod.scheduler.add_job(
        exec_mod.execute_job,
        trigger="date",
        run_date=datetime.now(TZ),
        args=[job_id],
        kwargs={"config": cfg},
    )

    _audit(request, "job.run_now", tenant=tenant, target_type="job", target_id=job_id, ok=True)
    return {"ok": True}


@router.post("/jobs/{job_id}/duplicate")
def duplicate_job(request: Request, job_id: str):
    _require_write(request)

    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    src_cfg = dict(j.kwargs.get("config", {})) if j.kwargs else {}

    try:
        trigger = CronTrigger.from_crontab(src_cfg.get("cron", "* * * * *"), timezone=TZ)
    except Exception as e:
        raise HTTPException(400, f"Invalid cron: {e}")

    new_id = uuid.uuid4().hex
    new_cfg = dict(src_cfg)
    new_cfg["id"] = new_id
    new_cfg["name"] = f'{src_cfg.get("name", job_id)} (copy)'

    # Duplicated jobs start paused so they don't run before you've had a
    # chance to review/edit them.
    exec_mod.scheduler.add_job(
        exec_mod.execute_job,
        trigger=trigger,
        id=new_id,
        args=[new_id],
        kwargs={"config": new_cfg},
        replace_existing=False,
        next_run_time=None,
    )

    _audit(
        request,
        "job.duplicate",
        tenant=tenant,
        target_type="job",
        target_id=new_id,
        ok=True,
        after=new_cfg,
        meta={"duplicated_from": job_id},
    )
    return {"ok": True, "id": new_id}


@router.post("/jobs/{job_id}/pause")
def pause_job(request: Request, job_id: str):
    _require_write(request)

    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    exec_mod.scheduler.pause_job(job_id)

    _audit(request, "job.pause", tenant=tenant, target_type="job", target_id=job_id, ok=True)
    return {"ok": True}


@router.post("/jobs/{job_id}/resume")
def resume_job(request: Request, job_id: str):
    _require_write(request)

    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    exec_mod.scheduler.resume_job(job_id)

    _audit(request, "job.resume", tenant=tenant, target_type="job", target_id=job_id, ok=True)
    return {"ok": True}


@router.delete("/jobs/{job_id}")
def delete_job(request: Request, job_id: str):
    _require_write(request)

    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    cfg_before = dict(j.kwargs.get("config", {})) if j.kwargs else {}

    exec_mod.scheduler.remove_job(job_id)
    exec_mod.LAST_RESULTS.pop(job_id, None)

    _audit(request, "job.delete", tenant=tenant, target_type="job", target_id=job_id, ok=True, before=cfg_before)
    return {"ok": True}


@router.get("/jobs/{job_id}/history")
def get_history(
    request: Request,
    job_id: str,
    since: float | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
):
    j = exec_mod.scheduler.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    _ensure_job_tenant(request, j)

    tenant = _active_tenant(request)
    items = history_select(tenant, job_id, since_ts=since, limit=limit)
    return {"job_id": job_id, "items": items}


@router.get("/audit")
def get_audit(request: Request, limit: int = 200):
    # Cronhub-RO audit görməməlidir, BA da audit görməməlidir.
    # yalnız DevOps(admin) görür.
    _require_admin(request)
    tenant = _active_tenant(request)
    return {"items": audit_list(tenant=tenant, limit=limit)}


@router.get("/metrics")
def metrics():
    if exec_mod.scheduler is None:
        return Response("cronhub_up 1\ncronhub_scheduler_running 0\n", media_type="text/plain; version=0.0.4; charset=utf-8")

    lines = []
    lines.append("cronhub_up 1")
    lines.append("cronhub_scheduler_running 1")

    for job in exec_mod.scheduler.get_jobs():
        job_id = job.id
        cfg = job.kwargs.get("config", {}) if job.kwargs else {}

        if not bool(cfg.get("metrics_enabled", False)):
            continue

        name = cfg.get("name", job_id)
        tenant = cfg.get("tenant", DEFAULT_TENANT)
        folder = cfg.get("folder") or ""

        last = _get_last(tenant, job_id)
        if not last:
            continue

        extra_labels = cfg.get("extra_labels") or {}
        extra_labels_str = "".join(
            f',{k}="{exec_mod._esc(str(v))}"' for k, v in extra_labels.items()
        )
        base_labels = (
            f'job_id="{job_id}",job_name="{exec_mod._esc(name)}",'
            f'tenant="{exec_mod._esc(str(tenant))}",folder="{exec_mod._esc(str(folder))}"'
            f'{extra_labels_str}'
        )

        success = 0 if last.get("error") else 1
        lines.append(f"cronhub_job_success{{{base_labels}}} {success}")

        if last.get("duration") is not None:
            lines.append(f'cronhub_job_duration_seconds{{{base_labels}}} {float(last["duration"])}')

        if last.get("value") is not None:
            lines.append(f'cronhub_job_value{{{base_labels}}} {float(last["value"])}')

        metrics_map = last.get("metrics") or {}
        for k, v in metrics_map.items():
            if k == "value":
                continue
            if not isinstance(v, (int, float)):
                continue
            sub = exec_mod._esc(str(k))
            lines.append(f'cronhub_job_value{{{base_labels},subjob="{sub}"}} {float(v)}')

        list_metrics = last.get("list_metrics") or []
        for it in list_metrics:
            park = it.get("list")
            subjob = it.get("subjob")
            v = it.get("value")
            if park is None or subjob is None or v is None:
                continue
            if not isinstance(v, (int, float)):
                continue
            park_lbl = exec_mod._esc(str(park))
            sub_lbl = exec_mod._esc(str(subjob))
            lines.append(f'cronhub_job_value{{{base_labels},subjob="{sub_lbl}",list="{park_lbl}"}} {float(v)}')

    body = "\n".join(lines) + "\n"
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")
