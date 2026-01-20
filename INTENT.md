# Intent: Intent Normaliser Service (FastAPI)

## Overview

Build a **single-ingress** HTTP service that sits in front of the Notion execution kernel (n8n). It accepts either:

* **Intent Packets** (semi-structured, may contain ambiguity) and returns **Action Packets / Plans** that are safe to execute; or
* **Action Packets** (fully-specified execution instructions) and either **passes through** to n8n (after strict validation + policy) or rejects.

This service is the **policy + disambiguation + schema-stability layer**:

* It blocks ambiguous requests.
* It performs controlled inference (e.g. “next week” → ISO date) and labels inference explicitly.
* It generates idempotency keys deterministically.
* It emits execution packets compatible with your existing n8n endpoints.
* It **always persists intent artifacts** to Postgres for audit + learning.

n8n remains a deterministic execution kernel; this service is the decision firewall.

---

## Goals

1. **Single target for runner**: `chat_to_notion_runner` posts only to this service.
2. **Stable interface for ChatGPT**: ChatGPT outputs **Intent Packets** that are stable across internal schema changes.
3. **Safety by default**: no silent guesses; ambiguous references return `needs_clarification`.
4. **Deterministic execution**: output Action Packets match the current n8n webhook contract.
5. **Idempotent**: retries never duplicate tasks or updates.
6. **Explainability**: responses include what was inferred/defaulted and why.
7. **Testable**: normalisation rules are unit-testable without n8n.
8. **Audit + learning**: **every request is stored as an intent artifact in Postgres** (non-optional).

---

## Non-Goals

* Not a reasoning agent; no long-form chain-of-thought.
* Not a workflow orchestrator (n8n does orchestration).
* Not a full Notion query engine beyond what’s required for disambiguation/resolution.
* Not model training. “Learning” = stored artifacts + tuning rules/preferences over time.

---

## Service API (v1)

### POST /v1/ingest/intent

Accept an **Intent Packet**, normalise it into:

* `status=ready` with `plan` (list of Action Packets) OR
* `status=needs_clarification` with questions/options OR
* `status=rejected` with reasons.

**Always** writes an intent artifact row to Postgres (see storage section).

### POST /v1/ingest/action

Accept an **Action Packet** and:

* `status=accepted` and optionally forward to n8n; OR
* `status=rejected` if policy/validation fails.

**Always** writes an intent artifact row to Postgres.

### GET /health

Simple health probe (includes DB connectivity check).

### GET /version

Returns git SHA/version + schema version.

---

## Packet Types

### Intent Packet (input)

Intent Packet is stable and human/LLM-facing. It may contain:

* natural language fields (relative dates, vague refs)
* unresolved entity references
* assumptions (optional)
* confidence (optional)

Minimum viable schema (conceptual):

```json
{
  "kind": "intent",
  "intent_type": "create_task",
  "natural_language": "Follow up with John next week about the loft.",
  "fields": {
    "title": "Follow up with John",
    "due": "next week",
    "project": "Sagitta loft",
    "priority": "medium"
  },
  "assumptions": [],
  "confidence": 0.82,
  "source": "chat",
  "timestamp": "2026-01-19T20:00:00Z"
}
```

### Action Packet (output)

Action Packet is execution-facing and must be fully resolved and schema-locked to n8n.

```json
{
  "kind": "action",
  "action": "notion.tasks.create",
  "payload": {
    "title": "Follow up with John",
    "due_date": "2026-01-26",
    "project_id": "proj_abc123",
    "priority": "medium"
  },
  "idempotency_key": "task-followup-john-proj_abc123-20260126"
}
```

### Plan (output)

A response can contain multiple action packets:

* create task
* then link
* then add metadata
  Each action includes its own idempotency key.

---

## Normalisation Rules (v1)

### Rule 0: Authenticate & rate-limit

* Require bearer token: `Authorization: Bearer <INTENT_SERVICE_TOKEN>`
* Reject missing/invalid token.
* Add simple per-IP rate limiting (configurable).

### Rule 1: Validate packet envelope

* `kind` must be `intent` or `action`.
* Unknown kind rejected.
* `intent_type`/`action` must be allowed list.

### Rule 2: Intent classification

Supported `intent_type` (initial):

