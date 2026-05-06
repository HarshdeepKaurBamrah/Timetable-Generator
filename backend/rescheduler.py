"""
rescheduler.py - Runtime change engine, constraint-aware validation, and notifications.
"""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from constraints import normalize_constraint_document
from database import active_timetable_filter, get_db, next_seq
from solver import (
    ConstraintEvaluator,
    ScheduleState,
    SchedulingContext,
    SlotRecord,
    StandardRequest,
    TimetableSolver,
    build_state_from_active_timetable,
    load_active_slot_records,
)


def _get_period_ids(db) -> List[str]:
    from database import get_config_value

    return list(get_config_value(db, "period_ids", ["P1", "P2", "P3", "P4", "P5", "P6"]))


def _row_to_slot(row: Dict) -> SlotRecord:
    return SlotRecord(
        int_id=row.get("int_id"),
        division_id=row["division_id"],
        day=row["day"],
        period_id=row["period_id"],
        subject_id=row["subject_id"],
        teacher_id=row.get("teacher_id"),
        room_id=row.get("room_id"),
        slot_type=row["slot_type"],
        batch_id=row.get("batch_id"),
        parallel_group_id=row.get("parallel_group_id"),
        session_id=row.get("session_id") or f"SLOT:{row.get('int_id')}",
        occupancy_key=row.get("occupancy_key") or ("batch:" + row["batch_id"] if row.get("batch_id") else "division"),
        is_locked=bool(row.get("is_locked")),
        status=row.get("status", "active"),
        source=row.get("source", "runtime"),
    )


def _load_active_rows(db) -> List[Dict]:
    return list(db.timetable_slots.find(active_timetable_filter(db), {"_id": 0}))


def _get_slot(db, slot_id: int) -> Optional[Dict]:
    return db.timetable_slots.find_one(active_timetable_filter(db, {"int_id": slot_id}), {"_id": 0})


def _get_session_rows(db, session_id: str) -> List[Dict]:
    return list(db.timetable_slots.find(active_timetable_filter(db, {"session_id": session_id}), {"_id": 0}))


def _slots_by_id(rows: Iterable[Dict]) -> Dict[int, Dict]:
    return {row["int_id"]: row for row in rows}


def _ordered_session_rows(context: SchedulingContext, rows: List[Dict]) -> List[Dict]:
    period_index = {pid: idx for idx, pid in enumerate(context.period_ids)}
    return sorted(
        rows,
        key=lambda row: (
            row.get("day") or "",
            period_index.get(row.get("period_id"), 999),
            row.get("batch_id") or "",
            row.get("subject_id") or "",
            row.get("int_id") or 0,
        ),
    )


def _session_summary(context: SchedulingContext, rows: List[Dict]) -> Dict[str, Any]:
    ordered = _ordered_session_rows(context, rows)
    first = ordered[0]
    periods = list(dict.fromkeys(row["period_id"] for row in ordered))
    room_ids = list(dict.fromkeys(row.get("room_id") for row in ordered if row.get("room_id")))
    slot_ids = [row["int_id"] for row in ordered]
    return {
        "session_id": first["session_id"],
        "division_id": first["division_id"],
        "subject_id": first["subject_id"],
        "teacher_id": first.get("teacher_id"),
        "slot_type": first["slot_type"],
        "day": first["day"],
        "period_id": periods[0],
        "period_ids": periods,
        "slot_ids": slot_ids,
        "room_ids": room_ids,
        "batch_id": first.get("batch_id"),
        "parallel_group_id": first.get("parallel_group_id"),
    }


