# Intent Normaliser Service (Phase 0)

Phase 0 skeleton for the Intent Normaliser Service. This includes a FastAPI app, mandatory Postgres persistence (append-only), Alembic migrations, and Docker-first development.

## Quickstart (Docker)

Build and start Postgres, run migrations, and start the API:

```bash
docker compose up --build
```

Service will be available at `http://localhost:8000`.

## Configuration

Required env vars:

- `DATABASE_URL`
- `INTENT_SERVICE_TOKEN`

Optional:

- `USER_TIMEZONE` (default: Europe/London)
- `MIN_CONFIDENCE_TO_WRITE` (default: 0.75)
- `MAX_INFERRED_FIELDS` (default: 2)
- `EXECUTE_ACTIONS` (default: false)
- `VERSION` (default: 0.0.0)
- `GIT_SHA` (default: unknown)

## Endpoints

### GET /health

Returns `{ "status": "ok" }` when the database is reachable. Returns `503` when it is not.

### GET /version

Returns `{ "version", "git_sha", "artifact_version" }`.

### POST /v1/ingest/intent

Requires bearer auth. Writes a `received` artifact row and returns `NOT_IMPLEMENTED` for normalisation.

### POST /v1/ingest/action

Requires bearer auth. Writes a `received` artifact row and returns `NOT_IMPLEMENTED` for execution.

## Curl examples

```bash
curl -s http://localhost:8000/health
```

```bash
curl -s http://localhost:8000/version
```

```bash
curl -s -X POST http://localhost:8000/v1/ingest/intent \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"kind":"intent","intent_type":"noop"}'
```

```bash
curl -s -X POST http://localhost:8000/v1/ingest/action \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"kind":"action","action":"notion.tasks.create"}'
```

## Tests

Run tests inside Docker:

```bash
docker compose run --rm api pytest
```
