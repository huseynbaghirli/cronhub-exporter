##. cronhub/scheduler/executor.py


import json, os, re, subprocess, tempfile, time, hmac
from datetime import datetime

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import logging
logger = logging.getLogger("uvicorn.error")


from ..core.config import TZ, jobstores, job_defaults
from .history import history_insert, history_prune, last_results_upsert

scheduler: AsyncIOScheduler | None = None
LAST_RESULTS: dict[str, dict] = {}

def _now_ts() -> float:
    return datetime.now(TZ).timestamp()

def _try_parse_json(text: str):
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        return None

def _coerce_float(x):
    try:
        if isinstance(x, bool) or x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str) and x.strip() != "":
            return float(x.strip())
    except Exception:
        return None
    return None


def _extract_number(text: str, regex: str | None):
    if not isinstance(text, str):
        return None
    if regex:
        try:
            m = re.search(regex, text, re.MULTILINE)
            if m:
                if "value" in m.groupdict():
                    return float(m.group("value"))
                elif m.lastindex and m.group(1) is not None:
                    return float(m.group(1))
                else:
                    return float(m.group(0))
        except Exception:
            pass
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if m:
        try:
            return float(m.group(0))
        except Exception:
            return None
    return None
def _infer_list_label_key_from_payload(payload) -> str | None:
    # payload: list[dict]
    for row in payload:
        if isinstance(row, dict) and row:
            # dict-in ilk key-i (python 3.7+ insertion order saxlayır)
            return next(iter(row.keys()))
    return None


def _list_payload_to_list_metrics(payload, list_label_key: str | None = None):
    if not isinstance(payload, list):
        return []

    # ✅ label key verilməyibsə, payload-dan avtomatik tap
    if list_label_key is None:
        list_label_key = _infer_list_label_key_from_payload(payload)

    if not list_label_key:
        return []

    out = []
    for row in payload:
        if not isinstance(row, dict):
            continue

        label_val = row.get(list_label_key)
        if label_val is None:
            continue

        for k, v in row.items():
            if k == list_label_key:
                continue

            fv = _coerce_float(v)
            if fv is None:
                continue

            out.append({"list": str(label_val), "subjob": str(k), "value": float(fv)})

    return out