def _selection_summary(context: SchedulingContext, rows: List[Dict]) -> Dict[str, Any]:
    ordered = _ordered_session_rows(context, rows)
    first = ordered[0]
    periods = list(dict.fromkeys(row["period_id"] for row in ordered))
    subject_ids = list(dict.fromkeys(row["subject_id"] for row in ordered))
    teacher_ids = list(dict.fromkeys(row.get("teacher_id") for row in ordered if row.get("teacher_id")))
    room_ids = list(dict.fromkeys(row.get("room_id") for row in ordered if row.get("room_id")))
    session_ids = list(dict.fromkeys(row["session_id"] for row in ordered))
    batch_assignments = []
    seen_batches = set()
    start_period = periods[0] if periods else None
    for row in ordered:
        batch_id = row.get("batch_id")
        if not batch_id or batch_id in seen_batches:
            continue
        if start_period and row["period_id"] != start_period:
            continue
        seen_batches.add(batch_id)
        batch_assignments.append(
            {
                "batch_id": batch_id,
                "subject_id": row.get("subject_id"),
                "teacher_id": row.get("teacher_id"),
                "room_id": row.get("room_id"),
                "session_id": row["session_id"],
            }
        )
    return {
        "primary_slot_id": first["int_id"],
        "division_id": first["division_id"],
        "day": first["day"],
        "period_ids": periods,
        "slot_ids": [row["int_id"] for row in ordered],
        "session_ids": session_ids,
        "subject_ids": subject_ids,
        "teacher_ids": teacher_ids,
        "room_ids": room_ids,
        "slot_type": first["slot_type"],
        "parallel_group_id": first.get("parallel_group_id"),
        "batch_assignments": batch_assignments,
    }


def _unique_session_rows(db, slot_ids: List[int]) -> Tuple[List[Dict], List[str]]:
    active_rows = _slots_by_id(_load_active_rows(db))
    selected_rows = [active_rows[slot_id] for slot_id in dict.fromkeys(slot_ids) if slot_id in active_rows]
    expanded_rows: List[Dict] = []
    seen_ids = set()
    for row in selected_rows:
        if row.get("parallel_group_id"):
            related = [
                other for other in active_rows.values()
                if other.get("parallel_group_id") == row.get("parallel_group_id")
                and other["division_id"] == row["division_id"]
                and other["day"] == row["day"]
                and other["period_id"] == row["period_id"]
            ]
        elif row.get("slot_type") == "lab":
            related = [
                other for other in active_rows.values()
                if other.get("slot_type") == "lab"
                and other["division_id"] == row["division_id"]
                and other["day"] == row["day"]
                and other["period_id"] == row["period_id"]
            ]
        else:
            related = [row]
        for item in related:
            if item["int_id"] in seen_ids:
                continue
            seen_ids.add(item["int_id"])
            expanded_rows.append(item)
    session_ids = list(dict.fromkeys(row["session_id"] for row in expanded_rows))
    rows: List[Dict] = []
    for session_id in session_ids:
        rows.extend(_get_session_rows(db, session_id))
    return rows, session_ids


def _candidate_cover_teachers(context: SchedulingContext, rows: List[Dict], absent_teacher_id: str) -> List[Dict[str, str]]:
    session_ids = list(dict.fromkeys(row["session_id"] for row in rows))
    base_state = _rebuild_state_without_sessions(context, session_ids)
    candidates: List[Dict[str, str]] = []
    for teacher_id, teacher in sorted(context.teachers.items()):
        if teacher_id == absent_teacher_id:
            continue
        state = ScheduleState(context.period_ids, context.days)
        state.add_slots(base_state.slots)
        replacement_rows = [
            _row_to_slot({**row, "teacher_id": teacher_id, "status": "active", "source": "absence_cover"})
            for row in rows
        ]
        state.add_slots(replacement_rows)
        if _validation_message(context, state):
            continue
        candidates.append({"teacher_id": teacher_id, "teacher_name": teacher.get("name") or teacher_id})
    return candidates


def _mark_session_notifications_read(db, session_id: str):
    for notif in db.notifications.find({"type": {"$in": ["slot_free", "slot_claimed"]}}, {"_id": 0, "int_id": 1, "data": 1}):
        try:
            data = json.loads(notif.get("data") or "{}")
        except Exception:
            data = {}
        if data.get("session_id") == session_id:
            db.notifications.update_one({"int_id": notif["int_id"]}, {"$set": {"is_read": 1}})


def _period_span_label(period_ids: List[str]) -> str:
    if not period_ids:
        return ""
    if len(period_ids) == 1:
        return period_ids[0]
    return f"{period_ids[0]}-{period_ids[-1]}"


def _clone_state(context: SchedulingContext, source: ScheduleState) -> ScheduleState:
    cloned = ScheduleState(context.period_ids, context.days)
    cloned.add_slots(list(source.slots))
    return cloned


