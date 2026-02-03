from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional
from zoneinfo import ZoneInfo

import httpx


class ProjectResolver:
    def resolve(self, selector: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


class StubProjectResolver(ProjectResolver):
    def resolve(self, selector: str) -> List[Dict[str, Any]]:
        return []


class HttpProjectResolver(ProjectResolver):
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: Optional[str] = None,
        search_path: str = "/v1/projects/search",
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._search_path = search_path
        self._timeout_seconds = timeout_seconds

    def resolve(self, selector: str) -> List[Dict[str, Any]]:
        url = f"{self._base_url}{self._search_path}"
        headers = {"Content-Type": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        payload = {"query": selector, "limit": 5}
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=self._timeout_seconds)
        except httpx.RequestError:
            return []
        if response.status_code != 200:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        candidates = data.get("results") or data.get("candidates") or []
        if not isinstance(candidates, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if "score" not in candidate and "confidence" in candidate:
                candidate = {**candidate, "score": candidate.get("confidence")}
            normalized.append(candidate)
        return normalized


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


_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _relative_due_label(value: str) -> Optional[str]:
    lowered = value.strip().lower()
    if lowered in {"today", "tomorrow", "next week", "next week monday"}:
        return lowered
    if lowered.startswith("next "):
        weekday = lowered.replace("next ", "", 1).strip()
        if weekday in _WEEKDAYS:
            return lowered
    if lowered in _WEEKDAYS:
        return lowered
    return None


def _next_weekday(start: date, weekday: int, *, strict_future: bool) -> date:
    days_ahead = (weekday - start.weekday()) % 7
    if days_ahead == 0 and strict_future:
        days_ahead = 7
    return start + timedelta(days=days_ahead)


def _resolve_relative_due(value: str, user_timezone: str) -> Optional[tuple[str, str]]:
    label = _relative_due_label(value)
    if not label:
        return None
    try:
        zone = ZoneInfo(user_timezone)
    except Exception:
        return None
    now = datetime.now(zone).date()
    if label == "today":
        return now.isoformat(), "today"
    if label == "tomorrow":
        return (now + timedelta(days=1)).isoformat(), "tomorrow"
    if label in {"next week", "next week monday"}:
        target = _next_weekday(now + timedelta(days=7), _WEEKDAYS["monday"], strict_future=False)
        return target.isoformat(), "next_week_monday"
    if label.startswith("next "):
        weekday = label.replace("next ", "", 1).strip()
        target = _next_weekday(now, _WEEKDAYS[weekday], strict_future=True)
        return target.isoformat(), f"next_{weekday}"
    if label in _WEEKDAYS:
        target = _next_weekday(now, _WEEKDAYS[label], strict_future=True)
        return target.isoformat(), f"next_{label}"
    return None


def _is_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _select_high_confidence_candidate(
    candidates: Iterable[Dict[str, Any]],
    *,
    threshold: float,
    margin: float,
) -> Optional[Dict[str, Any]]:
    scored = []
    for candidate in candidates:
        score = candidate.get("score")
        if score is None:
            score = 0.0
        scored.append((float(score), candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_candidate = scored[0]
    if top_score < threshold:
        return None
    if len(scored) > 1:
        second_score = scored[1][0]
        if (top_score - second_score) < margin:
            return None
    return top_candidate


def _normalize_project_candidates(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        original_id = candidate.get("id")
        label = candidate.get("label") or candidate.get("name") or original_id
        if not isinstance(label, str) or not label.strip():
            continue
        meta = dict(candidate.get("meta") or {})
        if isinstance(original_id, str) and original_id != label:
            meta.setdefault("project_id", original_id)
        normalized_candidate = dict(candidate)
        normalized_candidate["id"] = label
        normalized_candidate["label"] = label
        if meta:
            normalized_candidate["meta"] = meta
        normalized.append(normalized_candidate)
    return normalized


def normalize_intent(
    packet: Dict[str, Any],
    *,
    user_timezone: str,
    resolver: ProjectResolver,
    project_resolution_threshold: float = 0.90,
    project_resolution_margin: float = 0.10,
    min_confidence_to_write: float = 0.75,
    max_inferred_fields: int = 2,
) -> NormalizationResult:
    intent_type = packet.get("intent_type")
    fields = packet.get("fields") or {}
    confidence = packet.get("confidence")

    if confidence is not None:
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = None
        if confidence_value is not None and confidence_value < min_confidence_to_write:
            return NormalizationResult(
                status="rejected",
                error_code="POLICY_LOW_CONFIDENCE",
                message="Confidence below minimum threshold",
                details={"confidence": confidence_value, "threshold": min_confidence_to_write},
            )

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

    if intent_type not in {"create_task", "update_task"}:
        return NormalizationResult(
            status="rejected",
            error_code="UNSUPPORTED_INTENT_TYPE",
            message=f"Unsupported intent_type: {intent_type}",
        )

    inferred_fields = []

    canonical_fields: Dict[str, Any] = {}
    if intent_type == "create_task":
        title = fields.get("title") or packet.get("title")
        if not title:
            return NormalizationResult(
                status="rejected",
                error_code="VALIDATION_ERROR",
                message="Missing required field: title",
                details={"field": "title"},
            )
        canonical_fields["title"] = title
        if "status" in fields:
            canonical_fields["status"] = fields.get("status")
        if "priority" in fields:
            canonical_fields["priority"] = fields.get("priority")
    else:
        task_id = fields.get("task_id") or fields.get("notion_page_id")
        if not task_id:
            return NormalizationResult(
                status="rejected",
                error_code="POLICY_MISSING_TASK_ID",
                message="Missing required field: task_id",
                details={"field": "task_id"},
            )
        canonical_fields["task_id"] = task_id

    if intent_type == "create_task":
        project_value = None
        if "project" in fields:
            project_value = fields.get("project")
        elif "project_id" in fields:
            project_value = fields.get("project_id")
        if isinstance(project_value, str):
            if fields.get("project_resolved") is True:
                canonical_fields["project"] = project_value
            else:
                candidates = _normalize_project_candidates(resolver.resolve(project_value))
                resolved = _select_high_confidence_candidate(
                    candidates,
                    threshold=project_resolution_threshold,
                    margin=project_resolution_margin,
                )
                if resolved:
                    label = resolved.get("label") or resolved.get("id")
                    canonical_fields["project"] = label or project_value
                else:
                    canonical_draft = {
                        "intent_type": intent_type,
                        "fields": {
                            **canonical_fields,
                            "project": {"selector": project_value, "value": None},
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
                                else f"Provide the project name for '{project_value}'."
                            ),
                            expected_answer_type=expected_type,
                            candidates=candidates,
                        ),
                    )

    due_value = fields.get("due")
    patch_fields: Dict[str, Any] = {}
    if intent_type == "update_task":
        if "status" in fields:
            patch_fields["status"] = fields.get("status")
        if "priority" in fields:
            patch_fields["priority"] = fields.get("priority")

    if isinstance(due_value, str):
        if _relative_due_label(due_value):
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
            resolved_value, strategy = resolved
            inferred_fields.append(
                {
                    "field": "due",
                    "inferred_from": due_value,
                    "strategy": strategy,
                }
            )
            if intent_type == "update_task":
                patch_fields["due"] = resolved_value
            else:
                canonical_fields["due"] = resolved_value
        elif _is_iso_datetime(due_value) or _is_iso_date(due_value):
            if intent_type == "update_task":
                patch_fields["due"] = due_value
            else:
                canonical_fields["due"] = due_value
        else:
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
    elif due_value is not None:
        if intent_type == "update_task":
            patch_fields["due"] = due_value
        else:
            canonical_fields["due"] = due_value

    if inferred_fields and len(inferred_fields) > max_inferred_fields:
        return NormalizationResult(
            status="rejected",
            error_code="POLICY_TOO_MANY_INFERENCES",
            message="Too many inferred fields",
            details={"inferred_fields": inferred_fields, "max_inferred_fields": max_inferred_fields},
        )

    if intent_type == "update_task":
        if not patch_fields:
            return NormalizationResult(
                status="rejected",
                error_code="VALIDATION_ERROR",
                message="No updatable fields provided",
                details={"fields": list(fields.keys())},
            )
        canonical_fields["patch"] = patch_fields

    final_canonical = {
        "intent_type": intent_type,
        "fields": canonical_fields,
        "resolution": {"inferences": inferred_fields},
    }
    if intent_type == "update_task":
        payload = {
            "notion_page_id": canonical_fields.get("task_id"),
            "patch": canonical_fields.get("patch", {}),
        }
        action = "notion.tasks.update"
    else:
        payload = canonical_fields
        action = "notion.tasks.create"
    plan = [
        {
            "kind": "action",
            "action": action,
            "intent_id": packet.get("intent_id"),
            "correlation_id": packet.get("correlation_id"),
            "payload": payload,
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
            fields["project"] = choice_id
            fields.pop("project_id", None)
            fields["project_resolved"] = True
        elif answer_text:
            fields["project"] = answer_text
            fields.pop("project_id", None)
            fields["project_resolved"] = True
    elif field == "due":
        if answer_text:
            fields["due"] = answer_text
        elif choice_id:
            fields["due"] = choice_id

    canonical_draft.pop("pending", None)
    return canonical_draft