def _run_multiline_bash(script_text: str, timeout_s: int) -> tuple[str, str]:
    fd, path = tempfile.mkstemp(prefix="cronhub_", suffix=".sh")
    try:

        # ✅ CRLF -> LF
        script_text = (script_text or "").replace("\r\n", "\n").replace("\r", "\n")

        with os.fdopen(fd, "w") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write("set -euo pipefail\n")
            f.write("export PS4='+ [${LINENO}] '\n")
            f.write("set -x\n")
            f.write(
                "trap 'rc=$?; echo \"[cronhub] ERROR rc=$rc line=$LINENO cmd=$BASH_COMMAND\" >&2' ERR\n"
            )
            f.write("\n")
            f.write(script_text.rstrip() + "\n")

        os.chmod(path, 0o700)

        logger.warning("[cronhub] RUN script=%s timeout=%ss", path, timeout_s)

        p = subprocess.run(
            ["/usr/bin/env", "bash", path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )

        # 🔥 BURASI ƏSAS YERDİR
        logger.warning("[cronhub] rc=%s script=%s", p.returncode, path)

        if p.stdout:
            logger.warning("[cronhub] STDOUT:\n%s", p.stdout)

        if p.stderr:
            logger.warning("[cronhub] STDERR:\n%s", p.stderr)

        stdout = (p.stdout or "").strip()
        stderr = (p.stderr or "").strip()

        stderr = (f"[cronhub] rc={p.returncode}\n" + stderr).strip()

        return stdout, stderr

    except subprocess.TimeoutExpired:
        logger.error("[cronhub] TIMEOUT after %ss script=%s", timeout_s, path)
        return "", f"[cronhub] TIMEOUT after {timeout_s}s"

    finally:
        try:
            os.remove(path)
        except Exception:
            pass



def _esc(s: str, keep_quotes: bool = True) -> str:
    s = s.replace("\\", "\\\\").replace("\n", "\\n")
    if not keep_quotes:
        s = s.replace('"', '\\"')
    return s

# =========================
# Job execution
# =========================
def execute_job(job_id: str, config: dict):
    name = config.get("name", job_id)
    job_type = config.get("type")
    timeout_s = int(config.get("timeout", 60 if job_type == "http" else 3600))
    value_regex = config.get("value_regex")
    retention_days = float(config.get("retention_days", 1))
    tenant = config.get("tenant") or "business"

    started = time.time()
    output, err = "", ""
    try:
        if job_type == "shell":
            cmd = config.get("command") or ""
            if not cmd.strip():
                raise ValueError("Shell command is empty")
            output, err = _run_multiline_bash(cmd, timeout_s)

            logger.warning(
                "[cronhub] JOB %s finished type=%s output_len=%s error_len=%s",
                job_id,
                job_type,
                len(output or ""),
                len(err or ""),
            )

        elif job_type == "http":
            method = (config.get("method") or "GET").upper()
            url = config.get("url")
            if not url:
                raise ValueError("URL is empty")
            headers = {}
            headers_text = config.get("headers")
            if headers_text:
                try:
                    headers = json.loads(headers_text)
                except Exception:
                    for line in headers_text.splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            headers[k.strip()] = v.strip()
            resp = requests.request(method, url, headers=headers, data=config.get("body"), timeout=timeout_s)
            output = (resp.text or "").strip()
        else:
            raise ValueError(f"Unknown job type: {job_type}")

        # =========================
        # Parse output (JSON + fallback number)
        # =========================
        payload = _try_parse_json(output)

        metrics: dict[str, float] = {}
        list_metrics: list[dict] = []
        val = None

        # 1) JSON gəlibsə: çoxlu metric topla
        # 1) JSON dict gəlibsə: çoxlu metric topla
        if isinstance(payload, dict):
            for k, v in payload.items():
                fv = _coerce_float(v)
                if fv is not None:
                    metrics[k] = fv

            # primary value: json["value"] varsa onu götür
            if "value" in payload:
                val = _coerce_float(payload.get("value"))

        # 1b) JSON list gəlibsə: dinamik list_metrics yığ
        if isinstance(payload, list):
            list_metrics = _list_payload_to_list_metrics(payload)  # ✅ tam dinamik



    # 2) JSON yoxdursa (və ya value çıxmadısa): köhnə logic
        if val is None:
            val = _extract_number(output, value_regex)

        # 3) primary value varsa metrics-ə də yaz (prom üçün)
        if val is not None and "value" not in metrics:
            metrics["value"] = float(val)

        out_short = (output or "")[:8192]
        nowts = _now_ts()


        LAST_RESULTS[job_id] = {
            "tenant": tenant,   # ✅ əlavə
            "job_id": job_id, "job_name": name, "type": job_type,
            "value": val,
            "metrics": metrics,
            "list_metrics": list_metrics,
            "json": payload,
            "output_preview": out_short,
            "parsed_with_regex": bool(value_regex),
            "ts": nowts, "duration": time.time() - started, "error": err,
        }

        last_results_upsert(
            tenant=tenant,
            job_id=job_id,
            ts=nowts,
            value=val,
            metrics=metrics,
            error=err,
            duration=time.time() - started,
            output_preview=out_short,
        )

        if val is not None:
            history_insert(tenant, job_id, nowts, float(val), out_short)
            cutoff = nowts - retention_days * 86400.0
            history_prune(tenant, job_id, cutoff)
            

    except Exception as e:
        out_short = (output or "")[:8192]
        LAST_RESULTS[job_id] = {
            "job_id": job_id, "job_name": name, "type": job_type,
            "value": None, "output_preview": out_short,
            "parsed_with_regex": bool(value_regex),
            "ts": _now_ts(), "duration": time.time() - started,
            "error": f"{type(e).__name__}: {e}",
        }

        last_results_upsert(
            tenant=tenant,
            job_id=job_id,
            ts=_now_ts(),
            value=None,
            metrics={},
            error=f"{type(e).__name__}: {e}",
            duration=time.time() - started,
            output_preview=out_short,
        )



# cronhub/scheduler/executor.py (yalnız aşağı hissəni belə et)

from apscheduler.triggers.cron import CronTrigger
from ..core.config import TZ, jobstores, job_defaults, AUDIT_RETENTION_DAYS
from .audit import audit_prune

def _prune_audit_task():
    deleted = audit_prune(AUDIT_RETENTION_DAYS, tenant=None)
    logger.warning(
        "[cronhub] audit_prune deleted=%s retention_days=%s",
        deleted,
        AUDIT_RETENTION_DAYS,
    )

def init_scheduler():
    global scheduler
    scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults, timezone=TZ)
    scheduler.start()

    # hər gün 03:15-də audit prune
    scheduler.add_job(
        _prune_audit_task,
        trigger=CronTrigger.from_crontab("15 3 * * *", timezone=TZ),
        id="system.audit_prune",
        replace_existing=True,
    )

def shutdown_scheduler():
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


def shutdown_scheduler():
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