def _room_candidates(context: SchedulingContext, slot_type: str, preferred: List[Optional[str]]) -> List[str]:
    base = (
        [room["id"] for room in sorted(context.lab_rooms, key=lambda room: room["id"])]
        if slot_type == "lab"
        else [room["id"] for room in sorted(context.lecture_rooms, key=lambda room: room["id"])]
    )
    ordered: List[str] = []
    for room_id in preferred:
        if room_id and room_id in base and room_id not in ordered:
            ordered.append(room_id)
    for room_id in base:
        if room_id not in ordered:
            ordered.append(room_id)
    return ordered


def _first_available_room(
    context: SchedulingContext,
    state: ScheduleState,
    slot_type: str,
    day: str,
    periods: List[str],
    preferred: List[Optional[str]],
) -> Optional[str]:
    for room_id in _room_candidates(context, slot_type, preferred):
        if all(not state.room_entries(room_id, day, period_id) for period_id in periods):
            return room_id
    return None


def _build_cell_move_rows(
    context: SchedulingContext,
    rows: List[Dict],
    to_day: str,
    to_period: str,
    base_state: Optional[ScheduleState] = None,
) -> Tuple[Optional[List[SlotRecord]], Optional[str]]:
    if not rows:
        return None, "No timetable slots were selected"
    period_ids = context.period_ids
    if to_period not in period_ids:
        return None, f"Period {to_period} does not exist"
    period_index = {pid: idx for idx, pid in enumerate(period_ids)}
    evaluator = ConstraintEvaluator(context)
    session_ids = list(dict.fromkeys(row["session_id"] for row in rows))
    working_state = _clone_state(context, base_state or _rebuild_state_without_sessions(context, session_ids))
    grouped: Dict[str, List[Dict]] = {}
    for row in rows:
        grouped.setdefault(row["session_id"], []).append(row)
    moved: List[SlotRecord] = []
    target_start_idx = period_index[to_period]
    for session_rows in grouped.values():
        first = session_rows[0]
        if first.get("is_locked"):
            return None, "Locked sessions cannot be moved"
        if first["slot_type"] == "lab":
            if target_start_idx >= len(period_ids) - 1:
                return None, "Lab sessions need two consecutive periods"
            if evaluator.session_crosses_break(target_start_idx, 2):
                return None, "Labs cannot cross a configured break or lunch boundary"
            target_periods = period_ids[target_start_idx : target_start_idx + 2]
            room_id = _first_available_room(
                context,
                working_state,
                "lab",
                to_day,
                target_periods,
                [first.get("room_id"), context.subjects.get(first["subject_id"], {}).get("lab_room_id")],
            )
            if not room_id:
                return None, f"No lab room is free on {to_day} {_period_span_label(target_periods)}"
            current_start_idx = min(period_index[row["period_id"]] for row in session_rows)
            built_rows: List[SlotRecord] = []
            for row in session_rows:
                offset = period_index[row["period_id"]] - current_start_idx
                built_rows.append(_row_to_slot({**row, "day": to_day, "period_id": period_ids[target_start_idx + offset], "room_id": room_id}))
            moved.extend(built_rows)
            working_state.add_slots(built_rows)
        else:
            room_id = _first_available_room(
                context,
                working_state,
                "lecture",
                to_day,
                [to_period],
                [first.get("room_id"), context.divisions.get(first["division_id"], {}).get("room_id")],
            )
            if not room_id:
                return None, f"No lecture room is free on {to_day} {to_period}"
            built_rows: List[SlotRecord] = []
            for row in session_rows:
                built_rows.append(_row_to_slot({**row, "day": to_day, "period_id": to_period, "room_id": room_id}))
            moved.extend(built_rows)
            working_state.add_slots(built_rows)
    return moved, None


