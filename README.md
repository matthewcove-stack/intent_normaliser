# Intent Normaliser Service

FastAPI service for intent ingestion, normalization, clarification handling, and append-only artifact persistence. Action execution for intents is available when `EXECUTE_ACTIONS=true` and gateway credentials are set.

## Workspace

Open `intent_normaliser.code-workspace` in VS Code to load this repo plus the related `notion_gateway` and `action_relay` repos. Keep those folders next to this one so the relative paths resolve on each machine.

## Quickstart (Docker)

Build and start Postgres, run migrations, and start the API:

```bash
docker compose up --build
```

Service will be available at `http://localhost:8000` for host access. For container-to-container calls, use service names (for example `http://api:8000`) and avoid localhost.

## Configuration

Required env vars:

- `DATABASE_URL`
- `INTENT_SERVICE_TOKEN`

Optional:

- `USER_TIMEZONE` (default: Europe/London)
- `MIN_CONFIDENCE_TO_WRITE` (default: 0.75)
- `MAX_INFERRED_FIELDS` (default: 2)
- `EXECUTE_ACTIONS` (default: false)
- `GATEWAY_BASE_URL` (default: unset, use a Docker service name when running inside containers)
- `GATEWAY_BEARER_TOKEN` (default: unset)
- `GATEWAY_TASKS_CREATE_PATH` (default: /v1/notion/tasks/create)
- `GATEWAY_TASKS_UPDATE_PATH` (default: /v1/notion/tasks/update)
- `GATEWAY_TIMEOUT_SECONDS` (default: 15)
- `CONTEXT_API_BASE_URL` (default: unset, use a Docker service name when running inside containers)
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

Requires bearer auth. Writes a `received` artifact row, normalizes the intent, and returns one of:

- `ready` with a plan
- `needs_clarification` with a clarification payload
- `rejected` with an error object (`error.code`, `error.message`, `error.details`)

Additional artifacts are written for each outcome.

### GET /v1/intents/{intent_id}

Requires bearer auth. Returns the current intent status and latest plan/clarification state.

### POST /v1/actions

Requires bearer auth. Writes a `received` artifact row and returns `accepted`. Execution is not implemented yet for action packets.

### GET /v1/clarifications?status=open

Requires bearer auth. Returns open clarifications for the actor (if provided).

### POST /v1/clarifications/{clarification_id}/answer

Requires bearer auth. Submits an answer and resumes normalization.

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
  -d '{"kind":"intent","intent_type":"create_task","fields":{"title":"Write spec","project":"Sagitta loft"}}'
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
