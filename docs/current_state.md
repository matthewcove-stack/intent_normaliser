# intent_normaliser — Current State (Authoritative for this repo)

## What works today
- FastAPI service with Postgres persistence and Alembic migrations
- `POST /v1/intents` normalises intent, persists artifacts, and (when `EXECUTE_ACTIONS=true`) executes Notion task create/update via notion_gateway
- Execution responses include `details.notion_task_id` and `details.request_id` on success; failures include `error.code`, `error.message`, and `error.details.status_code`
- Clarification endpoints exist
- Tests run in Docker (`pytest`)

## What is incomplete / risky
- `POST /v1/actions` is still a stub (Phase 2+)
- End-to-end execution relies on correct gateway configuration (base URL + bearer token)

## Phase 1 scope (exact)

Goal: a single end-to-end vertical slice that reliably turns a natural-language intent into a Notion Task create/update, with an audit trail.

In scope:
- Submit intent (via action_relay client or curl) to intent_normaliser `POST /v1/intents`.
- intent_normaliser normalises into a deterministic plan (`notion.tasks.create` or `notion.tasks.update`).
- If `EXECUTE_ACTIONS=true` and confidence >= threshold, intent_normaliser executes the plan by calling notion_gateway:
  - `POST /v1/notion/tasks/create` or `POST /v1/notion/tasks/update`
- Write artifacts for: received → normalised → executed (or failed) with stable IDs.
- Idempotency: duplicate submissions with the same `request_id` (or generated deterministic key) must not create duplicate Notion tasks.
- Error handling: gateway errors are surfaced in the response and recorded as artifacts.
- Minimal context lookups:
  - Optional: query context_api for project/task hints when provided, but Phase 1 must still work without context_api being “perfect”.

Out of scope (Phase 2+):
- UI for clarifications (API-only is fine).
- Calendar events / reminders.
- Full automated background sync from Notion.
- Multi-user, permissions, or “agents” beyond single operator.


## Verification commands
- Unit/integration tests (Docker):
  - `docker compose run --rm api pytest`
