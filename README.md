# Intent Normaliser Service (Phase 0)

Phase 0 skeleton for the Intent Normaliser Service. This includes a FastAPI app, mandatory Postgres persistence (append-only), Alembic migrations, and Docker-first development.

## Workspace

Open `intent_normaliser.code-workspace` in VS Code to load this repo plus the related `notion_gateway` and `action_relay` repos. Keep those folders next to this one so the relative paths resolve on each machine.

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
- `CONTEXT_API_BASE_URL` (default: unset)
- `CONTEXT_API_BEARER_TOKEN` (default: unset)
- `CONTEXT_API_PROJECT_SEARCH_PATH` (default: /v1/projects/search)
- `CONTEXT_API_TIMEOUT_SECONDS` (default: 5)
- `VERSION` (default: 0.0.0)
- `GIT_SHA` (default: unknown)

## Endpoints

### GET /health

Returns `{ "status": "ok" }` when the database is reachable. Returns `503` when it is not.

### GET /version

Returns `{ "version", "git_sha", "artifact_version" }`.

### POST /v1/intents

Requires bearer auth. Writes a `received` artifact row and returns `NOT_IMPLEMENTED` for normalisation.

### POST /v1/actions

Requires bearer auth. Writes a `received` artifact row and returns `NOT_IMPLEMENTED` for execution.

## Curl examples

```bash
curl -s http://localhost:8000/health
```

```bash
curl -s http://localhost:8000/version
```

```bash
curl -s -X POST http://localhost:8000/v1/intents \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"kind":"intent","intent_type":"noop"}'
```

```bash
curl -s -X POST http://localhost:8000/v1/actions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"kind":"action","action":"notion.tasks.create"}'
```

## Tests

Run tests inside Docker:

```bash
docker compose run --rm api pytest
```