* `create_task`
* `update_task`
* `upsert_task` (optional)
* `query` (optional, non-mutating)
* `noop`

Map to one or more action operations:

* `notion.tasks.create`
* `notion.tasks.update`
* (others later)

### Rule 3: Required fields and defaults

For each intent_type, define required fields:

* create_task: `title` required; others optional with defaults.
  Defaults are explicit:
* priority default `medium`
* status default `todo` (if used)
  All defaults must be recorded in `resolution.defaults_applied`.

### Rule 4: Resolve entity references (no guessing)

Resolve strings like:

* `project: "Sagitta loft"` → `project_id`
* `assignee: "Matthew"` → `person_id` (if supported)

Resolution must follow:

* 0 matches → `needs_clarification` (provide guidance)
* 1 match → accept
* > 1 match → `needs_clarification` with options (top N + scores)

Resolution sources (configurable):

* Notion search endpoint(s) you already have
* cached lookups
* optionally a local mapping file

### Rule 5: Resolve relative time safely

If `due` is relative (“tomorrow”, “next week”, “Friday”):

* convert to ISO date/time using user timezone (Europe/London)
* store inference record:

  * `field=due_date`
  * `inferred_from="next week"`
  * `strategy="next_week_monday"` (configurable preference)
    If confidence below threshold or ambiguity exists → `needs_clarification`.

### Rule 6: Policy enforcement

Hard policy gates:

* No deletes in v1.
* No updating without stable identifier (task_id or resolvable unique key).
* If `confidence < MIN_CONFIDENCE_TO_WRITE` → block.
* If inferred fields exceed `MAX_INFERRED_FIELDS` → block or ask.
* Reject any unknown fields in payload mapping.

### Rule 7: Idempotency generation

For each resulting action:

* If caller provided `idempotency_key`, validate format and accept.
* Else compute deterministic key from canonicalised content:

  * action name
  * resolved IDs
  * ISO date
  * normalised title hash
    Example:
    `sha256(action + title_norm + project_id + due_date)[:32]`

### Rule 8: Produce Action Packet(s)

Emit one or more action packets with:

* fully resolved IDs
* absolute values only
* enum values validated
* idempotency key

### Rule 9: Optional forwarding (execution mode)

Config flag `EXECUTE_ACTIONS=true|false`

* If true: service forwards action packets to n8n endpoints and returns execution results.
* If false: service only returns the plan (runner executes), useful for phased rollout.

---

## Intent Artifact Storage (Postgres - Mandatory)

### Principle

**Every request** (intent or action) must be persisted to Postgres as an **append-only** artifact record:

* no updates-in-place
* corrections create new rows linked by correlation IDs

This provides:

* auditability
* reproducibility
* measurable system behaviour (ambiguity rates, failure rates)
* a foundation for improving normalisation rules over time

### Table design (recommended)

Single table, append-only, with `jsonb` artifact:

**Table: `intent_artifacts`**

* `id` (uuid, pk, default gen_random_uuid())
* `intent_id` (text, not null) — stable identifier returned to caller
* `correlation_id` (text, not null) — groups related artifacts (e.g. clarification cycle)
* `supersedes_intent_id` (text, null) — if this artifact replaces a previous one
* `received_at` (timestamptz, not null, default now())
* `kind` (text, not null) — `intent|action`
* `intent_type` (text, null) — for intent packets
* `action` (text, null) — for action packets
* `status` (text, not null) — `ready|needs_clarification|rejected|accepted|executed|failed`
* `idempotency_key` (text, null)
* `artifact_version` (int, not null, default 1)
* `artifact_hash` (text, not null) — sha256 of canonicalised artifact json
* `artifact` (jsonb, not null) — full request+resolution+plan+execution outcome

**Indexes**

* unique: `(intent_id)`
* btree: `(received_at)`, `(status)`, `(intent_type)`, `(action)`, `(idempotency_key)`
* gin: `(artifact)` (or targeted GIN indexes later if needed)

### Write points (mandatory)

Persist an artifact row at:

1. **Ingress**: raw packet received (status `received`)
2. **Post-normalisation**: resolution + plan (status `ready|needs_clarification|rejected`)
3. **Post-execution** (if `EXECUTE_ACTIONS=true`): execution outcome (status `executed|failed`)

