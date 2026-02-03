# UC01 - Task Capture (intent_normaliser)

UC01 is the Phase 1 vertical slice: natural language -> Notion task (create or update).

## Responsibilities
`intent_normaliser` is the API boundary that turns an intent packet into a deterministic plan and (optionally) executes it.

It must:
1. Accept intent packets at `POST /v1/intents`.
2. Normalise the request into a deterministic plan containing exactly one action in Phase 1:
   - `notion.tasks.create` with task fields
   - `notion.tasks.update` with `notion_page_id` and a patch
3. Persist an append-only audit trail (intent and action artifacts).
4. When execution is enabled, call `notion_gateway` task endpoints and surface results.

## API contract

### Request
`POST /v1/intents`

Required fields:
- `kind`: must be `intent`
- `intent_type`: `create_task` or `update_task`
- `natural_language`: free text (may be empty if structured fields are complete)

Strongly recommended for idempotency:
- `request_id`: a stable UUID for retries

Optional:
- `fields`: structured hints such as `title`, `due`, `notes`, or for update `task_id` and `patch`

### Response
Phase 1 expects the service to return an `IngestResponse` that is one of:
- `needs_clarification`
- `ready` (if `EXECUTE_ACTIONS` is false)
- `executed` (if execution is enabled and succeeds)
- `failed` (if execution is enabled and gateway call fails)

On success, the response must include:
- `details.request_id`
- `details.notion_task_id`

On failure, the response must include a machine-readable error envelope:
- `error.code`
- `error.message`
- optional `error.details.status_code`

## Execution behavior (Phase 1)

### Execution toggle
- Execution is enabled only when `EXECUTE_ACTIONS=true` and gateway configuration is present.

### Gateway calls
- Create routes to `notion_gateway` task create endpoint
- Update routes to `notion_gateway` task update endpoint

Each gateway call must include:
- `request_id`: propagate the original request_id when available
- `idempotency_key`: stable deterministic key for the specific action
- `actor`: `X-Actor-Id` header when present, else a default

### Idempotency rules
- If the same `request_id` is submitted twice, the second call must return the same `details.notion_task_id`.
- If no request_id is provided, a deterministic hash-based key may be used, but the Phase 1 smoke test assumes `request_id` is present.

## Artifacts (audit trail)
For each request, the service must persist artifacts for:
- received intent
- outcome status (ready / needs_clarification / rejected)
- each action execution attempt (success or failure)
- final outcome (executed / failed)

## Verification
Primary:
- From the Brain OS workspace: run `brain_os/scripts/phase1_smoke.ps1`.

Repo-local:
- `docker compose run --rm api pytest`

