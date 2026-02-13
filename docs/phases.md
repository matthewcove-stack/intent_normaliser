# intent_normaliser — Phases

## Phase 0 (done)
- API surface for ingest/normalise/clarify
- Append-only artifact persistence
- Config wiring for gateway + context API

## Phase 1 (this repo)
Implement execution + idempotency for the vertical slice:
- When `EXECUTE_ACTIONS=true`, execute the normalised plan by calling notion_gateway task create/update endpoints.
- Persist execution artifacts and return an outcome payload with created/updated Notion IDs.
- Ensure idempotency using request_id (preferred) or a deterministic idempotency key.

## Phase 2 (later)
- Robust retry + dead-letter flows
- Clarification UX (UI)
- Broader action types (calendar, CRM, knowledge capture)

---

## MVP to Market alignment

See `brain_os/docs/mvp_to_market.md`.

### Phase 1 (vertical slice hardening)
- Compatibility with voice client endpoint + auth
- Canonical response envelope for UI confirmation
- Clarification loop usable end-to-end

### Phase 2 (broader actions)
Add action types:
- `notion.list.add_item`
- `notion.note.capture`
- (optional) `notion.db.rows.create`

### Phase 3 (launch)
- Production-safe defaults, logging, and runbooks