Implementation note: you can store these as separate rows linked by `correlation_id`, or a single row per request that includes nested stages; append-only requirement strongly prefers separate rows.

### Canonicalisation for hashing

Define deterministic canonical JSON serialisation (sorted keys, stable whitespace) before hashing so hashes are reproducible across runs.

---

## Configuration (env)

* `INTENT_SERVICE_TOKEN`
* `DATABASE_URL` (Postgres DSN)
* `USER_TIMEZONE` (default `Europe/London`)
* `MIN_CONFIDENCE_TO_WRITE` (default `0.75`)
* `MAX_INFERRED_FIELDS` (default `2`)
* `EXECUTE_ACTIONS` (default `false`)
* `N8N_BASE_URL` and endpoint URLs
* `N8N_BEARER_TOKEN` (if needed)
* Notion / Notion OS endpoint URLs + tokens

---

## Observability

* Structured logs (JSON) with `intent_id` + `correlation_id`.
* Return `intent_id` and `correlation_id` in response headers.
* `/health` includes DB connectivity check.
* Metrics (optional): counts of ready/blocked/rejected, ambiguity rate, execution failure rate.

---

## Testing Strategy

1. **Unit tests** for:

   * parsing/validation
   * time resolution (“next week” etc.)
   * entity matching decisions
   * idempotency determinism
   * policy gates
2. **Contract tests**:

   * output action packets validate against action packet schema
3. **Integration tests**:

   * stub n8n endpoints
   * stub Notion search endpoints
4. **Golden tests**:

   * folder of intent packets → expected plan outcomes

---

## Milestones

### M1: Skeleton service

* FastAPI app + auth + /health + /version
* Postgres connection + migrations
* Artifact write on ingress

### M2: Intent → action for create_task

* Create task mapping
* Relative due date resolution
* Project resolution via search
* Idempotency generation
* Artifact write after normalisation

### M3: needs_clarification flow

* Multi-match and no-match handling
* Return options/questions
* Artifact write for clarification outcome

### M4: Action ingest hardening

* Strict action packet validation
* Policy gates
* Optional pass-through/execute mode
* Artifact write for acceptance/rejection

### M5: Execute mode + outcomes

* Forward action packets to n8n
* Record execution outcomes (executed/failed) as artifacts

---

## Decisions & Notes

* Prefer **blocking** over guessing.
* Keep n8n as deterministic execution kernel.
* Keep Intent Packet stable; allow Action Packet schema to evolve behind the normaliser.
* All inference/defaulting must be explicit in resolution metadata.
* Idempotency must be deterministic.
* Artifact storage is mandatory and append-only.

---

# Phase-0 / Phase-1 Build Plan (Codex)

## Phase 0 — Skeleton + Contracts (Docker-first; no Notion, no n8n execution)

### Goal

Ship a running FastAPI service with:

* stable request/response envelopes
* strict auth
* mandatory Postgres persistence (append-only)
* health/version endpoints
* unit-test scaffold

### Deliverables

1. **Repo structure**

   * `app/main.py` (FastAPI app)
   * `app/config.py` (pydantic-settings)
   * `app/models/` (Pydantic request/response models)
   * `app/storage/` (Postgres persistence)
   * `app/util/` (hashing, canonical JSON)
   * `tests/` (pytest)
   * `docker-compose.yml` (API + Postgres + migrate job)
   * `Dockerfile`

2. **Docker-first local dev**

   * `docker compose up --build` brings up:

     * `postgres` (with healthcheck)
     * `migrate` (one-shot Alembic upgrade)
     * `api` (uvicorn)
   * API must **not** assume host-installed Python.
   * Postgres is mandatory: if DB not healthy, API returns 503 for ingest endpoints.

3. **docker-compose.yml (required shape)**

   * **postgres**

     * image: `postgres:<pinned>`
     * volume for data persistence
     * healthcheck using `pg_isready`
   * **migrate** (one-shot)

     * builds same image as API
     * command: `alembic upgrade head`
     * depends_on postgres healthy
   * **api**

     * builds from Dockerfile
     * command: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
     * depends_on:

       * postgres healthy
       * migrate completed successfully
     * exposes `8000:8000`
     * env includes `DATABASE_URL`, `INTENT_SERVICE_TOKEN`, etc.

