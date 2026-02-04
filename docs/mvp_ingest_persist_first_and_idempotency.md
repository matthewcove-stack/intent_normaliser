# MVP Ingest Behavior: Persist-first + Idempotency

Status: draft (MVP)  
Owner: intent_normaliser  
Last updated: 2026-02-03

## Purpose
Make ingest reliable for copy/paste workflows:
- Persist the incoming intent before any downstream calls
- Make duplicate pastes safe (idempotent)
- Return a receipt users can trust

This doc does not change architecture; it formalizes the MVP behavior that the stack already aims for.

## Invariants (MUST hold)
1) No silent drops: if the client receives receipt_id, the payload is persisted.
2) Persist-first: persistence happens before normalisation execution calls.
3) Idempotent: same canonical payload -> same idempotency_key -> no duplicate downstream actions.
4) Clear error envelope on failure.

## Canonicalization and idempotency_key
### Canonical JSON
- Parse incoming JSON into an object.
- Re-serialize with:
  - UTF-8
  - sorted keys
  - no insignificant whitespace
- Compute sha256 over the canonical bytes.

### Handling duplicates
If idempotency_key already exists:
- Return the original stored response envelope (receipt_id, status, details).
- Do not re-run execution unless explicitly requested via a replay endpoint/tool (post-MVP).

## Persist-first sequence (recommended)
1) Validate request (JSON parse + schema check)
2) Compute idempotency_key
3) Upsert inbox/audit row:
   - idempotency_key (unique)
   - received_at
   - raw packet (as JSON)
   - source, schema_version, etc.
4) If duplicate:
   - return stored receipt immediately
5) Else:
   - normalise -> persist plan artifact
   - if EXECUTE_ACTIONS=true, execute via notion_gateway
   - persist outcome (executed/failed + details)
6) Return response envelope

## Response envelope fields (MVP minimum)
- receipt_id: stable DB id or UUID
- trace_id: generated per request (propagate to notion_gateway)
- status: accepted | planned | executed | failed
- idempotency_key
- error (only when failed)

## Failure modes
- Bad JSON: 400 error.code="bad_json"
- Schema fail: 400 error.code="schema_validation_failed"
- Unsupported schema_version: 400 error.code="unsupported_schema_version"
- Downstream Notion failure: 502/500 (choose consistently), status="failed", include gateway status_code in error.details

## Verification commands (suggested)
- Unit test: canonicalization produces stable key for equivalent JSON text
- Integration: duplicate POST returns same receipt_id and does not call notion_gateway twice
- Smoke: good payload returns 200 + receipt_id
