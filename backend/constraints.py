"""
constraints.py - Constraint normalization, validation, and metadata helpers.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCOPE_TYPES = ("global", "division", "subject", "teacher", "room", "batch_group")
SCOPE_KEY_MAP = {
    "division_ids": "division",
    "subject_ids": "subject",
    "teacher_ids": "teacher",
    "room_ids": "room",
    "batch_group_ids": "batch_group",
}
LEGACY_TYPE_ALIASES = {
    "no_repeat_per_day": "once_per_day",
    "subject_edge_only": "start_or_end_only",
    "subject_end_only": "end_only",
}
CANONICAL_TYPES = {
    "avoid_day",
    "avoid_period",
    "prefer_day",
    "prefer_period",
    "max_per_day",
    "once_per_day",
    "start_or_end_only",
    "end_only",
    "prefer_end_of_day",
    "no_free_slots",
    "parallel_group",
}


def constraint_type_meta() -> List[Dict[str, Any]]:
    return [
        {
            "type": "avoid_day",
            "label": "Avoid Day",
            "default_priority": "hard",
            "params": [{"name": "days", "kind": "day_multi", "required": True}],
        },
        {
            "type": "avoid_period",
            "label": "Avoid Period",
            "default_priority": "hard",
            "params": [{"name": "period_ids", "kind": "period_multi", "required": True}],
        },
        {
            "type": "prefer_day",
            "label": "Prefer Day",
            "default_priority": "soft",
            "params": [{"name": "days", "kind": "day_multi", "required": True}],
        },
        {
            "type": "prefer_period",
            "label": "Prefer Period",
            "default_priority": "soft",
            "params": [{"name": "period_ids", "kind": "period_multi", "required": True}],
        },
        {
            "type": "max_per_day",
            "label": "Max Sessions Per Day",
            "default_priority": "hard",
            "params": [{"name": "max_per_day", "kind": "number", "required": True, "min": 1}],
        },
        {
            "type": "once_per_day",
            "label": "Once Per Day",
            "default_priority": "hard",
            "params": [],
        },
        {
            "type": "start_or_end_only",
            "label": "Start Or End Only",
            "default_priority": "hard",
            "params": [],
        },
        {
            "type": "end_only",
            "label": "End Only",
            "default_priority": "hard",
            "params": [],
        },
        {
            "type": "prefer_end_of_day",
            "label": "Prefer End Of Day",
            "default_priority": "soft",
            "params": [],
        },
        {
            "type": "no_free_slots",
            "label": "No Free Slots",
            "default_priority": "soft",
            "params": [],
        },
        {
            "type": "parallel_group",
            "label": "Parallel Group",
            "default_priority": "hard",
            "params": [
                {"name": "subject_ids", "kind": "subject_multi", "required": True},
                {"name": "slot_type", "kind": "select", "required": False, "options": ["lecture", "tutorial"]},
                {"name": "group_name", "kind": "text", "required": False},
                {"name": "session_count", "kind": "number", "required": False, "min": 1},
            ],
        },
    ]


def constraints_ui_meta(period_ids: List[str], days: List[str]) -> Dict[str, Any]:
    return {
        "scope_types": list(SCOPE_TYPES),
        "constraint_types": constraint_type_meta(),
        "period_ids": list(period_ids),
        "days": list(days),
    }


def canonical_constraint_type(value: Optional[str]) -> str:
    ctype = (value or "").strip()
    if not ctype:
        return ""
    return LEGACY_TYPE_ALIASES.get(ctype, ctype)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _clean_list(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value in ("", None):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _infer_scope(scope: Dict[str, List[str]], requested: Optional[str]) -> str:
    if requested in SCOPE_TYPES:
        return requested
    for key, scope_name in SCOPE_KEY_MAP.items():
        if scope.get(key):
            return scope_name
    return "global"


def _build_scope(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    raw_scope = deepcopy(payload.get("scope") or {})
    scope = {
        "division_ids": _clean_list(raw_scope.get("division_ids", _as_list(payload.get("division_id")))),
        "subject_ids": _clean_list(raw_scope.get("subject_ids", _as_list(payload.get("subject_id")))),
        "teacher_ids": _clean_list(raw_scope.get("teacher_ids", _as_list(payload.get("teacher_id")))),
        "room_ids": _clean_list(raw_scope.get("room_ids", _as_list(payload.get("room_id")))),
        "batch_group_ids": _clean_list(
            raw_scope.get("batch_group_ids", _as_list(payload.get("batch_group_id")))
        ),
    }
    return scope


def _merge_legacy_params(ctype: str, payload: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(params or {})
    if "day" in payload and payload.get("day") and not merged.get("days"):
        merged["days"] = [payload["day"]]
    if payload.get("days") and not merged.get("days"):
        merged["days"] = _clean_list(_as_list(payload.get("days")))
    if "period_id" in payload and payload.get("period_id") and not merged.get("period_ids"):
        merged["period_ids"] = [payload["period_id"]]
    if payload.get("period_ids") and not merged.get("period_ids"):
        merged["period_ids"] = _clean_list(_as_list(payload.get("period_ids")))

    if ctype == "max_per_day":
        merged["max_per_day"] = _to_int(
            merged.get("max_per_day", payload.get("value")),
            default=1,
        )
    if ctype == "parallel_group":
        merged["subject_ids"] = _clean_list(
            _as_list(merged.get("subject_ids") or payload.get("subject_ids") or payload.get("parallel_subject_ids"))
        )
        merged["session_count"] = _to_int(merged.get("session_count", payload.get("value")))
        merged["slot_type"] = str(merged.get("slot_type", payload.get("slot_type", "lecture")) or "lecture")
        merged["group_name"] = str(merged.get("group_name", payload.get("group_name", "")) or "").strip()

    if ctype in {"avoid_day", "prefer_day"}:
        merged["days"] = _clean_list(_as_list(merged.get("days")))
    if ctype in {"avoid_period", "prefer_period"}:
        merged["period_ids"] = _clean_list(_as_list(merged.get("period_ids")))
    return merged


def normalize_constraint_document(
    payload: Dict[str, Any],
    *,
    strict: bool = False,
    now: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    raw = deepcopy(payload or {})
    ctype = canonical_constraint_type(raw.get("constraint_type"))
    errors: List[str] = []
    if not ctype:
        errors.append("constraint_type is required")
    elif ctype not in CANONICAL_TYPES:
        errors.append(f"Unsupported constraint type: {ctype}")

    scope = _build_scope(raw)
    target_scope = _infer_scope(scope, str(raw.get("target_scope", "") or "").strip() or None)
    if target_scope not in SCOPE_TYPES:
        errors.append(f"Unsupported target_scope: {target_scope}")
    if target_scope != "global":
        required_key = next((key for key, scope_name in SCOPE_KEY_MAP.items() if scope_name == target_scope), None)
        if required_key and not scope.get(required_key):
            errors.append(f"target_scope '{target_scope}' requires at least one target id")

    priority = str(raw.get("priority", "") or "").strip().lower()
    if priority not in {"hard", "soft"}:
        priority = "hard" if _as_bool(raw.get("is_hard"), default=True) else "soft"
    weight = _to_int(raw.get("weight", raw.get("penalty_weight")), default=10) or 10
    active = _as_bool(raw.get("active", raw.get("is_active")), default=True)

    params = _merge_legacy_params(ctype, raw, raw.get("params") or {})

    if ctype in {"avoid_day", "prefer_day"} and not params.get("days"):
        errors.append("At least one day is required")
    if ctype in {"avoid_period", "prefer_period"} and not params.get("period_ids"):
        errors.append("At least one period is required")
    if ctype == "max_per_day":
        max_per_day = _to_int(params.get("max_per_day"), default=0) or 0
        params["max_per_day"] = max_per_day
        if max_per_day <= 0:
            errors.append("max_per_day must be greater than 0")
    if ctype == "parallel_group":
        params["subject_ids"] = _clean_list(_as_list(params.get("subject_ids")))
        if len(params["subject_ids"]) < 2:
            errors.append("Parallel group must contain at least two subjects")
        slot_type = str(params.get("slot_type", "lecture") or "lecture").strip().lower()
        if slot_type not in {"lecture", "tutorial"}:
            errors.append("parallel_group slot_type must be lecture or tutorial")
        params["slot_type"] = slot_type
        if target_scope == "global" and not scope.get("division_ids"):
            errors.append("Parallel groups must target at least one division")

    fatal_error = (not ctype) or (ctype not in CANONICAL_TYPES) or (target_scope not in SCOPE_TYPES)
    if fatal_error:
        return None, errors

    if strict and errors:
        return None, errors

    timestamp = now or raw.get("updated_at") or datetime.now(timezone.utc).isoformat()
    normalized = {
        "constraint_type": ctype,
        "description": str(raw.get("description") or "").strip(),
        "target_scope": target_scope,
        "scope": scope,
        "priority": priority,
        "is_hard": 1 if priority == "hard" else 0,
        "weight": weight,
        "penalty_weight": weight,
        "params": params,
        "active": 1 if active else 0,
        "created_at": raw.get("created_at") or timestamp,
        "updated_at": timestamp,
    }

    if raw.get("int_id") is not None:
        normalized["int_id"] = int(raw["int_id"])

    # Backward-compatible mirrors for existing UI/API consumers.
    normalized["division_id"] = scope["division_ids"][0] if scope["division_ids"] else None
    normalized["subject_id"] = scope["subject_ids"][0] if scope["subject_ids"] else None
    normalized["teacher_id"] = scope["teacher_ids"][0] if scope["teacher_ids"] else None
    normalized["room_id"] = scope["room_ids"][0] if scope["room_ids"] else None
    normalized["batch_group_id"] = (
        scope["batch_group_ids"][0] if scope["batch_group_ids"] else None
    )
    normalized["days"] = list(params.get("days") or [])
    normalized["period_ids"] = list(params.get("period_ids") or [])
    if ctype == "max_per_day":
        normalized["value"] = params.get("max_per_day")
    elif ctype == "parallel_group":
        normalized["value"] = params.get("session_count")
        normalized["subject_ids"] = list(params.get("subject_ids") or [])
    else:
        normalized["value"] = raw.get("value")

    return normalized, errors


def normalize_constraint_list(rows: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    normalized: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for row in rows:
        item, errors = normalize_constraint_document(row)
        if item is None:
            rejected.append({"row": row, "errors": errors})
            continue
        if errors:
            item["normalization_errors"] = errors
        normalized.append(item)
    normalized.sort(
        key=lambda c: (
            0 if c.get("priority") == "hard" else 1,
            c.get("constraint_type") or "",
            c.get("description") or "",
            c.get("int_id", 0),
        )
    )
    return normalized, rejected


def constraint_matches(
    constraint: Dict[str, Any],
    *,
    division_id: Optional[str] = None,
    subject_id: Optional[str] = None,
    teacher_id: Optional[str] = None,
    room_id: Optional[str] = None,
    batch_group_id: Optional[str] = None,
) -> bool:
    scope = constraint.get("scope") or {}
    checks = [
        ("division_ids", division_id),
        ("subject_ids", subject_id),
        ("teacher_ids", teacher_id),
        ("room_ids", room_id),
        ("batch_group_ids", batch_group_id),
    ]
    for key, current in checks:
        wanted = scope.get(key) or []
        if wanted and current not in wanted:
            return False
    return True


def active_constraints(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized, _ = normalize_constraint_list(rows)
    return [c for c in normalized if c.get("active")]