4. **Endpoints**

   * `GET /health`

     * returns `{status:"ok"}`
     * includes a DB connectivity check (simple `SELECT 1`)
   * `GET /version`

     * returns `{version, git_sha, artifact_version}` (git sha can be env-provided in Phase 0)
   * `POST /v1/ingest/intent`

     * validates envelope only (no normalisation yet)
     * writes a **received** artifact row
     * returns `status="rejected"` with `NOT_IMPLEMENTED` for normalisation (for now)
   * `POST /v1/ingest/action`

     * validates envelope only
     * writes a **received** artifact row
     * returns `status="rejected"` with `NOT_IMPLEMENTED` (for now)

5. **Auth middleware / dependency**

   * Require `Authorization: Bearer <INTENT_SERVICE_TOKEN>` on both ingest endpoints.
   * Reject missing/invalid token with 401.

6. **Postgres: migrations + table**

   * Use Alembic.
   * Create `intent_artifacts` table (as specified above) with required indexes.
   * Ensure append-only semantics at application layer (no update statements).

7. **Artifact writing (Phase 0)**

   * On every ingest, write a single artifact row with:

     * `status = "received"`
     * `artifact` contains the inbound packet plus minimal metadata
   * `intent_id` and `correlation_id` generated if not provided:

     * `intent_id`: `int_<ulid>`
     * `correlation_id`: `cor_<ulid>`

8. **Canonical JSON + hashing**

   * Implement `canonical_json(obj) -> str` (sorted keys, no whitespace variability)
   * Implement `sha256_hex(canonical_json)`
   * Store as `artifact_hash`

9. **Testing**

   * pytest + httpx test client
   * tests for:

     * auth required
     * DB write occurs
     * `/health` fails cleanly if DB unavailable

10. **Concrete docker compose template (include in repo)**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: intent
      POSTGRES_USER: intent
      POSTGRES_PASSWORD: intent
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U intent -d intent"]
      interval: 3s
      timeout: 3s
      retries: 30

  migrate:
    build: .
    environment:
      DATABASE_URL: postgresql+psycopg://intent:intent@postgres:5432/intent
    depends_on:
      postgres:
        condition: service_healthy
    command: ["alembic", "upgrade", "head"]

  api:
    build: .
    environment:
      DATABASE_URL: postgresql+psycopg://intent:intent@postgres:5432/intent
      INTENT_SERVICE_TOKEN: change-me
      USER_TIMEZONE: Europe/London
      MIN_CONFIDENCE_TO_WRITE: "0.75"
      MAX_INFERRED_FIELDS: "2"
      EXECUTE_ACTIONS: "false"
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

volumes:
  postgres_data:
