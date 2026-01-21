from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional
from zoneinfo import ZoneInfo


class ProjectResolver:
    def resolve(self, selector: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


class StubProjectResolver(ProjectResolver):
    def resolve(self, selector: str) -> List[Dict[str, Any]]:
        return []


@dataclass
class ClarificationPayload:
    question: str
    expected_answer_type: str
    candidates: List[Dict[str, Any]]


@dataclass
class NormalizationResult:
    status: Literal["ready", "needs_clarification", "rejected"]
    canonical_draft: Optional[Dict[str, Any]] = None
    final_canonical: Optional[Dict[str, Any]] = None
    plan: Optional[List[Dict[str, Any]]] = None
    clarification: Optional[ClarificationPayload] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


def _relative_due_label(value: str) -> Optional[str]:
    lowered = value.strip().lower()
    if lowered in {"today", "tomorrow", "next monday", "next week monday"}:
        return lowered
    return None


def _resolve_relative_due(value: str, user_timezone: str) -> Optional[str]:
    label = _relative_due_label(value)
    if not label:
        return None
    try:
        zone = ZoneInfo(user_timezone)
    except Exception:
        return None
    now = datetime.now(zone).date()
    if label == "today":
        return now.isoformat()
    if label == "tomorrow":
        return (now + timedelta(days=1)).isoformat()
    if label in {"next monday", "next week monday"}:
        days_ahead = (7 - now.weekday() + 0) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (now + timedelta(days=days_ahead)).isoformat()
    return None


def normalize_intent(
    packet: Dict[str, Any],
    *,
    user_timezone: str,
    resolver: ProjectResolver,
) -> NormalizationResult:
    intent_type = packet.get("intent_type")
    fields = packet.get("fields") or {}

    if not intent_type:
        canonical_draft = {
            "intent_type": None,
            "fields": fields,
            "pending": {"field": "intent_type"},
        }
        return NormalizationResult(
            status="needs_clarification",
            canonical_draft=canonical_draft,
            clarification=ClarificationPayload(
                question="What is the intent type?",
                expected_answer_type="free_text",
                candidates=[],
            ),
        )

    if intent_type != "create_task":
        return NormalizationResult(
            status="rejected",
            error_code="UNSUPPORTED_INTENT_TYPE",
            message=f"Unsupported intent_type: {intent_type}",
        )

    title = fields.get("title") or packet.get("title")
    if not title:
        return NormalizationResult(
            status="rejected",
            error_code="VALIDATION_ERROR",
            message="Missing required field: title",
            details={"field": "title"},
        )

    canonical_fields: Dict[str, Any] = {"title": title}

    if "project_id" in fields:
        canonical_fields["project_id"] = fields.get("project_id")
    elif "project" in fields:
        project_value = fields.get("project")
        if isinstance(project_value, str):
            candidates = resolver.resolve(project_value)
            canonical_draft = {
                "intent_type": intent_type,
                "fields": {
                    **canonical_fields,
                    "project": {"selector": project_value, "project_id": None},
                },
                "pending": {"field": "project", "selector": project_value},
            }
            expected_type = "choice" if candidates else "free_text"
            return NormalizationResult(
                status="needs_clarification",
                canonical_draft=canonical_draft,
                clarification=ClarificationPayload(
                    question=(
                        f"Which project matches '{project_value}'?"
                        if candidates
                        else f"Provide the project id for '{project_value}'."
                    ),
                    expected_answer_type=expected_type,
                    candidates=candidates,
                ),
            )

    due_value = fields.get("due")
    if isinstance(due_value, str) and _relative_due_label(due_value):
        resolved = _resolve_relative_due(due_value, user_timezone)
        if not resolved:
            canonical_draft = {
                "intent_type": intent_type,
                "fields": {**canonical_fields, "due": {"selector": due_value}},
                "pending": {"field": "due", "selector": due_value},
            }
            return NormalizationResult(
                status="needs_clarification",
                canonical_draft=canonical_draft,
                clarification=ClarificationPayload(
                    question="What is the due date?",
                    expected_answer_type="date",
                    candidates=[],
                ),
            )
        canonical_fields["due"] = resolved
    elif due_value is not None:
        canonical_fields["due"] = due_value

    final_canonical = {"intent_type": intent_type, "fields": canonical_fields}
    plan = [
        {
            "kind": "action",
            "action": "create_task",
            "intent_id": packet.get("intent_id"),
            "correlation_id": packet.get("correlation_id"),
            "fields": canonical_fields,
        }
    ]
    return NormalizationResult(
        status="ready",
        final_canonical=final_canonical,
        canonical_draft=final_canonical,
        plan=plan,
    )


def apply_clarification_answer(
    canonical_draft: Dict[str, Any],
    answer_payload: Dict[str, Any],
) -> Dict[str, Any]:
    pending = canonical_draft.get("pending") or {}
    field = pending.get("field")
    choice_id = answer_payload.get("choice_id")
    answer_text = answer_payload.get("answer_text")
    fields = canonical_draft.setdefault("fields", {})

    if field == "intent_type":
        if answer_text:
            canonical_draft["intent_type"] = answer_text
        elif choice_id:
            canonical_draft["intent_type"] = choice_id
    elif field == "project":
        if choice_id:
            fields["project_id"] = choice_id
            fields.pop("project", None)
        elif answer_text:
            fields["project_id"] = answer_text
            fields.pop("project", None)
    elif field == "due":
        if answer_text:
            fields["due"] = answer_text
        elif choice_id:
            fields["due"] = choice_id

    canonical_draft.pop("pending", None)
    return canonical_draft