def validate_or_apply_cell_move(slot_ids: List[int], to_day: str, to_period: str, *, apply_change: bool = False, changed_by: str = None) -> Tuple[bool, str, Dict[str, Any]]:
    db = get_db()
    rows, session_ids = _unique_session_rows(db, slot_ids)
    if not rows:
        return False, "Selected timetable cell could not be found", {}
    context = SchedulingContext.from_db(db)
    moved_rows, build_error = _build_cell_move_rows(context, rows, to_day, to_period)
    if build_error:
        return False, build_error, {}
    state = _rebuild_state_without_sessions(context, session_ids)
    state.add_slots(moved_rows or [])
    failure = _validation_message(context, state)
    payload = {
        "slot_ids": list(dict.fromkeys(slot_ids)),
        "session_ids": session_ids,
        "day": to_day,
        "period_id": to_period,
        "slot_type": rows[0]["slot_type"],
        "session_count": len(session_ids),
    }
    if failure:
        return False, failure, payload
    if not apply_change:
        return True, "Target slot is valid", payload
    rows_by_id = _slots_by_id(rows)
    for moved in moved_rows or []:
        original = rows_by_id.get(moved.int_id)
        db.timetable_slots.update_one(
            active_timetable_filter(db, {"int_id": moved.int_id}),
            {"$set": {"day": moved.day, "period_id": moved.period_id, "source": "manual"}},
        )
        payload.setdefault("moved", []).append(
            {
                "slot_id": moved.int_id,
                "from": f"{original['day']} {original['period_id']}" if original else None,
                "to": f"{moved.day} {moved.period_id}",
            }
        )
    _log_change(
        db,
        "cell_move",
        f"Moved {len(session_ids)} session(s) to {to_day} {to_period}",
        payload["slot_ids"],
        "direct",
        True,
        changed_by=changed_by,
    )
    return True, "Timetable updated successfully", payload


def _log_change(db, ctype, desc, affected, resolved_by, success, reason="", changed_by=None):
    iid = next_seq(db, "change_log")
    db.change_log.insert_one(
        {
            "int_id": iid,
            "change_type": ctype,
            "description": desc,
            "affected": json.dumps(affected),
            "resolved_by": resolved_by,
            "success": 1 if success else 0,
            "reason": reason,
            "changed_by": changed_by,
            "created_at": datetime.now().isoformat(),
        }
    )


def _push_notification(db, ntype, title, message, data=None, for_role=None, for_teacher=None):
    iid = next_seq(db, "notifications")
    db.notifications.insert_one(
        {
            "int_id": iid,
            "type": ntype,
            "title": title,
            "message": message,
            "data": json.dumps(data or {}),
            "for_role": for_role,
            "for_teacher": for_teacher,
            "is_read": 0,
            "created_at": datetime.now().isoformat(),
        }
    )


def _validation_message(context: SchedulingContext, state: ScheduleState) -> Optional[str]:
    issues = ConstraintEvaluator(context).validate_state(state)
    if not issues:
        return None
    return issues[0].message


def _rebuild_state_without_sessions(context: SchedulingContext, remove_session_ids: List[str]) -> ScheduleState:
    state = build_state_from_active_timetable(context.db, context)
    for session_id in remove_session_ids:
        for slot in list(state.session_slots.get(session_id, [])):
            state.remove_slots([slot])
    return state


def _find_runtime_candidate(context: SchedulingContext, slot_row: Dict, exclude_day: Optional[str] = None) -> Optional[Tuple[str, str]]:
    solver = TimetableSolver(period_ids=context.period_ids, days=context.days, db=context.db)
    solver.state = _rebuild_state_without_sessions(context, [slot_row["session_id"]])
    request = StandardRequest(
        request_id=slot_row["session_id"],
        division_id=slot_row["division_id"],
        subject_id=slot_row["subject_id"],
        teacher_id=slot_row["teacher_id"],
        slot_type=slot_row["slot_type"],
        occurrence_index=1,
        preferred_room_id=slot_row.get("room_id") or context.divisions.get(slot_row["division_id"], {}).get("room_id"),
    )
    candidates, _ = solver._standard_candidates(request)
    for candidate in candidates:
        target = candidate.slots[0]
        if exclude_day and target.day == exclude_day:
            continue
        if target.day == slot_row["day"] and target.period_id == slot_row["period_id"]:
            continue
        return target.day, target.period_id
    return None


