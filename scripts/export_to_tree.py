#!/usr/bin/env python3
"""Convert a legacy CronHub export into the per-tenant, per-job layout.

An export from /admin/export/jobs.json is a single file holding every job:

    {"items": [ {...job...}, {...job...} ]}

GitLab sync instead keeps one branch per tenant, and inside it one JSON file
per job. This script does that split offline, so you can inspect the result
and push each tenant's directory to its branch by hand.

If your jobs are already live in CronHub you do not need this: enable sync and
press "Push all to GitLab", which creates the branches and files for you. This
is for seeding a repo from an export file alone.

Usage:
    python3 scripts/export_to_tree.py cronhub_jobs_export.json out/
    python3 scripts/export_to_tree.py export.json out/ --prefix jobs

Produces:
    out/<tenant-slug>/.cronhub/tenant.json      <- exact tenant name
    out/<tenant-slug>/<prefix>/<folder>/<job>.json

Each out/<tenant-slug>/ is the content of that tenant's branch.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cronhub.core.gitlab import _SYNCED_KEYS, _slug_path, tenant_slug  # noqa: E402


def job_relpath(cfg: dict, prefix: str) -> str:
    folder = "/".join(_slug_path(p) for p in (cfg.get("folder") or "").split("/") if p.strip())
    name = _slug_path(cfg.get("name") or cfg.get("id") or "job")
    parts = [prefix] if prefix else []
    if folder:
        parts.append(folder)
    parts.append(f"{name}.json")
    return os.path.join(*[p for p in parts if p])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="path to the exported jobs JSON")
    ap.add_argument("outdir", help="directory to write the tree into")
    ap.add_argument("--prefix", default="jobs",
                    help="path prefix inside each branch (default: jobs)")
    args = ap.parse_args()

    with open(args.export, encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        print("error: expected {\"items\": [...]} or a top-level list", file=sys.stderr)
        return 1

    by_tenant: dict[str, list[dict]] = {}
    skipped = 0
    for cfg in items:
        if not isinstance(cfg, dict) or not cfg.get("id"):
            skipped += 1
            continue
        by_tenant.setdefault(cfg.get("tenant") or "business", []).append(cfg)

    written = 0
    collisions = []
    for tenant, jobs in sorted(by_tenant.items()):
        slug = tenant_slug(tenant)
        root = os.path.join(args.outdir, slug)

        manifest_dir = os.path.join(root, ".cronhub")
        os.makedirs(manifest_dir, exist_ok=True)
        with open(os.path.join(manifest_dir, "tenant.json"), "w", encoding="utf-8") as f:
            json.dump({"tenant": tenant}, f, ensure_ascii=False, indent=2)
            f.write("\n")

        seen: dict[str, str] = {}
        for cfg in jobs:
            rel = job_relpath(cfg, args.prefix)
            # Two jobs with the same name in the same folder would land on the
            # same file. CronHub forbids that now, but older exports predate
            # the check - keep both and report rather than silently overwrite.
            if rel in seen:
                base, ext = os.path.splitext(rel)
                n = 2
                while f"{base}-{n}{ext}" in seen:
                    n += 1
                collisions.append(f"{tenant}: {rel} (kept as {base}-{n}{ext})")
                rel = f"{base}-{n}{ext}"
            seen[rel] = cfg["id"]

            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            out = {k: cfg[k] for k in _SYNCED_KEYS if k in cfg}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            written += 1

        print(f"  {tenant}  ->  {root}/  ({len(jobs)} job(s), branch: tenant/{slug})")

    print(f"\n{written} job file(s) across {len(by_tenant)} tenant(s) written to {args.outdir}/")
    if skipped:
        print(f"{skipped} item(s) skipped (no job id)")
    if collisions:
        print(f"\n{len(collisions)} name collision(s) - same name in the same folder:")
        for c in collisions:
            print(f"  {c}")

    print("\nNext: for each tenant directory, push its contents to that tenant's branch, e.g.")
    print("  git checkout --orphan tenant/<slug> && git rm -rf . \\")
    print("    && cp -r <outdir>/<slug>/. . && git add -A && git commit && git push -u origin HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
