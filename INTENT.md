# Intent: Intent Normaliser Service (FastAPI)

## Overview

Build a **single-ingress** HTTP service that sits in front of the Notion execution kernel (n8n). It accepts:

* **Intent Packets** (semi-structured, may contain ambiguity)
* **Action Packets** (fully-specified execution instructions)

and returns one of:

* **Ready** → a fully resolved **Plan** (Action Packets) that is safe to execute
* **Needs Clarification** → a structured clarification request that must be answered out-of-band
* **Rejected** → a hard failure with explicit reasons

This service is the **policy, disambiguation, schema-stability, and orchestration boundary** between ChatGPT-driven intent capture and deterministic execution.

Key architectural decision (updated):

> This service owns **all state**, **all clarification loops**, and **all resumability**.  
> Device-side runners are thin, stateless couriers.

n8n remains a deterministic execution kernel; this service is the decision firewall and intent orchestrator.

---

## Core Responsibilities (Updated)

* Single target for ChatGPT runners (`action_relay`)
* Canonicalise ambiguous intent into deterministic, executable plans
* Block ambiguous or unsafe requests by default
* Perform **controlled inference** (e.g. “next week” → ISO date) with full traceability
* Generate **deterministic idempotency keys**
* Emit Action Packets compatible with existing n8n webhooks
* Persist **all intent and clarification artifacts** to Postgres (mandatory, append-only)
* Manage **out-of-band clarification lifecycle** (server-side, resumable from any device)

---

## Goals

1. **Single ingress**: all runners post only to this service
2. **Stable LLM-facing contract**: Intent Packet schema is stable even as internals evolve
3. **Safety by default**: no silent guesses; ambiguity → clarification
4. **Deterministic execution**: Action Packets strictly match n8n contracts
5. **Idempotent by construction**: retries never duplicate effects
6. **Explainable decisions**: every inference, default, and block is recorded
7. **Testable in isolation**: no dependency on n8n for normalisation tests
8. **Audit + learning**: Postgres artifacts are the source of truth

---

## Non-Goals

* Not a reasoning agent or conversational system
* Not a UI layer
* Not a workflow orchestrator (n8n owns orchestration)
* Not a general-purpose Notion query engine
* Not model training (learning = analysing stored artifacts)

---

## Service API (v1) — Updated

### POST /v1/intents

Accept an **Intent Packet** and return:

* `status=ready` with a `plan`
* `status=needs_clarification` with a `clarification`
* `status=rejected` with structured errors

**Always** persists intent state and artifacts to Postgres.

### POST /v1/actions

Accept an **Action Packet** and:

* `status=accepted` (and optionally forward to n8n)
* `status=rejected` if validation or policy fails

**Always** persists an artifact.

### GET /v1/clarifications?status=open

Return all open clarifications for the authenticated user/system.

### POST /v1/clarifications/{clarification_id}/answer

Submit an answer to a clarification and resume the pending intent.

### GET /v1/intents/{intent_id}

Return current intent status, latest canonical form, and clarification state (if any).

### GET /health
### GET /version

---

## Packet Types

### Intent Packet (input, LLM-facing)

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
  "confidence": 0.82,
  "source": "chat",
  "timestamp": "2026-01-19T20:00:00Z"
}
```

Intent Packets may contain ambiguity, relative values, or unresolved references.

---

### Action Packet (output, execution-facing)

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
  "idempotency_key": "sha256:..."
}
```

Action Packets must be fully resolved, schema-locked, and safe to execute.

---

### Plan (output)

A Plan is an ordered list of Action Packets.  
Each Action Packet carries its own idempotency key.

---

## Clarification Model (First-Class, Updated)

Clarification is a **terminal-but-resumable** state.

```json
{
  "clarification_id": "uuid",
  "intent_id": "uuid",
  "question": "Which project do you mean by 'Sagitta'?",
  "expected_answer_type": "choice | free_text | date | datetime",
  "candidates": [
    { "id": "proj_1", "label": "Sagitta – Loft" },
    { "id": "proj_2", "label": "Sagitta – Flooring" }
  ],
  "status": "open"
}
```

Rules:
* No execution while clarification is open
* Clarifications are persisted server-side
* Answers may arrive from any device or channel
* Resumption must be deterministic and idempotent

---

## Normalisation Rules (v1, Updated)

### Rule 0: Auth & rate limiting
(as before)

### Rule 1: Envelope validation
(as before)

### Rule 2: Intent classification
(as before)

### Rule 3: Required fields & defaults
(as before; all defaults recorded explicitly)

### Rule 4: Entity resolution (STRICT, updated)

* All entity resolution uses indexed repositories (e.g. ProjectIndex)
* Resolution outcomes:
  * 0 matches → needs_clarification
  * 1 match → accept
  * >1 matches → needs_clarification with candidates
* Automatic resolution ONLY if:
  * top_score ≥ 0.90
  * (top_score − second_score) ≥ 0.10
  * entity is active
* Otherwise: block and ask

No guessing. Ever.

### Rule 5: Relative time resolution
(as before; inference always recorded)

### Rule 6: Policy enforcement
(as before)

### Rule 7: Idempotency generation
(as before; deterministic, canonicalised)

### Rule 8: Plan generation
(as before)

### Rule 9: Execution gating
(as before; EXECUTE_ACTIONS flag)

---

## Intent & Clarification State Model (Updated)

### intents
* intent_id (uuid, pk)
* status: received | needs_clarification | ready | executing | succeeded | failed | expired
* raw_packet (jsonb)
* canonical_draft (jsonb)
* final_canonical (jsonb, nullable)
* idempotency_key (unique)
* created_at / updated_at

### clarifications
* clarification_id (uuid, pk)
* intent_id (fk)
* status: open | answered | expired
* question
* candidates (jsonb)
* expected_answer_type
* answer (jsonb)
* answered_at

Append-only artifact records remain mandatory and unchanged.

---

## Intent Artifact Storage (Postgres — Mandatory)

(unchanged in principle; now explicitly includes clarification lifecycle)

Artifacts must be written at:
1. Ingress (received)
2. Post-normalisation (ready / needs_clarification / rejected)
3. Post-clarification resolution
4. Post-execution (if enabled)

---

## Configuration (env)

(existing list unchanged, plus:)

* `CLARIFICATION_EXPIRY_HOURS`
* `PROJECT_RESOLUTION_THRESHOLD`
* `PROJECT_RESOLUTION_MARGIN`

---

## Observability
(as before)

---

## Testing Strategy (Updated)

Add tests for:
* Clarification creation
* Clarification answer → intent resumption
* Cross-device resume safety
* Idempotent clarification answers

---

## Decisions & Notes (Updated)

* Clarification is a first-class state, not an error
* All orchestration state is server-side
* Runners are stateless and disposable
* Prefer blocking over guessing
* n8n remains a pure execution kernel

---

## Phase Plan

Phase structure remains unchanged, with clarification endpoints and state added in Phase 1.