def move_slot(slot_id: int, to_day: str, to_period: str, changed_by: str = None) -> Tuple[bool, str]:
    db = get_db()
    slot = _get_slot(db, slot_id)
    if not slot:
        return False, "Slot not found"
    if slot.get("is_locked"):
        return False, "Slot is locked and cannot be moved"
    if slot.get("slot_type") == "lab":
        return False, "Lab sessions cannot be moved with the single-slot move action"
    if slot.get("parallel_group_id"):
        return False, "Parallel-group sessions must remain synchronized and cannot be moved individually"

    context = SchedulingContext.from_db(db)
    rows, session_ids = _unique_session_rows(db, [slot_id])
    state = _rebuild_state_without_sessions(context, session_ids)
    moved_rows, build_error = _build_cell_move_rows(context, rows, to_day, to_period, state)
    if build_error:
        return False, build_error
    state.add_slots(moved_rows or [])
    failure = _validation_message(context, state)
    if failure:
        return False, failure

    for moved in moved_rows or []:
        db.timetable_slots.update_one(
            active_timetable_filter(db, {"int_id": moved.int_id}),
            {"$set": {"day": moved.day, "period_id": moved.period_id, "room_id": moved.room_id, "source": "manual"}},
        )
    _log_change(db, "move_slot", f"Moved {slot['subject_id']} Div {slot['division_id']} {slot['day']} {slot['period_id']} -> {to_day} {to_period}", [slot_id], "direct", True, changed_by=changed_by)
    return True, f"Moved to {to_day} {to_period}"


