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
and branch (see `.env.example`), then restart. The token needs `api` scope on
the project — a Project Access Token with the Developer role is enough. Keep it
in `.env`; it is never returned by the API or shown in the UI.

### Layout

One JSON file per job:

```
<prefix>/<tenant>/<folder>/<name>.json
```

One file per job (rather than a single export) keeps diffs readable and lets
two people edit different jobs without conflicting.

### CronHub → GitLab

Creating, editing, duplicating or deleting a job commits the change
automatically. The commit is authored by the CronHub user who made it and says
what changed:

```
update: Avis/Reports/sql-count #12 by huseyn

Action: update
Actor:  huseyn
Tenant: Avis
Folder: Reports
Job:    sql-count #12
```

Renaming a job, or moving it to another folder, deletes the old path in the
same commit so the repo doesn't accumulate stale files.

A GitLab outage never blocks work in CronHub: the job is still created or
updated locally, and the failure is reported in the response rather than raised.
Use **Push all to GitLab** afterwards to reconcile anything that didn't make it.

### GitLab → CronHub

The **GitLab** panel (admin only) has:

- **Push all to GitLab** — mirrors every job in one commit. Use it to seed a new
  repo or to re-align after edits outside CronHub.
- **Pull from GitLab** — applies the repo's files, creating and updating jobs.
  It never deletes.
- **Pull + prune** — also removes jobs whose file is gone from the repo. This
  deletes live jobs, so it is a separate, deliberate action.

Pull is idempotent and does not commit back, so it can't loop. `short_id` and
`created_at` are owned by CronHub: if a hand-written file omits them, the values
already on the job are kept rather than reallocated.

Conflicts are last-write-wins — whichever side acts last wins. There is no
merge step, so avoid editing the same job on both sides at once.