```

---

## Phase 1 — Create Task Normalisation (plan output, execute optional)

* `GET /health`

  * returns `{status:"ok"}`
  * includes a DB connectivity check (simple `SELECT 1`)
* `GET /version`

  * returns `{version, git_sha, artifact_version}` (git sha can be env-provided in Phase 0)
* `POST /v1/ingest/intent`

  * validates envelope only (no normalisation yet)
  * writes a **received** artifact row
  * returns `status="rejected"` with `NOT_IMPLEMENTED` for normalisation (for now)
* `POST /v1/ingest/action`

  * validates envelope only
  * writes a **received** artifact row
  * returns `status="rejected"` with `NOT_IMPLEMENTED` (for now)

3. **Auth middleware / dependency**

   * Require `Authorization: Bearer <INTENT_SERVICE_TOKEN>` on both ingest endpoints.
   * Reject missing/invalid token with 401.

4. **Postgres: migrations + table**

   * Use Alembic.
   * Create `intent_artifacts` table (as specified above) with required indexes.
   * Ensure append-only semantics at application layer (no update statements).

5. **Artifact writing (Phase 0)**

   * On every ingest, write a single artifact row with:

     * `status = "received"`
     * `artifact` contains the inbound packet plus minimal metadata
   * `intent_id` and `correlation_id` generated if not provided:

     * `intent_id`: `int_<ulid>`
     * `correlation_id`: `cor_<ulid>`

6. **Canonical JSON + hashing**

   * Implement `canonical_json(obj) -> str` (sorted keys, no whitespace variability)
   * Implement `sha256_hex(canonical_json)`
   * Store as `artifact_hash`

7. **Testing**

   * pytest + httpx test client
   * tests for:

     * auth required
     * DB write occurs
     * `/health` fails cleanly if DB unavailable

### Phase 0 Acceptance Criteria

* `docker compose up` runs service + Postgres.
* `POST /v1/ingest/intent` with valid token writes exactly one `received` row.
* All tests pass in CI locally.

---

## Phase 1 — Create Task Normalisation (plan output, execute optional)

### Goal

Implement **intent → plan** for `intent_type=create_task` with:

* controlled inference (relative dates)
* optional project resolution (stubbed first, then real)
* policy gates
* deterministic idempotency
* mandatory artifact writes for each stage

### Deliverables

1. **Pydantic models**

   * `IntentPacket`
   * `ActionPacket`
   * `NormaliseResponse` with union statuses:

     * `ready | needs_clarification | rejected`
   * Shared `Resolution` model:

     * `resolved_entities[]`
     * `inferences[]`
     * `defaults_applied[]`
     * `policy` block

2. **Normalisation pipeline (create_task)**
   Implement a pure function-style pipeline:

   * `validate_required_fields`
   * `apply_defaults`
   * `resolve_due_date` (relative -> ISO date)
   * `resolve_project`:

     * Phase 1a: allow `project_id` passthrough if already provided
     * Phase 1b: if only `project` string is provided, call a `ProjectResolver` interface

       * initially a stub returning `needs_clarification` (to avoid guessing)
   * `policy_check` (min confidence, max inferred fields)
   * `generate_idempotency_key`
   * `emit_action_packet`

3. **Relative date handling**

   * Support at least:

     * `today`, `tomorrow`
     * `next week` (configurable strategy; default Monday)
     * weekday names (e.g. `Friday`) with “next occurrence” logic
   * All inferences recorded in `resolution.inferences`.
   * If ambiguous (e.g. “next Friday” vs “Friday”) and confidence < threshold -> `needs_clarification`.

4. **Artifact writes (mandatory, append-only)**
   For each request, write multiple rows linked by `correlation_id`:

   * Row A: `status="received"` (raw inbound)
   * Row B: `status in {"ready","needs_clarification","rejected"}` with:

     * resolution
     * plan (if ready)
     * reasons/questions (if not)
       If `EXECUTE_ACTIONS=true`:
   * Row C: `status in {"executed","failed"}` with:

     * execution result from n8n

5. **Execution forwarding (optional in Phase 1)**

   * Implement behind `EXECUTE_ACTIONS` flag.
   * Forward each action packet to the configured n8n endpoint.
   * Capture response payload and store in executed/failed artifact row.

6. **Error taxonomy**
   Standardise error codes:

   * `VALIDATION_ERROR`
   * `POLICY_LOW_CONFIDENCE`
   * `POLICY_TOO_MANY_INFERENCES`
   * `NEEDS_PROJECT_DISAMBIGUATION`
   * `NOT_IMPLEMENTED`
   * `EXECUTION_FAILED`

7. **Tests**

   * Unit tests for date parsing and idempotency determinism.
   * API tests:

     * create_task with explicit ISO due_date -> ready
     * create_task with `due="next week"` -> ready + inference recorded
     * missing title -> rejected
     * low confidence -> rejected
     * project string without resolver -> needs_clarification
   * Storage tests confirm 2 rows per request (received + outcome).

### Phase 1 Acceptance Criteria

* `POST /v1/ingest/intent` with `intent_type=create_task` returns:

  * `ready` + a valid action packet when unambiguous
  * `needs_clarification` when project reference is not resolvable
  * `rejected` when policy/validation fails
* Postgres contains append-only artifacts for every call (at least `received` + outcome).
* Idempotency key is deterministic across repeated identical requests.

---

## Codex Implementation Notes

* Keep normalisation logic in pure functions/services; FastAPI endpoints should be thin.
* Treat Postgres as mandatory: if DB write fails, return 503 and do not proceed.
* Do not leak secrets in logs. Redact auth headers.
* Use `ulid` for `intent_id`/`correlation_id` for sortable IDs.
* Prefer `json.dumps(..., sort_keys=True, separators=(",", ":"))` for canonicalisation.