def validate_or_apply_swap(
    slot_id_1: int,
    slot_id_2: int,
    *,
    apply_change: bool = False,
    changed_by: str = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    db = get_db()
    rows_1, session_ids_1 = _unique_session_rows(db, [slot_id_1])
    rows_2, session_ids_2 = _unique_session_rows(db, [slot_id_2])
    if not rows_1 or not rows_2:
        return False, "One or both slots not found", {}
    context = SchedulingContext.from_db(db)
    summary_1 = _selection_summary(context, rows_1)
    summary_2 = _selection_summary(context, rows_2)
    payload = {
        "slot_id_1": slot_id_1,
        "slot_id_2": slot_id_2,
        "from": summary_1,
        "to": summary_2,
    }
    if summary_1["division_id"] != summary_2["division_id"]:
        return False, "Can only swap within the same division", payload
    if set(session_ids_1) & set(session_ids_2):
        return False, "Pick two different timetable sessions to swap", payload
    if any(row.get("is_locked") for row in rows_1 + rows_2):
        return False, "Locked slots cannot be swapped", payload
    if len(summary_1["period_ids"]) != len(summary_2["period_ids"]):
        return False, "Only sessions with the same duration can be swapped", payload

    base_state = _rebuild_state_without_sessions(context, session_ids_1 + session_ids_2)
    moved_1, error_1 = _build_cell_move_rows(context, rows_1, summary_2["day"], summary_2["period_ids"][0], base_state)
    if error_1:
        return False, error_1, payload
    temp_state = _clone_state(context, base_state)
    temp_state.add_slots(moved_1 or [])
    moved_2, error_2 = _build_cell_move_rows(context, rows_2, summary_1["day"], summary_1["period_ids"][0], temp_state)
    if error_2:
        return False, error_2, payload

    state = _clone_state(context, base_state)
    state.add_slots(moved_1 or [])
    state.add_slots(moved_2 or [])
    failure = _validation_message(context, state)
    if failure:
        return False, failure, payload

    affected_ids = list(dict.fromkeys(summary_1["slot_ids"] + summary_2["slot_ids"]))
    payload["affected"] = affected_ids
    payload["moved"] = [
        {
            "slot_id": moved.int_id,
            "to": f"{moved.day} {moved.period_id}",
        }
        for moved in list((moved_1 or []) + (moved_2 or []))
    ]
    if not apply_change:
        return True, "Swap is feasible", payload

    staged_rows = list((moved_1 or []) + (moved_2 or []))
    temp_days = {moved.int_id: f"__swap__{uuid.uuid4().hex[:10]}_{moved.int_id}" for moved in staged_rows}
    for moved in staged_rows:
        db.timetable_slots.update_one(
            active_timetable_filter(db, {"int_id": moved.int_id}),
            {"$set": {"day": temp_days[moved.int_id], "source": "manual"}},
        )
    for moved in staged_rows:
        db.timetable_slots.update_one(
            {"int_id": moved.int_id},
            {"$set": {"day": moved.day, "period_id": moved.period_id, "room_id": moved.room_id, "source": "manual"}},
        )
    _log_change(
        db,
        "swap_slots",
        f"Swapped {len(summary_1['session_ids'])} session(s) at {summary_1['day']} {_period_span_label(summary_1['period_ids'])}"
        f" with {len(summary_2['session_ids'])} session(s) at {summary_2['day']} {_period_span_label(summary_2['period_ids'])}",
        affected_ids,
        "direct",
        True,
        changed_by=changed_by,
    )
    return True, "Sessions swapped successfully", payload


def swap_slots(slot_id_1: int, slot_id_2: int, changed_by: str = None) -> Tuple[bool, str]:
    ok, msg, _ = validate_or_apply_swap(slot_id_1, slot_id_2, apply_change=True, changed_by=changed_by)
    return ok, msg


def teacher_absent(teacher_id: str, day: str, absent_period: str = None, changed_by: str = None) -> Dict:
    db = get_db()
    context = SchedulingContext.from_db(db)
    flt = active_timetable_filter(db, {"teacher_id": teacher_id, "day": day, "status": "active"})
    if absent_period:
        flt["period_id"] = absent_period
    affected = list(db.timetable_slots.find(flt, {"_id": 0}))
    teacher = db.teachers.find_one({"id": teacher_id}, {"_id": 0}) or {"name": teacher_id}
    if not affected:
        return {"success": True, "resolved": [], "freed_slots": [], "opened_sessions": [], "message": "No active sessions found for the selected absence window"}

    opened_sessions = []
    seen_session_ids = set()
    for slot in affected:
        session_id = slot["session_id"]
        if session_id in seen_session_ids:
            continue
        seen_session_ids.add(session_id)
        session_rows = _ordered_session_rows(context, _get_session_rows(db, session_id))
        if not session_rows:
            continue
        for row in session_rows:
            db.timetable_slots.update_one(
                active_timetable_filter(db, {"int_id": row["int_id"]}),
                {"$set": {"teacher_id": None, "status": "open_cover", "source": "absence"}},
            )
        summary = _selection_summary(context, session_rows)
        subject = context.subjects.get(session_rows[0]["subject_id"], {"name": session_rows[0]["subject_id"], "short_name": session_rows[0]["subject_id"]})
        candidates = _candidate_cover_teachers(context, session_rows, teacher_id)
        payload = {
            "session_id": session_id,
            "slot_ids": summary["slot_ids"],
            "subject_id": session_rows[0]["subject_id"],
            "subject_name": subject.get("name") or session_rows[0]["subject_id"],
            "subject_short_name": subject.get("short_name") or session_rows[0]["subject_id"],
            "division_id": session_rows[0]["division_id"],
            "day": session_rows[0]["day"],
            "period_id": summary["period_ids"][0],
            "period_ids": summary["period_ids"],
            "room_id": summary["room_ids"][0] if summary["room_ids"] else None,
            "room_ids": summary["room_ids"],
            "slot_type": session_rows[0]["slot_type"],
            "batch_assignments": summary["batch_assignments"],
            "candidate_teachers": candidates,
            "absent_teacher": teacher["name"],
            "absent_teacher_id": teacher_id,
        }
        period_label = _period_span_label(summary["period_ids"])
        title = f"Cover Needed - {subject.get('name') or session_rows[0]['subject_id']}"
        message = (
            f"{teacher['name']} is absent on {session_rows[0]['day']} {period_label}. "
            f"Cover is needed for division {session_rows[0]['division_id']}."
        )
        if summary["batch_assignments"]:
            batch_labels = ", ".join(item["batch_id"] for item in summary["batch_assignments"])
            message += f" Batch coverage: {batch_labels}."
        for faculty_id in sorted(context.teachers.keys()):
            if faculty_id == teacher_id:
                continue
            _push_notification(
                db,
                "slot_free",
                title,
                message,
                data=payload,
                for_role="faculty",
                for_teacher=faculty_id,
            )
        _push_notification(db, "slot_free", title, message, data=payload, for_role="coordinator")
        opened_sessions.append(
            {
                **payload,
                "candidate_count": len(candidates),
            }
        )

    _log_change(
        db,
        "teacher_absent",
        f"{teacher['name']} absent on {day}" + (f" {absent_period}" if absent_period else ""),
        [slot["int_id"] for slot in affected],
        "cover_request",
        True,
        changed_by=changed_by,
    )
    return {
        "success": True,
        "resolved": [],
        "freed_slots": [slot_id for session in opened_sessions for slot_id in session["slot_ids"]],
        "opened_sessions": opened_sessions,
        "message": f"{len(opened_sessions)} session(s) opened for cover and notifications sent",
    }


def claim_slot(notification_id: int, teacher_id: str, changed_by: str = None, assigned_by_role: str = "faculty") -> Tuple[bool, str]:
    db = get_db()
    notif = db.notifications.find_one({"int_id": notification_id}, {"_id": 0})
    if not notif:
        return False, "Notification not found"
    data = json.loads(notif.get("data") or "{}")
    session_id = data.get("session_id")
    if session_id:
        session_rows = _ordered_session_rows(SchedulingContext.from_db(db), _get_session_rows(db, session_id))
    else:
        slot_id = data.get("slot_id")
        if not slot_id:
            return False, "Notification does not point to a timetable slot"
        slot = _get_slot(db, slot_id)
        if not slot:
            return False, "Slot not found"
        session_id = slot["session_id"]
        session_rows = _ordered_session_rows(SchedulingContext.from_db(db), _get_session_rows(db, session_id))
    if not session_rows:
        return False, "Slot not found"
    if any(row.get("status") != "open_cover" for row in session_rows):
        return False, "This session is no longer open for cover"

    context = SchedulingContext.from_db(db)
    state = _rebuild_state_without_sessions(context, [session_id])
    claimed_rows = [
        _row_to_slot({**row, "teacher_id": teacher_id, "status": "active", "source": "claim"})
        for row in session_rows
    ]
    state.add_slots(claimed_rows)
    failure = _validation_message(context, state)
    if failure:
        return False, failure

    for row in session_rows:
        db.timetable_slots.update_one(
            active_timetable_filter(db, {"int_id": row["int_id"]}),
            {"$set": {"teacher_id": teacher_id, "status": "active", "source": "claim"}},
        )
    _mark_session_notifications_read(db, session_id)
    subject = context.subjects.get(session_rows[0]["subject_id"], {"name": session_rows[0]["subject_id"]})
    claimer = context.teachers.get(teacher_id, {"name": teacher_id})
    _push_notification(
        db,
        "slot_claimed",
        f"Cover Assigned - {subject.get('name') or session_rows[0]['subject_id']}",
        f"{claimer.get('name') or teacher_id} will cover division {session_rows[0]['division_id']} on {session_rows[0]['day']} {_period_span_label(list(dict.fromkeys(row['period_id'] for row in session_rows)))}.",
        data={
            "session_id": session_id,
            "slot_ids": [row["int_id"] for row in session_rows],
            "teacher_id": teacher_id,
            "division_id": session_rows[0]["division_id"],
        },
        for_role="coordinator",
    )
    if assigned_by_role == "coordinator":
        _push_notification(
            db,
            "slot_claimed",
            f"Cover Assigned - {subject.get('name') or session_rows[0]['subject_id']}",
            f"You have been assigned to cover division {session_rows[0]['division_id']} on {session_rows[0]['day']} {_period_span_label(list(dict.fromkeys(row['period_id'] for row in session_rows)))}.",
            data={
                "session_id": session_id,
                "slot_ids": [row["int_id"] for row in session_rows],
                "teacher_id": teacher_id,
                "division_id": session_rows[0]["division_id"],
            },
            for_role="faculty",
            for_teacher=teacher_id,
        )
    _log_change(
        db,
        "slot_claimed",
        f"Teacher {teacher_id} claimed session {session_id}",
        [row["int_id"] for row in session_rows],
        "direct",
        True,
        changed_by=changed_by or teacher_id,
    )
    return True, "Session claimed successfully"


def change_teacher_for_slot(slot_id: int, new_teacher_id: str, changed_by: str = None) -> Tuple[bool, str]:
    db = get_db()
    slot = _get_slot(db, slot_id)
    if not slot:
        return False, "Slot not found"
    if not db.teachers.find_one({"id": new_teacher_id}):
        return False, "Teacher not found"

    context = SchedulingContext.from_db(db)
    state = _rebuild_state_without_sessions(context, [slot["session_id"]])
    changed = _row_to_slot({**slot, "teacher_id": new_teacher_id, "status": "active", "source": "manual"})
    state.add_slots([changed])
    failure = _validation_message(context, state)
    if failure:
        return False, failure

    db.timetable_slots.update_one(active_timetable_filter(db, {"int_id": slot_id}), {"$set": {"teacher_id": new_teacher_id, "status": "active", "source": "manual"}})
    _log_change(db, "change_teacher", f"Changed teacher for slot {slot_id} to {new_teacher_id}", [slot_id], "direct", True, changed_by=changed_by)
    return True, "Teacher updated successfully"


def lock_slot(slot_id: int, locked: bool = True) -> Tuple[bool, str]:
    db = get_db()
    result = db.timetable_slots.update_one(active_timetable_filter(db, {"int_id": slot_id}), {"$set": {"is_locked": 1 if locked else 0}})
    if not result.matched_count:
        return False, "Slot not found"
    return True, "Slot " + ("locked" if locked else "unlocked")


def add_constraint(data: dict, changed_by: str = None) -> Tuple[bool, str]:
    db = get_db()
    normalized, errors = normalize_constraint_document(data, strict=True)
    if normalized is None:
        return False, "; ".join(errors)
    iid = next_seq(db, "user_constraints")
    normalized["int_id"] = iid
    db.user_constraints.insert_one(normalized)
    _log_change(db, "constraint_add", f"Added constraint {normalized['constraint_type']}", [iid], "direct", True, changed_by=changed_by)
    return True, "Constraint added"


def update_constraint(cid: int, data: dict, changed_by: str = None) -> Tuple[bool, str]:
    db = get_db()
    current = db.user_constraints.find_one({"int_id": cid}, {"_id": 0})
    if not current:
        return False, "Constraint not found"
    merged = deepcopy(current)
    merged.update(data or {})
    normalized, errors = normalize_constraint_document(merged, strict=True)
    if normalized is None:
        return False, "; ".join(errors)
    normalized["int_id"] = cid
    db.user_constraints.update_one({"int_id": cid}, {"$set": normalized})
    _log_change(db, "constraint_update", f"Updated constraint {cid}", [cid], "direct", True, changed_by=changed_by)
    return True, "Constraint updated"


def get_timetable(division_id: str = None) -> Dict:
    db = get_db()
    flt = active_timetable_filter(db, {"division_id": division_id} if division_id else {})
    slots = list(db.timetable_slots.find(flt, {"_id": 0}).sort([("division_id", 1), ("day", 1), ("period_id", 1)]))
    result = {}
    for slot in slots:
        div = slot["division_id"]
        day = slot["day"]
        pid = slot["period_id"]
        result.setdefault(div, {}).setdefault(day, {}).setdefault(pid, [])
        result[div][day][pid].append(
            {
                "id": slot["int_id"],
                "subject_id": slot["subject_id"],
                "teacher_id": slot.get("teacher_id"),
                "room_id": slot.get("room_id"),
                "slot_type": slot["slot_type"],
                "batch_id": slot.get("batch_id"),
                "parallel_group_id": slot.get("parallel_group_id"),
                "status": slot.get("status", "active"),
                "session_id": slot.get("session_id"),
                "is_locked": bool(slot.get("is_locked")),
            }
        )
    return result


def get_notifications(teacher_id: str = None, role: str = None, unread_only: bool = False) -> List[Dict]:
    db = get_db()
    flt = {}
    if role:
        flt["$or"] = [{"for_role": role}, {"for_role": None}]
    if teacher_id:
        teacher_cond = [{"for_teacher": teacher_id}, {"for_teacher": None}]
        flt = {"$and": [flt, {"$or": teacher_cond}]} if flt else {"$or": teacher_cond}
    if unread_only:
        flt["is_read"] = 0
    return list(db.notifications.find(flt, {"_id": 0}).sort("created_at", -1).limit(50))


def get_change_log() -> List[Dict]:
    db = get_db()
    return list(db.change_log.find({}, {"_id": 0}).sort("created_at", -1))
