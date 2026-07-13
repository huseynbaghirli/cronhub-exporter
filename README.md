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
