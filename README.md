# cronjob-app

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn cronhub.main:app --host 0.0.0.0 --port 8000 --reload

```

## Configuration (.env)

All configuration (admin password, Keycloak client secret, export token, etc.)
is read from environment variables in `cronhub/core/config.py`. For local
development:

```
cp .env.example .env
# edit .env and fill in real values
```

`.env` is git-ignored and must never be committed. Since the app doesn't load
`.env` automatically, export it before running uvicorn:

```
export $(grep -v '^#' .env | xargs)
uvicorn cronhub.main:app --host 0.0.0.0 --port 8000 --reload
```

Or, when running with Docker: `docker run --env-file .env ...`

In production (k8s), values come from the `cronhub-env` Secret referenced in
`k8s/Deployment.yaml`, not from a `.env` file.

**Whenever you add a new environment variable to `cronhub/core/config.py`,
add it to `.env.example` too** (with a placeholder value, no real secrets) so
the list stays up to date.

## Deploy (Docker)

```bash
git pull

docker build -t <your-registry>/cronhub-exporter:latest .

docker stop cronhub
docker rm cronhub

docker run -d \
  --name cronhub \
  -p 8060:8000 \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  <your-registry>/cronhub-exporter:latest
```

Replace `<your-registry>` with your actual image registry/tag. `.env` must
exist in the working directory (see Configuration above) and `data/` is the
persisted volume (job history, sqlite DB, the auto-generated SSH keypair).

## GitLab sync (GitOps)

Jobs can be mirrored into a GitLab repo, so a job definition lives in git and
can be changed from either side.

Enable it by setting `CRONHUB_GITLAB_ENABLED=true` plus the project id, token
and base branch (see `.env.example`), then restart. The token needs `api` scope
on the project — a Project Access Token with the Developer role is enough. Keep
it in `.env`; it is never returned by the API or shown in the UI.

### Layout: one branch per tenant

Each tenant gets its own branch, `tenant/<slug>` by default, and inside that
branch jobs are one JSON file each:

```
tenant/ops-team                 <- branch = tenant
├── .cronhub/tenant.json        <- the exact tenant name
└── jobs/                       <- CRONHUB_GITLAB_PATH_PREFIX
    ├── reports/
    │   ├── pgsql-count.json
    │   └── oracle-debt.json
    ├── audit/
    │   └── register.json
    └── standalone.json         <- job with no folder
```

The tenant is the branch, so it isn't repeated as a directory. Nested folders
(`Payments/Reports`) become real nested directories.

Branch names have to be slugs, and slugging is lossy — "Ops Team" and "ops-team"
would collapse together — so every tenant branch carries
`.cronhub/tenant.json` with the exact tenant name. That manifest is what a pull
reads back, never the branch name.

### CronHub → GitLab

Creating a tenant creates its branch. Creating, editing, duplicating or deleting
a job commits to that tenant's branch. The commit is authored by the CronHub
user who made it and says what changed:

```
update: acme/reports/sql-count #12 by jdoe

Action: update
Actor:  jdoe
Tenant: acme
Folder: reports
Job:    sql-count #12
```

Renaming a job or moving it between folders deletes the old path in the same
commit. Moving a job to a different tenant also removes it from the old
tenant's branch, in a commit of its own.

A GitLab outage never blocks work in CronHub: the job is still created or
updated locally, and the failure is reported in the response rather than raised.
Use **Push all to GitLab** afterwards to reconcile anything that didn't make it.

### GitLab → CronHub

The **GitLab** panel (admin only) has:

- **Push all to GitLab** — mirrors every job into its tenant's branch, one
  commit per tenant. Use it to seed a new repo or to re-align after edits
  outside CronHub.
- **Pull from GitLab** — walks every tenant branch, registers any tenant that is
  new, and applies that branch's job files. It creates and updates, never
  deletes.
- **Pull + prune** — also removes jobs whose file is gone from the repo. This
  deletes live jobs, so it is a separate, deliberate action. Pruning only
  touches tenants that actually have a branch, so a tenant that was never
  pushed can't be wiped by it.

Creating a branch named `tenant/<something>` in GitLab is enough to make the
tenant appear in CronHub on the next pull, even before it has any jobs.

Pull is idempotent and does not commit back, so it can't loop. `short_id` and
`created_at` are owned by CronHub: if a hand-written file omits them, the values
already on the job are kept rather than reallocated.

Deleting a tenant in CronHub leaves its branch in place, because a branch holds
history — which means the next pull brings that tenant back. The response says
so explicitly. Pass `?delete_branch=1` to remove the branch as well.

Conflicts are last-write-wins — whichever side acts last wins. There is no
merge step, so avoid editing the same job on both sides at once.

### Migrating an existing export

If your jobs are already running in CronHub, just enable sync and press
**Push all to GitLab** — it groups jobs by tenant and creates each branch and
file for you. Nothing else is needed.

To seed a repo from an export file alone (`/admin/export/jobs.json`, the single
`{"items": [...]}` shape), split it into the new layout offline:

```bash
python3 scripts/export_to_tree.py cronhub_jobs_export.json out/
```

That writes `out/<tenant-slug>/` per tenant, each containing the tenant manifest
and the job files, ready to push to `tenant/<slug>`. Jobs sharing a name inside
one folder — possible in older exports, before the uniqueness check — are kept
as `<name>-2.json` rather than silently overwritten, and reported at the end.
