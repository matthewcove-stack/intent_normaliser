from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional
from zoneinfo import ZoneInfo
import re
import uuid

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


class TaskResolver:
    def resolve(self, query: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


class StubTaskResolver(TaskResolver):
    def resolve(self, query: str) -> List[Dict[str, Any]]:
        return []


class HttpTaskResolver(TaskResolver):
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: Optional[str] = None,
        search_path: str = "/v1/notion/search",
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._search_path = search_path
        self._timeout_seconds = timeout_seconds

    def resolve(self, query: str) -> List[Dict[str, Any]]:
        url = f"{self._base_url}{self._search_path}"
        headers = {"Content-Type": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        payload = {
            "request_id": f"task-search:{uuid.uuid4()}",
            "actor": "intent_normaliser",
            "payload": {
                "query": query,
                "limit": 5,
                "types": ["page"],
            },
        }
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
        body = data.get("data") if isinstance(data, dict) else None
        results = body.get("results") if isinstance(body, dict) else []
        if not isinstance(results, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            title = item.get("title")
            if not isinstance(item_id, str) or not item_id.strip():
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            normalized.append(
                {
                    "id": item_id,
                    "title": title,
                    "status": item.get("status"),
                    "due": item.get("due"),
                    "url": item.get("url"),
                    "score": item.get("score"),
                }
            )
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

_SHOPPING_VERBS = ("buy", "get", "pick up", "pickup", "order")
_SHOPPING_PREFIXES = ("shopping", "add to shopping")
_SHOPPING_KEYWORDS = ("screwfix", "toolstation", "amazon")
_TASK_IMPERATIVE_VERBS = {
    "call",
    "book",
    "fix",
    "send",
    "write",
    "review",
    "plan",
    "check",
    "schedule",
    "email",
    "message",
    "organise",
    "organize",
}
_STATUS_INTENT_PATTERNS: List[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*mark\s+(.+?)\s+done\s*$", re.IGNORECASE), "Done"),
    (re.compile(r"^\s*start\s+(.+?)\s*$", re.IGNORECASE), "In Progress"),
    (re.compile(r"^\s*pause\s+(.+?)\s*$", re.IGNORECASE), "Todo"),
]
_URL_PATTERN = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)


def _extract_status_update_command(natural_language: str) -> Optional[tuple[str, str]]:
    text = natural_language.strip()
    for pattern, status_name in _STATUS_INTENT_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        selector = match.group(1).strip()
        if selector:
            return selector, status_name
    return None


def _tokenize_text(value: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    return {token for token in tokens if token}


def _task_title_score(query: str, title: str) -> float:
    query_clean = query.strip().lower()
    title_clean = title.strip().lower()
    if not query_clean or not title_clean:
        return 0.0
    if query_clean == title_clean:
        return 0.99
    if title_clean.startswith(query_clean):
        return 0.96
    if query_clean in title_clean:
        return 0.92
    query_tokens = _tokenize_text(query_clean)
    title_tokens = _tokenize_text(title_clean)
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens.intersection(title_tokens)) / len(query_tokens)
    if overlap >= 0.8:
        return 0.90
    if overlap >= 0.6:
        return 0.85
    if overlap >= 0.4:
        return 0.78
    return 0.60


def _normalize_task_candidates(candidates: Iterable[Dict[str, Any]], selector: str) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        task_id = candidate.get("id") or candidate.get("page_id")
        title = candidate.get("title") or candidate.get("label") or candidate.get("name")
        if not isinstance(task_id, str) or not task_id.strip():
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        score_raw = candidate.get("score")
        if score_raw is None and candidate.get("confidence") is not None:
            score_raw = candidate.get("confidence")
        if score_raw is None:
            score = _task_title_score(selector, title)
        else:
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                score = _task_title_score(selector, title)
        meta: Dict[str, Any] = {"score": score}
        status_value = candidate.get("status")
        due_value = candidate.get("due")
        url_value = candidate.get("url")
        if status_value is not None:
            meta["status"] = status_value
        if due_value is not None:
            meta["due"] = due_value
        if url_value is not None:
            meta["url"] = url_value
        normalized.append(
            {
                "id": task_id,
                "label": title,
                "score": score,
                "meta": meta,
            }
        )
    normalized.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return normalized[:5]


def _extract_urls(value: Optional[str]) -> List[str]:
    if not isinstance(value, str):
        return []
    return [match.group(1).rstrip(").,;") for match in _URL_PATTERN.finditer(value)]


def _is_attachment(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("filename"), str) and bool(value.get("filename").strip())


def _build_note_fields_from_rich_capture(fields: Dict[str, Any], natural_language: Optional[str]) -> Optional[Dict[str, Any]]:
    tags: List[str] = []
    title: Optional[str] = None
    content_parts: List[str] = []
    urls: List[str] = []
    field_url = fields.get("url")
    if isinstance(field_url, str) and field_url.strip():
        urls.append(field_url.strip())
    field_urls = fields.get("urls")
    if isinstance(field_urls, list):
        urls.extend([value.strip() for value in field_urls if isinstance(value, str) and value.strip()])
    urls.extend(_extract_urls(natural_language))
    dedup_urls = list(dict.fromkeys(urls))
    if dedup_urls:
        tags.append("url")
        title = fields.get("title") if isinstance(fields.get("title"), str) else None
        if not title:
            title = f"URL: {dedup_urls[0][:72]}"
        content_parts.append("URLs:")
        for value in dedup_urls:
            content_parts.append(f"- {value}")
        comment = fields.get("url_comment")
        if isinstance(comment, str) and comment.strip():
            content_parts.append("")
            content_parts.append("Comment:")
            content_parts.append(comment.strip())

    attachment = fields.get("attachment")
    if _is_attachment(attachment):
        attachment_payload = dict(attachment)
        tags.append("file")
        filename = str(attachment_payload.get("filename")).strip()
        mime = str(attachment_payload.get("mime") or "application/octet-stream")
        size = attachment_payload.get("size")
        sha256 = str(attachment_payload.get("sha256") or "")
        if not title:
            title = f"File: {filename}"
        content_parts.append("File Metadata:")
        content_parts.append(f"- filename: {filename}")
        content_parts.append(f"- mime: {mime}")
        if size is not None:
            content_parts.append(f"- size: {size}")
        if sha256:
            content_parts.append(f"- sha256: {sha256}")
        extracted_text = attachment_payload.get("text")
        if isinstance(extracted_text, str) and extracted_text.strip():
            content_parts.append("")
            content_parts.append("Extracted Text:")
            content_parts.append("```text")
            content_parts.append(extracted_text.strip())
            content_parts.append("```")

    if not content_parts:
        return None
    if not title:
        if isinstance(natural_language, str) and natural_language.strip():
            title = natural_language.strip()[:80]
        else:
            title = "Captured note"
    return {
        "title": title,
        "content": "\n".join(content_parts).strip(),
        "tags": list(dict.fromkeys(tags)),
        "url": dedup_urls[0] if dedup_urls else None,
    }


def _infer_auto_intent_type(natural_language: str) -> tuple[str, float]:
    text = natural_language.strip()
    lowered = text.lower()

    if any(lowered.startswith(prefix) for prefix in _SHOPPING_PREFIXES):
        return "add_list_item", 0.9
    if any(keyword in lowered for keyword in _SHOPPING_KEYWORDS):
        return "add_list_item", 0.9
    if any(
        re.search(rf"\b{re.escape(verb)}\b", lowered) is not None
        for verb in _SHOPPING_VERBS
    ):
        return "add_list_item", 0.9

    if "to do" in lowered or lowered.startswith("todo "):
        return "create_task", 0.7
    first_word = lowered.split(" ", 1)[0]
    if first_word in _TASK_IMPERATIVE_VERBS:
        return "create_task", 0.9

    return "capture_note", 0.8


def _intent_type_clarification() -> ClarificationPayload:
    return ClarificationPayload(
        question="What should I create from this capture?",
        expected_answer_type="choice",
        candidates=[
            {"id": "create_task", "label": "Task"},
            {"id": "capture_note", "label": "Note"},
            {"id": "add_list_item", "label": "Shopping list item"},
        ],
    )


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
    task_resolver: Optional[TaskResolver] = None,
    project_resolution_threshold: float = 0.90,
    project_resolution_margin: float = 0.10,
    task_resolution_threshold: float = 0.90,
    task_resolution_margin: float = 0.10,
    min_confidence_to_write: float = 0.75,
    max_inferred_fields: int = 2,
) -> NormalizationResult:
    if task_resolver is None:
        task_resolver = StubTaskResolver()
    intent_type = packet.get("intent_type")
    target = packet.get("target") if isinstance(packet.get("target"), dict) else {}
    fields = packet.get("fields") or {}
    confidence = packet.get("confidence")
    natural_language = packet.get("natural_language")

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

    target_kind = target.get("kind")
    target_key = target.get("key")

    if not intent_type and target_kind not in {"list", "notes"}:
        rich_capture_fields = _build_note_fields_from_rich_capture(fields, natural_language)
        if rich_capture_fields:
            intent_type = "capture_note"
            fields = {**fields, **{key: value for key, value in rich_capture_fields.items() if value is not None}}

    if target_kind == "list" and target_key == "shopping_list":
        item_value = fields.get("item") or fields.get("title") or packet.get("natural_language")
        if not isinstance(item_value, str) or not item_value.strip():
            return NormalizationResult(
                status="rejected",
                error_code="VALIDATION_ERROR",
                message="Missing required field: item",
                details={"field": "item"},
            )

        final_canonical = {
            "intent_type": "add_list_item",
            "fields": {
                "list_key": "shopping_list",
                "item": item_value.strip(),
                "notes": fields.get("notes"),
            },
            "resolution": {"inferences": []},
        }
        plan = [
            {
                "kind": "action",
                "action": "notion.list.add_item",
                "intent_id": packet.get("intent_id"),
                "correlation_id": packet.get("correlation_id"),
                "payload": final_canonical["fields"],
            }
        ]
        return NormalizationResult(
            status="ready",
            final_canonical=final_canonical,
            canonical_draft=final_canonical,
            plan=plan,
        )

    if target_kind == "notes":
        note_value = fields.get("content") or fields.get("note") or packet.get("natural_language")
        if not isinstance(note_value, str) or not note_value.strip():
            return NormalizationResult(
                status="rejected",
                error_code="VALIDATION_ERROR",
                message="Missing required field: note content",
                details={"field": "content"},
            )

        title_value = fields.get("title")
        if not isinstance(title_value, str) or not title_value.strip():
            title_value = note_value.strip()[:80]

        note_fields = {
            "title": title_value,
            "content": note_value.strip(),
        }
        if fields.get("tags") is not None:
            note_fields["tags"] = fields.get("tags")
        if fields.get("url") is not None:
            note_fields["url"] = fields.get("url")
        final_canonical = {
            "intent_type": "capture_note",
            "fields": note_fields,
            "resolution": {"inferences": []},
        }
        plan = [
            {
                "kind": "action",
                "action": "notion.note.capture",
                "intent_id": packet.get("intent_id"),
                "correlation_id": packet.get("correlation_id"),
                "payload": final_canonical["fields"],
            }
        ]
        return NormalizationResult(
            status="ready",
            final_canonical=final_canonical,
            canonical_draft=final_canonical,
            plan=plan,
        )

    if not intent_type and not target_kind:
        if isinstance(natural_language, str) and natural_language.strip():
            status_update = _extract_status_update_command(natural_language)
            fields = dict(fields)
            if status_update:
                selector, desired_status = status_update
                candidates = _normalize_task_candidates(task_resolver.resolve(selector), selector)
                resolved = _select_high_confidence_candidate(
                    candidates,
                    threshold=task_resolution_threshold,
                    margin=task_resolution_margin,
                )
                if resolved:
                    intent_type = "update_task"
                    fields["task_id"] = resolved.get("id")
                    fields["status"] = desired_status
                else:
                    canonical_draft = {
                        "intent_type": "update_task",
                        "fields": {
                            "task_id": {"selector": selector, "value": None},
                            "patch": {"status": desired_status},
                        },
                        "pending": {
                            "field": "task_id",
                            "selector": selector,
                            "desired_status": desired_status,
                        },
                    }
                    expected_answer_type = "choice" if candidates else "free_text"
                    question = (
                        f"Which task matches '{selector}'?"
                        if candidates
                        else f"Provide the task id or exact title for '{selector}'."
                    )
                    return NormalizationResult(
                        status="needs_clarification",
                        canonical_draft=canonical_draft,
                        clarification=ClarificationPayload(
                            question=question,
                            expected_answer_type=expected_answer_type,
                            candidates=candidates,
                        ),
                    )
            else:
                inferred_intent_type, inferred_confidence = _infer_auto_intent_type(natural_language)
                if inferred_confidence < min_confidence_to_write:
                    canonical_draft = {
                        "intent_type": None,
                        "fields": fields,
                        "pending": {"field": "intent_type"},
                        "inference": {"intent_type": inferred_intent_type, "confidence": inferred_confidence},
                    }
                    return NormalizationResult(
                        status="needs_clarification",
                        canonical_draft=canonical_draft,
                        clarification=_intent_type_clarification(),
                    )
                intent_type = inferred_intent_type
                if intent_type == "add_list_item":
                    fields["item"] = fields.get("item") or natural_language.strip()
                elif intent_type == "create_task":
                    fields["title"] = fields.get("title") or natural_language.strip()
                    fields["status"] = fields.get("status") or "Todo"
                elif intent_type == "capture_note":
                    trimmed = natural_language.strip()
                    fields["title"] = fields.get("title") or trimmed[:80]
                    fields["content"] = fields.get("content") or trimmed
        else:
            canonical_draft = {
                "intent_type": None,
                "fields": fields,
                "pending": {"field": "intent_type"},
            }
            return NormalizationResult(
                status="needs_clarification",
                canonical_draft=canonical_draft,
                clarification=_intent_type_clarification(),
            )

    if intent_type == "add_list_item":
        item_value = fields.get("item") or fields.get("title") or packet.get("natural_language")
        if not isinstance(item_value, str) or not item_value.strip():
            return NormalizationResult(
                status="rejected",
                error_code="VALIDATION_ERROR",
                message="Missing required field: item",
                details={"field": "item"},
            )
        final_canonical = {
            "intent_type": "add_list_item",
            "fields": {
                "list_key": "shopping_list",
                "item": item_value.strip(),
                "notes": fields.get("notes"),
            },
            "resolution": {"inferences": []},
        }
        plan = [
            {
                "kind": "action",
                "action": "notion.list.add_item",
                "intent_id": packet.get("intent_id"),
                "correlation_id": packet.get("correlation_id"),
                "payload": final_canonical["fields"],
            }
        ]
        return NormalizationResult(
            status="ready",
            final_canonical=final_canonical,
            canonical_draft=final_canonical,
            plan=plan,
        )

    if intent_type == "capture_note":
        note_value = fields.get("content") or fields.get("note") or packet.get("natural_language")
        if not isinstance(note_value, str) or not note_value.strip():
            return NormalizationResult(
                status="rejected",
                error_code="VALIDATION_ERROR",
                message="Missing required field: note content",
                details={"field": "content"},
            )
        title_value = fields.get("title")
        if not isinstance(title_value, str) or not title_value.strip():
            title_value = note_value.strip()[:80]
        note_fields = {
            "title": title_value,
            "content": note_value.strip(),
        }
        if fields.get("tags") is not None:
            note_fields["tags"] = fields.get("tags")
        if fields.get("url") is not None:
            note_fields["url"] = fields.get("url")
        final_canonical = {
            "intent_type": "capture_note",
            "fields": note_fields,
            "resolution": {"inferences": []},
        }
        plan = [
            {
                "kind": "action",
                "action": "notion.note.capture",
                "intent_id": packet.get("intent_id"),
                "correlation_id": packet.get("correlation_id"),
                "payload": final_canonical["fields"],
            }
        ]
        return NormalizationResult(
            status="ready",
            final_canonical=final_canonical,
            canonical_draft=final_canonical,
            plan=plan,
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
        canonical_fields["status"] = fields.get("status") or "Todo"
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
    elif field == "task_id":
        if choice_id:
            fields["task_id"] = choice_id
        elif answer_text:
            fields["task_id"] = answer_text
        desired_status = pending.get("desired_status")
        if desired_status and not fields.get("status"):
            fields["status"] = desired_status

    canonical_draft.pop("pending", None)
    return canonical_draft
