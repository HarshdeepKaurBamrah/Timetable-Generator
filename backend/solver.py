"""
solver.py - Shared scheduling engine, constraint evaluator, and timetable solver.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from constraints import active_constraints, constraint_matches
from database import active_timetable_filter, get_config_value, get_db, next_seq, replace_active_timetable


DEFAULT_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@dataclass
class ConstraintIssue:
    code: str
    message: str
    entities: Dict[str, Any] = field(default_factory=dict)
    blocking: bool = True


@dataclass
class GenerationDiagnostic:
    phase: str
    code: str
    message: str
    entities: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SlotRecord:
    division_id: str
    day: str
    period_id: str
    subject_id: str
    teacher_id: Optional[str]
    room_id: Optional[str]
    slot_type: str
    session_id: str
    occupancy_key: str
    batch_id: Optional[str] = None
    parallel_group_id: Optional[str] = None
    int_id: Optional[int] = None
    is_locked: bool = False
    status: str = "active"
    source: str = "solver"

    def to_doc(self) -> Dict[str, Any]:
        return {
            "int_id": self.int_id,
            "division_id": self.division_id,
            "day": self.day,
            "period_id": self.period_id,
            "subject_id": self.subject_id,
            "teacher_id": self.teacher_id,
            "room_id": self.room_id,
            "slot_type": self.slot_type,
            "batch_id": self.batch_id,
            "parallel_group_id": self.parallel_group_id,
            "session_id": self.session_id,
            "occupancy_key": self.occupancy_key,
            "is_locked": 1 if self.is_locked else 0,
            "status": self.status,
            "source": self.source,
        }


@dataclass
class PlacementCandidate:
    slots: List[SlotRecord]
    penalty: int
    sort_key: Tuple[Any, ...]


@dataclass
class StandardRequest:
    request_id: str
    division_id: str
    subject_id: str
    teacher_id: str
    slot_type: str
    occurrence_index: int
    preferred_room_id: Optional[str] = None

    @property
    def duration(self) -> int:
        return 1

    @property
    def display_name(self) -> str:
        return f"{self.division_id}/{self.subject_id}/{self.slot_type}#{self.occurrence_index}"


@dataclass
class ParallelMember:
    subject_id: str
    teacher_id: str
    slot_type: str
    session_id: str


@dataclass
class ParallelRequest:
    request_id: str
    division_id: str
    group_id: str
    group_name: str
    occurrence_index: int
    members: List[ParallelMember]

    @property
    def duration(self) -> int:
        return 1

    @property
    def display_name(self) -> str:
        return f"{self.division_id}/{self.group_name}#{self.occurrence_index}"


@dataclass
class LabBatchItem:
    batch_id: str
    subject_id: str
    teacher_id: str
    session_id: str
    preferred_room_id: Optional[str] = None


@dataclass
class LabBlockRequest:
    request_id: str
    division_id: str
    occurrence_index: int
    batch_ids: List[str]

    @property
    def duration(self) -> int:
        return 2

    @property
    def display_name(self) -> str:
        return f"{self.division_id}/lab_block#{self.occurrence_index}"


@dataclass
class SchedulingContext:
    db: Any
    period_ids: List[str]
    days: List[str]
    break_after_periods: List[str]
    teachers: Dict[str, Dict[str, Any]]
    rooms: Dict[str, Dict[str, Any]]
    subjects: Dict[str, Dict[str, Any]]
    divisions: Dict[str, Dict[str, Any]]
    batches: Dict[str, List[Dict[str, Any]]]
    division_subjects: Dict[str, List[str]]
    batch_teachers: Dict[Tuple[str, str], str]
    constraints: List[Dict[str, Any]]

    @property
    def lecture_rooms(self) -> List[Dict[str, Any]]:
        return [room for room in self.rooms.values() if room.get("room_type") == "lecture"]

    @property
    def lab_rooms(self) -> List[Dict[str, Any]]:
        return [room for room in self.rooms.values() if room.get("room_type") == "lab"]

    @classmethod
    def from_db(cls, db=None, period_ids=None, days=None):
        if db is None:
            db = get_db()
        period_ids = list(period_ids or get_config_value(db, "period_ids", ["P1", "P2", "P3", "P4", "P5", "P6"]))
        days = list(days or get_config_value(db, "days", DEFAULT_DAYS))
        teachers = {row["id"]: row for row in db.teachers.find({}, {"_id": 0})}
        for teacher in teachers.values():
            unavailable = teacher.get("unavailable") or {}
            if isinstance(unavailable, str):
                try:
                    unavailable = json.loads(unavailable)
                except Exception:
                    unavailable = {}
            teacher["unavailable"] = unavailable
            teacher["max_hrs_per_day"] = max(1, int(teacher.get("max_hrs_per_day", 4) or 4))
            teacher["max_hrs_per_week"] = max(1, int(teacher.get("max_hrs_per_week", 18) or 18))

        rooms = {row["id"]: row for row in db.rooms.find({}, {"_id": 0})}
        subjects = {row["id"]: row for row in db.subjects.find({}, {"_id": 0})}
        divisions = {row["id"]: row for row in db.divisions.find({}, {"_id": 0})}

        batches: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        batch_to_division: Dict[str, str] = {}
        for batch in db.batches.find({}, {"_id": 0}).sort([("division_id", 1), ("id", 1)]):
            batches[batch["division_id"]].append(batch)
            batch_to_division[batch["id"]] = batch["division_id"]

        division_subjects: Dict[str, List[str]] = defaultdict(list)
        for row in db.division_subjects.find({}, {"_id": 0}):
            division_subjects[row["division_id"]].append(row["subject_id"])

        batch_teachers = {}
        for row in db.batch_teachers.find({}, {"_id": 0}):
            batch_teachers[(row["batch_id"], row["subject_id"])] = row["teacher_id"]
            inferred_division_id = batch_to_division.get(row.get("batch_id"))
            subject_id = row.get("subject_id")
            subject = subjects.get(subject_id or "")
            if not inferred_division_id or not subject:
                continue
            if subject.get("has_lab") and int(subject.get("lab_hours_per_week", 0) or 0) > 0:
                division_subjects[inferred_division_id].append(subject_id)

        for division_id in list(division_subjects.keys()):
            division_subjects[division_id] = sorted(set(division_subjects[division_id]))

        constraints = active_constraints(db.user_constraints.find({}, {"_id": 0}))

        return cls(
            db=db,
            period_ids=period_ids,
            days=days,
            break_after_periods=list(get_config_value(db, "break_after_periods", ["P2", "P4"])),
            teachers=teachers,
            rooms=rooms,
            subjects=subjects,
            divisions=divisions,
            batches=dict(batches),
            division_subjects=dict(division_subjects),
            batch_teachers=batch_teachers,
            constraints=constraints,
        )


class ScheduleState:
    def __init__(self, period_ids: Sequence[str], days: Sequence[str]):
        self.period_ids = list(period_ids)
        self.period_index = {pid: idx for idx, pid in enumerate(self.period_ids)}
        self.days = list(days)
        self.slots: List[SlotRecord] = []
        self.session_slots: Dict[str, List[SlotRecord]] = defaultdict(list)
        self.division_slots: Dict[Tuple[str, str, str], List[SlotRecord]] = defaultdict(list)
        self.teacher_slots: Dict[Tuple[str, str, str], List[SlotRecord]] = defaultdict(list)
        self.room_slots: Dict[Tuple[str, str, str], List[SlotRecord]] = defaultdict(list)
        self.batch_slots: Dict[Tuple[str, str, str], List[SlotRecord]] = defaultdict(list)
        self.teacher_day_totals: Dict[Tuple[str, str], int] = defaultdict(int)
        self.teacher_week_totals: Dict[str, int] = defaultdict(int)
        self.division_subject_day_sessions: Dict[Tuple[str, str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.division_subject_type_day_sessions: Dict[Tuple[str, str, str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.division_day_period_counts: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.lab_session_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self.lab_subject_day_start_counts: Dict[Tuple[str, str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.division_day_lab_start_counts: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def _register_lab_session(self, rows: List[SlotRecord]):
        if not rows or rows[0].slot_type != "lab":
            return
        ordered = sorted(rows, key=lambda row: self.period_index[row.period_id])
        first = ordered[0]
        start_period = first.period_id
        self.lab_subject_day_start_counts[(first.division_id, first.subject_id, first.day)][start_period] += 1
        self.division_day_lab_start_counts[(first.division_id, first.day)][start_period] += 1
        if first.batch_id:
            self.lab_session_counts[(first.division_id, first.batch_id, first.subject_id)] += 1

    def _unregister_lab_session(self, rows: List[SlotRecord]):
        if not rows or rows[0].slot_type != "lab":
            return
        ordered = sorted(rows, key=lambda row: self.period_index[row.period_id])
        first = ordered[0]
        start_period = first.period_id
        subject_key = (first.division_id, first.subject_id, first.day)
        day_key = (first.division_id, first.day)
        if first.batch_id:
            count_key = (first.division_id, first.batch_id, first.subject_id)
            self.lab_session_counts[count_key] -= 1
            if self.lab_session_counts[count_key] <= 0:
                del self.lab_session_counts[count_key]
        subject_starts = self.lab_subject_day_start_counts.get(subject_key)
        if subject_starts:
            subject_starts[start_period] -= 1
            if subject_starts[start_period] <= 0:
                del subject_starts[start_period]
            if not subject_starts:
                del self.lab_subject_day_start_counts[subject_key]
        day_starts = self.division_day_lab_start_counts.get(day_key)
        if day_starts:
            day_starts[start_period] -= 1
            if day_starts[start_period] <= 0:
                del day_starts[start_period]
            if not day_starts:
                del self.division_day_lab_start_counts[day_key]

    def add_slots(self, slots: Iterable[SlotRecord]):
        slots_list = list(slots)
        new_lab_sessions: Dict[str, bool] = {}
        for slot in slots_list:
            if slot.slot_type == "lab" and slot.session_id not in new_lab_sessions:
                new_lab_sessions[slot.session_id] = slot.session_id not in self.session_slots
            self.slots.append(slot)
            self.session_slots[slot.session_id].append(slot)
            self.division_slots[(slot.division_id, slot.day, slot.period_id)].append(slot)
            self.division_subject_day_sessions[(slot.division_id, slot.subject_id, slot.day)][slot.session_id] += 1
            self.division_subject_type_day_sessions[(slot.division_id, slot.subject_id, slot.slot_type, slot.day)][slot.session_id] += 1
            self.division_day_period_counts[(slot.division_id, slot.day)][slot.period_id] += 1
            if slot.teacher_id:
                self.teacher_slots[(slot.teacher_id, slot.day, slot.period_id)].append(slot)
                self.teacher_day_totals[(slot.teacher_id, slot.day)] += 1
                self.teacher_week_totals[slot.teacher_id] += 1
            if slot.room_id:
                self.room_slots[(slot.room_id, slot.day, slot.period_id)].append(slot)
            if slot.batch_id:
                self.batch_slots[(slot.batch_id, slot.day, slot.period_id)].append(slot)
        for session_id, is_new in new_lab_sessions.items():
            if is_new and self.session_slots.get(session_id):
                self._register_lab_session(self.session_slots[session_id])

    def remove_slots(self, slots: Iterable[SlotRecord]):
        slots_list = list(slots)
        removed_lab_sessions: Dict[str, List[SlotRecord]] = {}
        for slot in slots_list:
            if slot.slot_type == "lab" and slot.session_id not in removed_lab_sessions and slot.session_id in self.session_slots:
                removed_lab_sessions[slot.session_id] = list(self.session_slots[slot.session_id])
            if slot in self.slots:
                self.slots.remove(slot)
            if slot in self.session_slots.get(slot.session_id, []):
                self.session_slots[slot.session_id].remove(slot)
                if not self.session_slots[slot.session_id]:
                    del self.session_slots[slot.session_id]
            key = (slot.division_id, slot.day, slot.period_id)
            if slot in self.division_slots.get(key, []):
                self.division_slots[key].remove(slot)
                if not self.division_slots[key]:
                    del self.division_slots[key]
            subject_key = (slot.division_id, slot.subject_id, slot.day)
            session_counts = self.division_subject_day_sessions.get(subject_key)
            if session_counts and slot.session_id in session_counts:
                session_counts[slot.session_id] -= 1
                if session_counts[slot.session_id] <= 0:
                    del session_counts[slot.session_id]
                if not session_counts:
                    del self.division_subject_day_sessions[subject_key]
            subject_type_key = (slot.division_id, slot.subject_id, slot.slot_type, slot.day)
            subject_type_counts = self.division_subject_type_day_sessions.get(subject_type_key)
            if subject_type_counts and slot.session_id in subject_type_counts:
                subject_type_counts[slot.session_id] -= 1
                if subject_type_counts[slot.session_id] <= 0:
                    del subject_type_counts[slot.session_id]
                if not subject_type_counts:
                    del self.division_subject_type_day_sessions[subject_type_key]
            period_counts = self.division_day_period_counts.get((slot.division_id, slot.day))
            if period_counts and slot.period_id in period_counts:
                period_counts[slot.period_id] -= 1
                if period_counts[slot.period_id] <= 0:
                    del period_counts[slot.period_id]
                if not period_counts:
                    del self.division_day_period_counts[(slot.division_id, slot.day)]
            if slot.teacher_id:
                key = (slot.teacher_id, slot.day, slot.period_id)
                if slot in self.teacher_slots.get(key, []):
                    self.teacher_slots[key].remove(slot)
                    if not self.teacher_slots[key]:
                        del self.teacher_slots[key]
                day_key = (slot.teacher_id, slot.day)
                self.teacher_day_totals[day_key] -= 1
                if self.teacher_day_totals[day_key] <= 0:
                    del self.teacher_day_totals[day_key]
                self.teacher_week_totals[slot.teacher_id] -= 1
                if self.teacher_week_totals[slot.teacher_id] <= 0:
                    del self.teacher_week_totals[slot.teacher_id]
            if slot.room_id:
                key = (slot.room_id, slot.day, slot.period_id)
                if slot in self.room_slots.get(key, []):
                    self.room_slots[key].remove(slot)
                    if not self.room_slots[key]:
                        del self.room_slots[key]
            if slot.batch_id:
                key = (slot.batch_id, slot.day, slot.period_id)
                if slot in self.batch_slots.get(key, []):
                    self.batch_slots[key].remove(slot)
                    if not self.batch_slots[key]:
                        del self.batch_slots[key]
        for session_id, rows in removed_lab_sessions.items():
            if session_id not in self.session_slots:
                self._unregister_lab_session(rows)

    def division_entries(self, division_id: str, day: str, period_id: str, exclude_sessions: Optional[Set[str]] = None) -> List[SlotRecord]:
        rows = self.division_slots.get((division_id, day, period_id), [])
        if not exclude_sessions:
            return list(rows)
        return [row for row in rows if row.session_id not in exclude_sessions]

    def teacher_entries(self, teacher_id: str, day: str, period_id: str, exclude_sessions: Optional[Set[str]] = None) -> List[SlotRecord]:
        rows = self.teacher_slots.get((teacher_id, day, period_id), [])
        if not exclude_sessions:
            return list(rows)
        return [row for row in rows if row.session_id not in exclude_sessions]

    def room_entries(self, room_id: str, day: str, period_id: str, exclude_sessions: Optional[Set[str]] = None) -> List[SlotRecord]:
        rows = self.room_slots.get((room_id, day, period_id), [])
        if not exclude_sessions:
            return list(rows)
        return [row for row in rows if row.session_id not in exclude_sessions]

    def batch_entries(self, batch_id: str, day: str, period_id: str, exclude_sessions: Optional[Set[str]] = None) -> List[SlotRecord]:
        rows = self.batch_slots.get((batch_id, day, period_id), [])
        if not exclude_sessions:
            return list(rows)
        return [row for row in rows if row.session_id not in exclude_sessions]

    def teacher_day_count(self, teacher_id: str, day: str, exclude_sessions: Optional[Set[str]] = None) -> int:
        total = self.teacher_day_totals.get((teacher_id, day), 0)
        if not exclude_sessions:
            return total
        excluded = 0
        for session_id in exclude_sessions:
            for row in self.session_slots.get(session_id, []):
                if row.teacher_id == teacher_id and row.day == day:
                    excluded += 1
        return max(0, total - excluded)

    def teacher_week_count(self, teacher_id: str, exclude_sessions: Optional[Set[str]] = None) -> int:
        total = self.teacher_week_totals.get(teacher_id, 0)
        if not exclude_sessions:
            return total
        excluded = 0
        for session_id in exclude_sessions:
            for row in self.session_slots.get(session_id, []):
                if row.teacher_id == teacher_id:
                    excluded += 1
        return max(0, total - excluded)

    def teacher_day_unique_period_count(
        self,
        teacher_id: str,
        day: str,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> int:
        periods: Set[str] = set()
        for period_id in self.period_ids:
            rows = self.teacher_entries(teacher_id, day, period_id, exclude_sessions)
            if rows:
                periods.add(period_id)
        return len(periods)

    def teacher_week_unique_period_count(
        self,
        teacher_id: str,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> int:
        occupied: Set[Tuple[str, str]] = set()
        for day in self.days:
            for period_id in self.period_ids:
                rows = self.teacher_entries(teacher_id, day, period_id, exclude_sessions)
                if rows:
                    occupied.add((day, period_id))
        return len(occupied)

    def subject_session_count(self, division_id: str, subject_id: str, day: str, exclude_sessions: Optional[Set[str]] = None) -> int:
        sessions = self.division_subject_day_sessions.get((division_id, subject_id, day), {})
        if not exclude_sessions:
            return len(sessions)
        return len([session_id for session_id in sessions if session_id not in exclude_sessions])

    def subject_type_session_count(
        self,
        division_id: str,
        subject_id: str,
        slot_type: str,
        day: str,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> int:
        sessions = self.division_subject_type_day_sessions.get((division_id, subject_id, slot_type, day), {})
        if not exclude_sessions:
            return len(sessions)
        return len([session_id for session_id in sessions if session_id not in exclude_sessions])

    def occupied_indices(self, division_id: str, day: str, exclude_sessions: Optional[Set[str]] = None) -> Set[int]:
        if not exclude_sessions:
            return {self.period_index[period_id] for period_id in self.division_day_period_counts.get((division_id, day), {})}
        used = set()
        for period_id in self.period_ids:
            if self.division_entries(division_id, day, period_id, exclude_sessions):
                used.add(self.period_index[period_id])
        return used

    def lab_subject_day_start_periods(
        self,
        division_id: str,
        subject_id: str,
        day: str,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> Set[str]:
        if not exclude_sessions:
            return set(self.lab_subject_day_start_counts.get((division_id, subject_id, day), {}).keys())
        start_periods: Set[str] = set()
        for session_rows in self.session_slots.values():
            if not session_rows or session_rows[0].slot_type != "lab":
                continue
            first = session_rows[0]
            if first.division_id != division_id or first.subject_id != subject_id or first.day != day:
                continue
            if any(row.session_id in exclude_sessions for row in session_rows):
                continue
            ordered = sorted(session_rows, key=lambda row: self.period_index[row.period_id])
            start_periods.add(ordered[0].period_id)
        return start_periods

    def division_day_lab_block_count(
        self,
        division_id: str,
        day: str,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> int:
        if not exclude_sessions:
            return len(self.division_day_lab_start_counts.get((division_id, day), {}))
        start_periods: Set[str] = set()
        for session_rows in self.session_slots.values():
            if not session_rows or session_rows[0].slot_type != "lab":
                continue
            first = session_rows[0]
            if first.division_id != division_id or first.day != day:
                continue
            if any(row.session_id in exclude_sessions for row in session_rows):
                continue
            ordered = sorted(session_rows, key=lambda row: self.period_index[row.period_id])
            start_periods.add(ordered[0].period_id)
        return len(start_periods)


class ConstraintEvaluator:
    def __init__(self, context: SchedulingContext):
        self.context = context
        self.period_ids = context.period_ids
        self.period_index = {pid: idx for idx, pid in enumerate(self.period_ids)}
        self.days = context.days
        self.break_after = set(context.break_after_periods or [])
        self.global_constraints: List[Dict[str, Any]] = []
        self.constraints_by_division: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.constraints_by_subject: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.constraints_by_teacher: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.constraints_by_room: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.constraints_by_batch_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._matching_constraints_cache: Dict[Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]], List[Dict[str, Any]]] = {}
        self._index_constraints()
        self.has_room_scoped_constraints = bool(self.constraints_by_room)

    def _index_constraints(self):
        for constraint in self.context.constraints:
            scope = constraint.get("scope") or {}
            indexed = False
            for division_id in scope.get("division_ids") or []:
                self.constraints_by_division[division_id].append(constraint)
                indexed = True
            for subject_id in scope.get("subject_ids") or []:
                self.constraints_by_subject[subject_id].append(constraint)
                indexed = True
            for teacher_id in scope.get("teacher_ids") or []:
                self.constraints_by_teacher[teacher_id].append(constraint)
                indexed = True
            for room_id in scope.get("room_ids") or []:
                self.constraints_by_room[room_id].append(constraint)
                indexed = True
            for batch_group_id in scope.get("batch_group_ids") or []:
                self.constraints_by_batch_group[batch_group_id].append(constraint)
                indexed = True
            if not indexed:
                self.global_constraints.append(constraint)

    def _issue(self, code: str, message: str, **entities) -> ConstraintIssue:
        return ConstraintIssue(code=code, message=message, entities=entities)

    def _matching_constraints(
        self,
        *,
        division_id: str,
        subject_id: Optional[str] = None,
        teacher_id: Optional[str] = None,
        room_id: Optional[str] = None,
        batch_group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cache_key = (division_id, subject_id, teacher_id, room_id, batch_group_id)
        cached = self._matching_constraints_cache.get(cache_key)
        if cached is not None:
            return cached
        matched = []
        candidates = list(self.global_constraints)
        if division_id:
            candidates.extend(self.constraints_by_division.get(division_id, []))
        if subject_id:
            candidates.extend(self.constraints_by_subject.get(subject_id, []))
        if teacher_id:
            candidates.extend(self.constraints_by_teacher.get(teacher_id, []))
        if room_id:
            candidates.extend(self.constraints_by_room.get(room_id, []))
        if batch_group_id:
            candidates.extend(self.constraints_by_batch_group.get(batch_group_id, []))
        seen = set()
        for constraint in candidates:
            marker = id(constraint)
            if marker in seen:
                continue
            seen.add(marker)
            if not constraint.get("active"):
                continue
            if constraint_matches(
                constraint,
                division_id=division_id,
                subject_id=subject_id,
                teacher_id=teacher_id,
                room_id=room_id,
                batch_group_id=batch_group_id,
            ):
                matched.append(constraint)
        self._matching_constraints_cache[cache_key] = matched
        return matched

    def session_crosses_break(self, start_idx: int, duration: int) -> bool:
        for offset in range(duration - 1):
            if self.period_ids[start_idx + offset] in self.break_after:
                return True
        return False

    def end_of_day_lab_start_idx(self) -> Optional[int]:
        max_start = len(self.period_ids) - 2
        for start_idx in range(max_start, -1, -1):
            if not self.session_crosses_break(start_idx, 2):
                return start_idx
        return None

    def end_of_day_lab_periods(self) -> List[str]:
        start_idx = self.end_of_day_lab_start_idx()
        if start_idx is None:
            return []
        return self.period_ids[start_idx : start_idx + 2]

    def required_end_of_day_lab_days(self) -> int:
        total_lab_blocks = 0
        for division_id, subject_ids in self.context.division_subjects.items():
            for subject_id in subject_ids:
                subject = self.context.subjects.get(subject_id)
                if not subject or not subject.get("has_lab"):
                    continue
                total_lab_blocks += max(0, int(subject.get("lab_hours_per_week", 0) or 0) // 2)
        return min(len(self.days), total_lab_blocks)

    def scheduled_end_of_day_lab_days(
        self,
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> Set[str]:
        periods = self.end_of_day_lab_periods()
        if not periods:
            return set()
        start_period = periods[0]
        covered_days: Set[str] = set()
        for division_id in self.context.divisions:
            for day in self.days:
                if not exclude_sessions and start_period in state.division_day_lab_start_counts.get((division_id, day), {}):
                    covered_days.add(day)
                    continue
                if any(row.slot_type == "lab" for row in state.division_entries(division_id, day, start_period, exclude_sessions)):
                    covered_days.add(day)
        return covered_days

    def validate_end_of_day_lab_coverage(self, state: ScheduleState) -> Optional[GenerationDiagnostic]:
        required_end_lab_days = self.required_end_of_day_lab_days()
        covered_end_lab_days = sorted(self.scheduled_end_of_day_lab_days(state))
        if required_end_lab_days and len(covered_end_lab_days) < required_end_lab_days:
            return GenerationDiagnostic(
                "validation",
                "end_of_day_lab_coverage",
                f"End-of-day lab coverage is short: {len(covered_end_lab_days)}/{required_end_lab_days} required day slots are covered",
                {
                    "required_days": required_end_lab_days,
                    "covered_days": covered_end_lab_days,
                    "missing_count": required_end_lab_days - len(covered_end_lab_days),
                    "target_periods": self.end_of_day_lab_periods(),
                },
            )
        return None

    def _base_teacher_checks(
        self,
        teacher_id: Optional[str],
        day: str,
        periods: List[str],
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
        allow_parallel_overlap: bool = False,
    ) -> List[ConstraintIssue]:
        issues = []
        if not teacher_id:
            return [self._issue("teacher_missing", "No teacher assigned")]
        teacher = self.context.teachers.get(teacher_id)
        if not teacher:
            return [self._issue("teacher_missing", f"Teacher {teacher_id} does not exist", teacher_id=teacher_id)]
        unavailable = teacher.get("unavailable") or {}
        if any(period in unavailable.get(day, []) for period in periods):
            issues.append(self._issue("teacher_unavailable", f"Teacher {teacher_id} is unavailable on {day}", teacher_id=teacher_id, day=day))
        if not allow_parallel_overlap:
            for period in periods:
                if state.teacher_entries(teacher_id, day, period, exclude_sessions):
                    issues.append(self._issue("teacher_overlap", f"Teacher {teacher_id} is already booked on {day} {period}", teacher_id=teacher_id, day=day, period_id=period))
        if allow_parallel_overlap:
            existing_day_load = state.teacher_day_unique_period_count(teacher_id, day, exclude_sessions)
            projected_day_load = len(set(periods) | {
                period_id
                for period_id in self.period_ids
                if state.teacher_entries(teacher_id, day, period_id, exclude_sessions)
            })
        else:
            existing_day_load = state.teacher_day_count(teacher_id, day, exclude_sessions)
            projected_day_load = existing_day_load + len(periods)
        if projected_day_load > teacher["max_hrs_per_day"]:
            issues.append(self._issue("teacher_daily_limit", f"Teacher {teacher_id} exceeds daily hour limit on {day}", teacher_id=teacher_id, day=day))
        if allow_parallel_overlap:
            occupied_week_slots: Set[Tuple[str, str]] = set()
            for iter_day in self.days:
                for period_id in self.period_ids:
                    if state.teacher_entries(teacher_id, iter_day, period_id, exclude_sessions):
                        occupied_week_slots.add((iter_day, period_id))
            projected_week_load = len(occupied_week_slots | {(day, period_id) for period_id in periods})
        else:
            projected_week_load = state.teacher_week_count(teacher_id, exclude_sessions) + len(periods)
        if projected_week_load > teacher["max_hrs_per_week"]:
            issues.append(self._issue("teacher_weekly_limit", f"Teacher {teacher_id} exceeds weekly hour limit", teacher_id=teacher_id))
        return issues

    def _base_room_checks(
        self,
        room_id: Optional[str],
        expected_type: str,
        day: str,
        periods: List[str],
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> List[ConstraintIssue]:
        issues = []
        if not room_id:
            return [self._issue("room_missing", "No room assigned")]
        room = self.context.rooms.get(room_id)
        if not room:
            return [self._issue("room_missing", f"Room {room_id} does not exist", room_id=room_id)]
        if room.get("room_type") != expected_type:
            issues.append(self._issue("room_type_mismatch", f"Room {room_id} is not a {expected_type} room", room_id=room_id))
        for period in periods:
            if state.room_entries(room_id, day, period, exclude_sessions):
                issues.append(self._issue("room_overlap", f"Room {room_id} is already booked on {day} {period}", room_id=room_id, day=day, period_id=period))
        return issues

    def _same_day_standard_repeat_issue(
        self,
        division_id: str,
        subject_id: str,
        slot_type: str,
        day: str,
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> Optional[ConstraintIssue]:
        if slot_type not in {"lecture", "tutorial"}:
            return None
        if state.subject_type_session_count(division_id, subject_id, slot_type, day, exclude_sessions) >= 1:
            label = "lecture" if slot_type == "lecture" else slot_type
            return self._issue(
                "same_day_repeat",
                f"{subject_id} {label} cannot repeat on {day}",
                division_id=division_id,
                subject_id=subject_id,
                slot_type=slot_type,
                day=day,
            )
        return None

    def _default_penalty(
        self,
        division_id: str,
        subject_id: str,
        day: str,
        start_idx: int,
        duration: int,
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> int:
        penalty = state.subject_session_count(division_id, subject_id, day, exclude_sessions) * 12
        for offset in range(duration):
            idx = start_idx + offset
            for neighbor in (idx - 1, idx + 1):
                if 0 <= neighbor < len(self.period_ids):
                    for row in state.division_entries(division_id, day, self.period_ids[neighbor], exclude_sessions):
                        if row.subject_id == subject_id:
                            penalty += 8
                            break
        return penalty

    def _apply_dynamic_constraints(
        self,
        *,
        division_id: str,
        subject_id: str,
        teacher_id: Optional[str],
        room_id: Optional[str],
        day: str,
        start_idx: int,
        duration: int,
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
        batch_group_id: Optional[str] = None,
    ) -> Tuple[List[ConstraintIssue], int]:
        issues: List[ConstraintIssue] = []
        penalty = 0
        periods = self.period_ids[start_idx : start_idx + duration]
        matching = self._matching_constraints(
            division_id=division_id,
            subject_id=subject_id,
            teacher_id=teacher_id,
            room_id=room_id,
            batch_group_id=batch_group_id,
        )
        for constraint in matching:
            ctype = constraint["constraint_type"]
            params = constraint.get("params") or {}
            priority = constraint.get("priority", "hard")
            matched_issue: Optional[ConstraintIssue] = None

            if ctype == "avoid_day" and day in set(params.get("days") or []):
                matched_issue = self._issue("avoid_day", f"Constraint blocks {subject_id} on {day}", constraint_type=ctype, day=day)
            elif ctype == "avoid_period" and set(periods) & set(params.get("period_ids") or []):
                matched_issue = self._issue("avoid_period", f"Constraint blocks {subject_id} at {'/'.join(periods)}", constraint_type=ctype, period_ids=periods)
            elif ctype == "max_per_day":
                limit = int(params.get("max_per_day") or 0)
                if limit and state.subject_session_count(division_id, subject_id, day, exclude_sessions) + 1 > limit:
                    matched_issue = self._issue("max_per_day", f"{subject_id} exceeds max-per-day on {day}", constraint_type=ctype, day=day)
            elif ctype == "once_per_day":
                if state.subject_session_count(division_id, subject_id, day, exclude_sessions) >= 1:
                    matched_issue = self._issue("once_per_day", f"{subject_id} may only appear once on {day}", constraint_type=ctype, day=day)
            elif ctype == "start_or_end_only":
                if start_idx not in {0, len(self.period_ids) - duration}:
                    matched_issue = self._issue("start_or_end_only", f"{subject_id} must be at the start or end of the day", constraint_type=ctype)
            elif ctype == "end_only":
                if start_idx != len(self.period_ids) - duration:
                    matched_issue = self._issue("end_only", f"{subject_id} must be scheduled at the end of the day", constraint_type=ctype)
            elif ctype == "prefer_day":
                if day not in set(params.get("days") or []):
                    if priority == "hard":
                        matched_issue = self._issue("prefer_day", f"{subject_id} must be on one of the preferred days", constraint_type=ctype)
                    else:
                        penalty += int(constraint.get("weight", 10) or 10)
            elif ctype == "prefer_period":
                allowed = set(params.get("period_ids") or [])
                if allowed and not set(periods).issubset(allowed):
                    if priority == "hard":
                        matched_issue = self._issue("prefer_period", f"{subject_id} must be on one of the preferred periods", constraint_type=ctype)
                    else:
                        penalty += int(constraint.get("weight", 10) or 10)
            elif ctype == "prefer_end_of_day":
                penalty += (len(self.period_ids) - duration - start_idx) * int(constraint.get("weight", 6) or 6)
            elif ctype == "no_free_slots" and priority == "soft":
                filled = sorted(state.occupied_indices(division_id, day, exclude_sessions) | set(range(start_idx, start_idx + duration)))
                gaps = 0
                for idx in range(1, len(filled)):
                    gaps += max(0, filled[idx] - filled[idx - 1] - 1)
                penalty += gaps * int(constraint.get("weight", 10) or 10)

            if matched_issue:
                if priority == "hard":
                    issues.append(matched_issue)
                else:
                    penalty += int(constraint.get("weight", 10) or 10)
        return issues, penalty

    def evaluate_standard_base(
        self,
        request: StandardRequest,
        day: str,
        period_id: str,
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> Tuple[bool, int, List[ConstraintIssue]]:
        if state.division_entries(request.division_id, day, period_id, exclude_sessions):
            return False, 0, [self._issue("division_occupied", f"Division {request.division_id} is already occupied on {day} {period_id}", division_id=request.division_id, day=day, period_id=period_id)]
        issues = self._base_teacher_checks(request.teacher_id, day, [period_id], state, exclude_sessions)
        dyn_issues, dyn_penalty = self._apply_dynamic_constraints(
            division_id=request.division_id,
            subject_id=request.subject_id,
            teacher_id=request.teacher_id,
            room_id=None,
            day=day,
            start_idx=self.period_index[period_id],
            duration=1,
            state=state,
            exclude_sessions=exclude_sessions,
        )
        issues.extend(dyn_issues)
        repeat_issue = self._same_day_standard_repeat_issue(
            request.division_id,
            request.subject_id,
            request.slot_type,
            day,
            state,
            exclude_sessions,
        )
        if repeat_issue:
            issues.append(repeat_issue)
        if issues:
            return False, 0, issues
        penalty = dyn_penalty + self._default_penalty(request.division_id, request.subject_id, day, self.period_index[period_id], 1, state, exclude_sessions)
        return True, penalty, []

    def evaluate_parallel_member_base(
        self,
        division_id: str,
        member: ParallelMember,
        day: str,
        period_id: str,
        state: ScheduleState,
    ) -> Tuple[bool, int, List[ConstraintIssue]]:
        issues = self._base_teacher_checks(member.teacher_id, day, [period_id], state)
        dyn_issues, dyn_penalty = self._apply_dynamic_constraints(
            division_id=division_id,
            subject_id=member.subject_id,
            teacher_id=member.teacher_id,
            room_id=None,
            day=day,
            start_idx=self.period_index[period_id],
            duration=1,
            state=state,
        )
        issues.extend(dyn_issues)
        repeat_issue = self._same_day_standard_repeat_issue(
            division_id,
            member.subject_id,
            member.slot_type,
            day,
            state,
        )
        if repeat_issue:
            issues.append(repeat_issue)
        if issues:
            return False, 0, issues
        penalty = dyn_penalty + self._default_penalty(division_id, member.subject_id, day, self.period_index[period_id], 1, state)
        return True, penalty, []

    def evaluate_lab_item_base(
        self,
        division_id: str,
        item: LabBatchItem,
        day: str,
        start_idx: int,
        state: ScheduleState,
    ) -> Tuple[bool, int, List[ConstraintIssue]]:
        periods = self.period_ids[start_idx : start_idx + 2]
        issues = self._base_teacher_checks(item.teacher_id, day, periods, state, allow_parallel_overlap=True)
        for period in periods:
            if state.batch_entries(item.batch_id, day, period):
                issues.append(self._issue("batch_overlap", f"Batch {item.batch_id} is already occupied on {day} {period}", batch_id=item.batch_id, day=day, period_id=period))
        dyn_issues, dyn_penalty = self._apply_dynamic_constraints(
            division_id=division_id,
            subject_id=item.subject_id,
            teacher_id=item.teacher_id,
            room_id=None,
            day=day,
            start_idx=start_idx,
            duration=2,
            state=state,
        )
        issues.extend(dyn_issues)
        if issues:
            return False, 0, issues
        penalty = dyn_penalty + self._default_penalty(division_id, item.subject_id, day, start_idx, 2, state)
        return True, penalty, []

    def evaluate_standard(
        self,
        request: StandardRequest,
        day: str,
        period_id: str,
        room_id: str,
        state: ScheduleState,
        exclude_sessions: Optional[Set[str]] = None,
    ) -> Tuple[bool, int, List[ConstraintIssue]]:
        if state.division_entries(request.division_id, day, period_id, exclude_sessions):
            return False, 0, [self._issue("division_occupied", f"Division {request.division_id} is already occupied on {day} {period_id}", division_id=request.division_id, day=day, period_id=period_id)]
        issues = []
        issues.extend(self._base_teacher_checks(request.teacher_id, day, [period_id], state, exclude_sessions))
        issues.extend(self._base_room_checks(room_id, "lecture", day, [period_id], state, exclude_sessions))
        dyn_issues, dyn_penalty = self._apply_dynamic_constraints(
            division_id=request.division_id,
            subject_id=request.subject_id,
            teacher_id=request.teacher_id,
            room_id=room_id,
            day=day,
            start_idx=self.period_index[period_id],
            duration=1,
            state=state,
            exclude_sessions=exclude_sessions,
        )
        issues.extend(dyn_issues)
        repeat_issue = self._same_day_standard_repeat_issue(
            request.division_id,
            request.subject_id,
            request.slot_type,
            day,
            state,
            exclude_sessions,
        )
        if repeat_issue:
            issues.append(repeat_issue)
        if issues:
            return False, 0, issues
        penalty = dyn_penalty + self._default_penalty(request.division_id, request.subject_id, day, self.period_index[period_id], 1, state, exclude_sessions)
        if request.preferred_room_id and room_id != request.preferred_room_id:
            penalty += 1
        return True, penalty, []

    def evaluate_parallel_member(self, division_id: str, member: ParallelMember, day: str, period_id: str, room_id: str, state: ScheduleState) -> Tuple[bool, int, List[ConstraintIssue]]:
        issues = []
        issues.extend(self._base_teacher_checks(member.teacher_id, day, [period_id], state))
        issues.extend(self._base_room_checks(room_id, "lecture", day, [period_id], state))
        dyn_issues, dyn_penalty = self._apply_dynamic_constraints(
            division_id=division_id,
            subject_id=member.subject_id,
            teacher_id=member.teacher_id,
            room_id=room_id,
            day=day,
            start_idx=self.period_index[period_id],
            duration=1,
            state=state,
        )
        issues.extend(dyn_issues)
        repeat_issue = self._same_day_standard_repeat_issue(
            division_id,
            member.subject_id,
            member.slot_type,
            day,
            state,
        )
        if repeat_issue:
            issues.append(repeat_issue)
        if issues:
            return False, 0, issues
        penalty = dyn_penalty + self._default_penalty(division_id, member.subject_id, day, self.period_index[period_id], 1, state)
        return True, penalty, []

    def evaluate_lab_item(self, division_id: str, item: LabBatchItem, day: str, start_idx: int, room_id: str, state: ScheduleState) -> Tuple[bool, int, List[ConstraintIssue]]:
        periods = self.period_ids[start_idx : start_idx + 2]
        issues = []
        issues.extend(self._base_teacher_checks(item.teacher_id, day, periods, state, allow_parallel_overlap=True))
        issues.extend(self._base_room_checks(room_id, "lab", day, periods, state))
        for period in periods:
            if state.batch_entries(item.batch_id, day, period):
                issues.append(self._issue("batch_overlap", f"Batch {item.batch_id} is already occupied on {day} {period}", batch_id=item.batch_id, day=day, period_id=period))
        dyn_issues, dyn_penalty = self._apply_dynamic_constraints(
            division_id=division_id,
            subject_id=item.subject_id,
            teacher_id=item.teacher_id,
            room_id=room_id,
            day=day,
            start_idx=start_idx,
            duration=2,
            state=state,
        )
        issues.extend(dyn_issues)
        if issues:
            return False, 0, issues
        penalty = dyn_penalty + self._default_penalty(division_id, item.subject_id, day, start_idx, 2, state)
        if item.preferred_room_id and room_id != item.preferred_room_id:
            penalty += 1
        return True, penalty, []

    def validate_state(self, state: ScheduleState) -> List[GenerationDiagnostic]:
        issues: List[GenerationDiagnostic] = []
        for (teacher_id, day, period_id), rows in state.teacher_slots.items():
            if teacher_id and len(rows) > 1:
                # Allow shared-lab facilitation: same teacher may supervise same-division/same-subject parallel batches.
                allowed_shared_lab = all(row.slot_type == "lab" for row in rows) and len({row.division_id for row in rows}) == 1 and len({row.subject_id for row in rows}) == 1
                if allowed_shared_lab:
                    continue
                issues.append(GenerationDiagnostic("validation", "teacher_overlap", f"Teacher {teacher_id} is double-booked on {day} {period_id}", {"teacher_id": teacher_id, "day": day, "period_id": period_id}))
        for (room_id, day, period_id), rows in state.room_slots.items():
            if room_id and len(rows) > 1:
                issues.append(GenerationDiagnostic("validation", "room_overlap", f"Room {room_id} is double-booked on {day} {period_id}", {"room_id": room_id, "day": day, "period_id": period_id}))
        for (division_id, day, period_id), rows in state.division_slots.items():
            non_lab = [row for row in rows if row.slot_type != "lab"]
            labs = [row for row in rows if row.slot_type == "lab"]
            if non_lab and labs:
                issues.append(GenerationDiagnostic("validation", "division_mixed_overlap", f"Division {division_id} has lecture/tutorial mixed with lab on {day} {period_id}", {"division_id": division_id, "day": day, "period_id": period_id}))
            if len(non_lab) > 1:
                groups = {row.parallel_group_id for row in non_lab}
                if None in groups or len(groups) != 1:
                    issues.append(GenerationDiagnostic("validation", "division_parallel_overlap", f"Division {division_id} has conflicting sessions on {day} {period_id}", {"division_id": division_id, "day": day, "period_id": period_id}))
            if labs:
                expected_batches = {batch["id"] for batch in self.context.batches.get(division_id, [])}
                actual_batches = [row.batch_id for row in labs if row.batch_id]
                if len(set(actual_batches)) != len(actual_batches):
                    issues.append(GenerationDiagnostic("validation", "batch_overlap", f"Division {division_id} has duplicate batch lab allocation on {day} {period_id}", {"division_id": division_id, "day": day, "period_id": period_id}))
                if expected_batches and set(actual_batches) != expected_batches:
                    issues.append(
                        GenerationDiagnostic(
                            "validation",
                            "parallel_lab_incomplete",
                            f"Division {division_id} does not have all batches scheduled in the same lab block on {day} {period_id}",
                            {"division_id": division_id, "day": day, "period_id": period_id, "expected_batches": sorted(expected_batches), "actual_batches": sorted(set(actual_batches))},
                        )
                    )

        for session_id, rows in state.session_slots.items():
            if rows and rows[0].slot_type == "lab":
                if len(rows) != 2:
                    issues.append(GenerationDiagnostic("validation", "lab_duration", f"Lab session {session_id} does not span exactly two periods", {"session_id": session_id}))
                    continue
                ordered = sorted(rows, key=lambda row: self.period_index[row.period_id])
                if ordered[0].day != ordered[1].day or self.period_index[ordered[1].period_id] != self.period_index[ordered[0].period_id] + 1:
                    issues.append(GenerationDiagnostic("validation", "lab_contiguity", f"Lab session {session_id} is not contiguous", {"session_id": session_id}))
                if self.session_crosses_break(self.period_index[ordered[0].period_id], 2):
                    issues.append(GenerationDiagnostic("validation", "lab_break_cross", f"Lab session {session_id} crosses a break boundary", {"session_id": session_id}))
                exclude = {session_id}
                batch_id = ordered[0].batch_id
                teacher_issues = []
                if ordered[0].status == "active":
                    teacher_issues = self._base_teacher_checks(
                        ordered[0].teacher_id,
                        ordered[0].day,
                        [ordered[0].period_id, ordered[1].period_id],
                        state,
                        exclude,
                        allow_parallel_overlap=True,
                    )
                room_issues = self._base_room_checks(ordered[0].room_id, "lab", ordered[0].day, [ordered[0].period_id, ordered[1].period_id], state, exclude)
                if batch_id:
                    for period in [ordered[0].period_id, ordered[1].period_id]:
                        if state.batch_entries(batch_id, ordered[0].day, period, exclude):
                            issues.append(GenerationDiagnostic("validation", "batch_overlap", f"Batch {batch_id} is double-booked on {ordered[0].day} {period}", {"batch_id": batch_id, "day": ordered[0].day, "period_id": period}))
                dyn_issues, _ = self._apply_dynamic_constraints(
                    division_id=ordered[0].division_id,
                    subject_id=ordered[0].subject_id,
                    teacher_id=ordered[0].teacher_id,
                    room_id=ordered[0].room_id,
                    day=ordered[0].day,
                    start_idx=self.period_index[ordered[0].period_id],
                    duration=2,
                    state=state,
                    exclude_sessions=exclude,
                )
                for issue in teacher_issues + room_issues + dyn_issues:
                    issues.append(GenerationDiagnostic("validation", issue.code, issue.message, issue.entities))
            elif rows:
                row = rows[0]
                exclude = {session_id}
                if not row.parallel_group_id and state.division_entries(row.division_id, row.day, row.period_id, exclude):
                    issues.append(GenerationDiagnostic("validation", "division_occupied", f"Division {row.division_id} has multiple sessions on {row.day} {row.period_id}", {"division_id": row.division_id, "day": row.day, "period_id": row.period_id}))
                teacher_issues = []
                if row.status == "active":
                    teacher_issues = self._base_teacher_checks(row.teacher_id, row.day, [row.period_id], state, exclude)
                room_issues = self._base_room_checks(row.room_id, "lecture", row.day, [row.period_id], state, exclude)
                dyn_issues, _ = self._apply_dynamic_constraints(
                    division_id=row.division_id,
                    subject_id=row.subject_id,
                    teacher_id=row.teacher_id,
                    room_id=row.room_id,
                    day=row.day,
                    start_idx=self.period_index[row.period_id],
                    duration=1,
                    state=state,
                    exclude_sessions=exclude,
                )
                repeat_issue = self._same_day_standard_repeat_issue(
                    row.division_id,
                    row.subject_id,
                    row.slot_type,
                    row.day,
                    state,
                    exclude,
                )
                if repeat_issue:
                    dyn_issues.append(repeat_issue)
                for issue in teacher_issues + room_issues + dyn_issues:
                    issues.append(GenerationDiagnostic("validation", issue.code, issue.message, issue.entities))

        lab_subject_day_periods: Dict[Tuple[str, str, str], Set[str]] = defaultdict(set)
        for session_rows in state.session_slots.values():
            if not session_rows or session_rows[0].slot_type != "lab":
                continue
            first = session_rows[0]
            ordered = sorted(session_rows, key=lambda row: self.period_index[row.period_id])
            lab_subject_day_periods[(first.division_id, first.subject_id, first.day)].add(ordered[0].period_id)
        for (division_id, subject_id, day), start_periods in lab_subject_day_periods.items():
            if len(start_periods) > 1:
                issues.append(
                    GenerationDiagnostic(
                        "validation",
                        "same_day_repeat",
                        f"{subject_id} lab cannot repeat on {day}",
                        {"division_id": division_id, "subject_id": subject_id, "day": day, "period_ids": sorted(start_periods, key=lambda pid: self.period_index[pid])},
                    )
                )

        for constraint in self.context.constraints:
            if constraint["constraint_type"] != "no_free_slots" or constraint.get("priority") != "hard":
                continue
            division_ids = constraint.get("scope", {}).get("division_ids") or list(self.context.divisions.keys())
            for division_id in division_ids:
                for day in self.days:
                    for period_id in self.period_ids:
                        if not state.division_entries(division_id, day, period_id):
                            issues.append(GenerationDiagnostic("validation", "no_free_slots", f"No-free-slot hard rule failed for division {division_id} on {day} {period_id}", {"division_id": division_id, "day": day, "period_id": period_id}))
                            return issues
        return issues


class TimetableSolver:
    def __init__(
        self,
        period_ids: List[str],
        days: Optional[List[str]] = None,
        seed: int = 42,
        db=None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        search_limits: Optional[Dict[str, List[Optional[int]]]] = None,
        max_phase_seconds: Optional[float] = None,
    ):
        self.db = db if db is not None else get_db()
        self.seed = seed
        self.context = SchedulingContext.from_db(self.db, period_ids=period_ids, days=days)
        self.evaluator = ConstraintEvaluator(self.context)
        self.state = ScheduleState(self.context.period_ids, self.context.days)
        self.assignments: List[SlotRecord] = []
        self.lab_assignments: List[SlotRecord] = []
        self.coverage_report: Dict[str, Any] = {}
        self.diagnostics: List[GenerationDiagnostic] = []
        self.failure: Optional[GenerationDiagnostic] = None
        self.parallel_specs: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.progress_callback = progress_callback
        self.unscheduled: List[Dict[str, Any]] = []
        self.partial = False
        self.search_limits = search_limits or {
            "lectures": [18, 54, None],
            "labs": [8, 20, None],
            "parallel": [8, 20, None],
        }
        self.max_phase_seconds = max_phase_seconds
        self._base_lecture_room_ids = tuple(room["id"] for room in sorted(self.context.lecture_rooms, key=lambda room: room["id"]))
        self._base_lab_room_ids = tuple(room["id"] for room in sorted(self.context.lab_rooms, key=lambda room: room["id"]))
        self._lecture_room_ids_cache: Dict[Optional[str], Tuple[str, ...]] = {}
        self._lab_room_ids_cache: Dict[Optional[str], Tuple[str, ...]] = {}

    def _emit_progress(self, stage: str, message: str, percent: Optional[float] = None, **details):
        if not self.progress_callback:
            return
        payload: Dict[str, Any] = {"stage": stage, "message": message}
        if percent is not None:
            payload["percent"] = round(float(percent), 2)
        if details:
            payload["details"] = details
        self.progress_callback(payload)

    def _best_issue(self, issue_counts: Dict[str, Tuple[int, ConstraintIssue]]) -> ConstraintIssue:
        if not issue_counts:
            return ConstraintIssue("no_candidate", "No valid candidate could be found")
        _, issue = max(issue_counts.values(), key=lambda item: item[0])
        return issue

    def _request_summary(self, request: Any, phase: str) -> Dict[str, Any]:
        summary = {
            "phase": phase,
            "request_id": getattr(request, "request_id", None),
            "display_name": getattr(request, "display_name", str(request)),
            "division_id": getattr(request, "division_id", None),
            "subject_id": getattr(request, "subject_id", None),
            "slot_type": getattr(request, "slot_type", None),
        }
        if isinstance(request, LabBlockRequest):
            summary["batch_ids"] = list(request.batch_ids)
        if isinstance(request, ParallelRequest):
            summary["subject_ids"] = [member.subject_id for member in request.members]
            summary["parallel_group_id"] = request.group_id
        return summary

    def _record_unscheduled_request(self, phase: str, request: Any, issue: ConstraintIssue):
        self.unscheduled.append(
            {
                **self._request_summary(request, phase),
                "reason_code": issue.code,
                "reason": issue.message,
                "entities": issue.entities,
            }
        )

    def _record_issue(self, store: Dict[str, Tuple[int, ConstraintIssue]], issues: Iterable[ConstraintIssue]):
        for issue in issues:
            count, sample = store.get(issue.code, (0, issue))
            store[issue.code] = (count + 1, sample)

    def _fail(self, phase: str, request_name: str, issue: ConstraintIssue, **entities) -> bool:
        self.failure = GenerationDiagnostic(
            phase=phase,
            code=issue.code,
            message=f"Unable to place {request_name}: {issue.message}",
            entities={**entities, **issue.entities},
        )
        self.diagnostics.append(self.failure)
        return False

    def validate_inputs(self) -> List[GenerationDiagnostic]:
        problems: List[GenerationDiagnostic] = []
        ctx = self.context
        if not ctx.teachers:
            problems.append(GenerationDiagnostic("input", "teachers_missing", "Add at least one teacher first"))
        if not ctx.lecture_rooms:
            problems.append(GenerationDiagnostic("input", "lecture_rooms_missing", "Add at least one lecture room first"))
        if not ctx.subjects:
            problems.append(GenerationDiagnostic("input", "subjects_missing", "Add at least one subject first"))
        if not ctx.divisions:
            problems.append(GenerationDiagnostic("input", "divisions_missing", "Add at least one division first"))
        if not ctx.division_subjects:
            problems.append(GenerationDiagnostic("input", "division_subjects_missing", "Assign subjects to at least one division"))

        for division_id, subject_ids in ctx.division_subjects.items():
            if division_id not in ctx.divisions:
                problems.append(GenerationDiagnostic("input", "division_missing", f"Division {division_id} is referenced in division_subjects but does not exist", {"division_id": division_id}))
            for subject_id in subject_ids:
                subject = ctx.subjects.get(subject_id)
                if not subject:
                    problems.append(GenerationDiagnostic("input", "subject_missing", f"Subject {subject_id} assigned to division {division_id} does not exist", {"division_id": division_id, "subject_id": subject_id}))
                    continue
                if int(subject.get("lectures_per_week", 0) or 0) > 0 and not subject.get("teacher_id"):
                    problems.append(GenerationDiagnostic("input", "subject_teacher_missing", f"Subject {subject_id} in division {division_id} has lecture hours but no teacher", {"division_id": division_id, "subject_id": subject_id}))
                if subject.get("has_tutorial") and int(subject.get("tutorials_per_week", 0) or 0) > 0 and not subject.get("teacher_id"):
                    problems.append(GenerationDiagnostic("input", "tutorial_teacher_missing", f"Subject {subject_id} in division {division_id} has tutorial hours but no teacher", {"division_id": division_id, "subject_id": subject_id}))
                if subject.get("has_lab"):
                    lab_hours = int(subject.get("lab_hours_per_week", 0) or 0)
                    if lab_hours % 2 != 0:
                        problems.append(GenerationDiagnostic("input", "lab_hours_invalid", f"Subject {subject_id} has odd lab hours; labs must be exactly two consecutive periods", {"subject_id": subject_id}))
                    if lab_hours and not ctx.lab_rooms:
                        problems.append(GenerationDiagnostic("input", "lab_rooms_missing", "Add at least one lab room first"))
                    if lab_hours and not ctx.batches.get(division_id):
                        problems.append(GenerationDiagnostic("input", "lab_batches_missing", f"Division {division_id} needs batches before lab sessions can be scheduled", {"division_id": division_id}))
                    for batch in ctx.batches.get(division_id, []):
                        teacher_id = ctx.batch_teachers.get((batch["id"], subject_id)) or subject.get("lab_teacher_id")
                        if lab_hours and not teacher_id:
                            problems.append(GenerationDiagnostic("input", "lab_teacher_missing", f"Batch {batch['id']} needs a lab teacher for subject {subject_id}", {"division_id": division_id, "subject_id": subject_id, "batch_id": batch["id"]}))
                        if teacher_id and teacher_id not in ctx.teachers:
                            problems.append(GenerationDiagnostic("input", "lab_teacher_invalid", f"Batch {batch['id']} references missing lab teacher {teacher_id}", {"division_id": division_id, "subject_id": subject_id, "batch_id": batch["id"], "teacher_id": teacher_id}))
        for division_id, batches in sorted(ctx.batches.items()):
            lab_subjects = [
                subject_id
                for subject_id in ctx.division_subjects.get(division_id, [])
                if (ctx.subjects.get(subject_id) or {}).get("has_lab") and int((ctx.subjects.get(subject_id) or {}).get("lab_hours_per_week", 0) or 0) > 0
            ]
            if lab_subjects and len(batches) > len(ctx.lab_rooms):
                problems.append(
                    GenerationDiagnostic(
                        "input",
                        "grouped_lab_room_shortage",
                        f"Division {division_id} needs {len(batches)} lab rooms for parallel batch labs, but only {len(ctx.lab_rooms)} exist",
                        {"division_id": division_id, "required_rooms": len(batches), "available_rooms": len(ctx.lab_rooms)},
                    )
                )

        weekly_capacity = len(ctx.period_ids) * len(ctx.days)
        for division_id, subject_ids in sorted(ctx.division_subjects.items()):
            lecture_tutorial_sessions = 0
            lab_blocks = 0
            for subject_id in subject_ids:
                subject = ctx.subjects.get(subject_id)
                if not subject:
                    continue
                lecture_tutorial_sessions += int(subject.get("lectures_per_week", 0) or 0)
                if subject.get("has_tutorial"):
                    lecture_tutorial_sessions += int(subject.get("tutorials_per_week", 0) or 0)
                if subject.get("has_lab"):
                    lab_blocks += max(0, int(subject.get("lab_hours_per_week", 0) or 0) // 2)
            required_periods = lecture_tutorial_sessions + (lab_blocks * 2)
            if required_periods > weekly_capacity:
                problems.append(
                    GenerationDiagnostic(
                        "input",
                        "division_over_capacity",
                        (
                            f"Division {division_id} needs {required_periods} weekly periods "
                            f"but only {weekly_capacity} are available"
                        ),
                        {
                            "division_id": division_id,
                            "required_periods": required_periods,
                            "weekly_capacity": weekly_capacity,
                            "lecture_tutorial_sessions": lecture_tutorial_sessions,
                            "lab_blocks": lab_blocks,
                        },
                    )
                )

        for constraint in ctx.constraints:
            if constraint["constraint_type"] != "parallel_group":
                continue
            params = constraint.get("params") or {}
            divisions = constraint.get("scope", {}).get("division_ids") or []
            subject_ids = params.get("subject_ids") or []
            slot_type = params.get("slot_type", "lecture")
            if len(subject_ids) < 2:
                problems.append(GenerationDiagnostic("input", "parallel_group_invalid", "Parallel group requires at least two subjects"))
                continue
            for division_id in divisions:
                counts = []
                teachers = []
                for subject_id in subject_ids:
                    if subject_id not in ctx.division_subjects.get(division_id, []):
                        problems.append(GenerationDiagnostic("input", "parallel_group_subject_missing", f"Parallel group references subject {subject_id} that is not assigned to division {division_id}", {"division_id": division_id, "subject_id": subject_id}))
                        continue
                    subject = ctx.subjects.get(subject_id)
                    if not subject:
                        continue
                    count = int(subject.get("lectures_per_week", 0) or 0) if slot_type == "lecture" else int(subject.get("tutorials_per_week", 0) or 0)
                    counts.append(count)
                    teachers.append(subject.get("teacher_id"))
                session_count = params.get("session_count")
                if session_count is None and counts and len(set(counts)) > 1:
                    problems.append(GenerationDiagnostic("input", "parallel_group_count_mismatch", f"Parallel group in division {division_id} has mismatched weekly counts; set an explicit session_count", {"division_id": division_id, "subject_ids": subject_ids}))
                if session_count is not None:
                    requested = int(session_count)
                    for subject_id, count in zip(subject_ids, counts):
                        if requested > count:
                            problems.append(GenerationDiagnostic("input", "parallel_group_count_overflow", f"Parallel group requests {requested} sessions but subject {subject_id} only has {count}", {"division_id": division_id, "subject_id": subject_id}))
                if len(set(teacher for teacher in teachers if teacher)) != len([teacher for teacher in teachers if teacher]):
                    problems.append(GenerationDiagnostic("input", "parallel_group_teacher_overlap", f"Parallel group in division {division_id} reuses the same teacher across simultaneous subjects", {"division_id": division_id, "subject_ids": subject_ids}))
        return problems

    def _parallel_plans(self) -> Tuple[List[ParallelRequest], Dict[Tuple[str, str, str], int]]:
        requests: List[ParallelRequest] = []
        consumed: Dict[Tuple[str, str, str], int] = defaultdict(int)
        for constraint in self.context.constraints:
            if constraint["constraint_type"] != "parallel_group":
                continue
            params = constraint.get("params") or {}
            slot_type = params.get("slot_type", "lecture")
            base_group_name = params.get("group_name") or f"parallel-{constraint.get('int_id', 'x')}"
            divisions = constraint.get("scope", {}).get("division_ids") or []
            subject_ids = list(params.get("subject_ids") or [])
            for division_id in divisions:
                counts = []
                members: List[Tuple[str, str]] = []
                for subject_id in subject_ids:
                    subject = self.context.subjects[subject_id]
                    teacher_id = subject.get("teacher_id")
                    count = int(subject.get("lectures_per_week", 0) or 0) if slot_type == "lecture" else int(subject.get("tutorials_per_week", 0) or 0)
                    counts.append(count)
                    members.append((subject_id, teacher_id))
                session_count = int(params.get("session_count") or min(counts))
                base_group_id = f"PG:{constraint.get('int_id', base_group_name)}:{division_id}"
                self.parallel_specs[(division_id, base_group_id)] = {
                    "subject_ids": list(subject_ids),
                    "session_count": session_count,
                    "group_name": base_group_name,
                }
                for idx in range(1, session_count + 1):
                    member_rows = [ParallelMember(subject_id=subject_id, teacher_id=teacher_id, slot_type=slot_type, session_id=f"{base_group_id}:{subject_id}:{idx}") for subject_id, teacher_id in members]
                    requests.append(ParallelRequest(request_id=f"{base_group_id}:{idx}", division_id=division_id, group_id=base_group_id, group_name=base_group_name, occurrence_index=idx, members=member_rows))
                for subject_id in subject_ids:
                    consumed[(division_id, subject_id, slot_type)] += session_count
        requests.sort(key=lambda req: (req.division_id, req.group_name, req.occurrence_index))
        return requests, dict(consumed)

    def _lab_session_requirements(self, division_id: str) -> Dict[Tuple[str, str], int]:
        requirements: Dict[Tuple[str, str], int] = {}
        for batch in self.context.batches.get(division_id, []):
            for subject_id in self.context.division_subjects.get(division_id, []):
                subject = self.context.subjects.get(subject_id)
                if not subject or not subject.get("has_lab"):
                    continue
                sessions = int(subject.get("lab_hours_per_week", 0) or 0) // 2
                if sessions <= 0:
                    continue
                requirements[(batch["id"], subject_id)] = sessions
        return requirements

    def _scheduled_lab_session_counts(self, division_id: str, state: Optional[ScheduleState] = None) -> Dict[Tuple[str, str], int]:
        state = state or self.state
        counts: Dict[Tuple[str, str], int] = {}
        for (current_division_id, batch_id, subject_id), count in state.lab_session_counts.items():
            if current_division_id == division_id:
                counts[(batch_id, subject_id)] = count
        return counts

    def _remaining_lab_session_counts(self, division_id: str, state: Optional[ScheduleState] = None) -> Dict[Tuple[str, str], int]:
        required = self._lab_session_requirements(division_id)
        scheduled = self._scheduled_lab_session_counts(division_id, state)
        remaining = {}
        for key, value in required.items():
            remaining[key] = max(0, value - scheduled.get(key, 0))
        return remaining

    def _division_required_lab_blocks(self, division_id: str) -> int:
        requirements = self._lab_session_requirements(division_id)
        batch_ids = [batch["id"] for batch in self.context.batches.get(division_id, [])]
        if not batch_ids:
            return 0
        return sum(requirements.get((batch_ids[0], subject_id), 0) for subject_id in self.context.division_subjects.get(division_id, []))

    def _lab_subject_day_start_periods(self, division_id: str, subject_id: str, day: str, state: Optional[ScheduleState] = None) -> Set[str]:
        state = state or self.state
        return state.lab_subject_day_start_periods(division_id, subject_id, day)

    def _division_day_lab_block_count(self, division_id: str, day: str, state: Optional[ScheduleState] = None) -> int:
        state = state or self.state
        return state.division_day_lab_block_count(division_id, day)

    def _division_day_period_load(
        self,
        division_id: str,
        day: str,
        state: Optional[ScheduleState] = None,
        extra_periods: Optional[Iterable[str]] = None,
    ) -> int:
        state = state or self.state
        occupied = set((state.division_day_period_counts.get((division_id, day), {}) or {}).keys())
        if extra_periods:
            occupied.update(extra_periods)
        return len(occupied)

    def _division_day_balance_penalty(
        self,
        division_id: str,
        day: str,
        periods: Iterable[str],
        state: Optional[ScheduleState] = None,
    ) -> int:
        projected_load = self._division_day_period_load(division_id, day, state, periods)
        return projected_load * projected_load

    def _build_standard_requests(self, consumed_parallel: Dict[Tuple[str, str, str], int]) -> List[StandardRequest]:
        requests: List[StandardRequest] = []
        for division_id, subject_ids in sorted(self.context.division_subjects.items()):
            for subject_id in sorted(subject_ids):
                subject = self.context.subjects.get(subject_id)
                if not subject:
                    continue
                teacher_id = subject.get("teacher_id")
                preferred_room_id = self.context.divisions.get(division_id, {}).get("room_id")
                lecture_count = int(subject.get("lectures_per_week", 0) or 0) - consumed_parallel.get((division_id, subject_id, "lecture"), 0)
                tutorial_count = int(subject.get("tutorials_per_week", 0) or 0) - consumed_parallel.get((division_id, subject_id, "tutorial"), 0)
                for idx in range(1, max(lecture_count, 0) + 1):
                    requests.append(StandardRequest(request_id=f"STD:{division_id}:{subject_id}:lecture:{idx}", division_id=division_id, subject_id=subject_id, teacher_id=teacher_id, slot_type="lecture", occurrence_index=idx, preferred_room_id=preferred_room_id))
                if subject.get("has_tutorial"):
                    for idx in range(1, max(tutorial_count, 0) + 1):
                        requests.append(StandardRequest(request_id=f"STD:{division_id}:{subject_id}:tutorial:{idx}", division_id=division_id, subject_id=subject_id, teacher_id=teacher_id, slot_type="tutorial", occurrence_index=idx, preferred_room_id=preferred_room_id))
        requests.sort(key=lambda req: (req.division_id, req.subject_id, req.slot_type, req.occurrence_index))
        return requests

    def _build_lab_requests(self) -> List[LabBlockRequest]:
        requests: List[LabBlockRequest] = []
        for division_id in sorted(self.context.divisions):
            batch_ids = [batch["id"] for batch in self.context.batches.get(division_id, [])]
            total_blocks = self._division_required_lab_blocks(division_id)
            for idx in range(1, total_blocks + 1):
                requests.append(
                    LabBlockRequest(
                        request_id=f"LABBLOCK:{division_id}:{idx}",
                        division_id=division_id,
                        occurrence_index=idx,
                        batch_ids=batch_ids,
                    )
                )
        requests.sort(key=lambda req: (-len(req.batch_ids), req.division_id, req.occurrence_index))
        return requests

    def _ordered_room_ids(self, base_room_ids: Tuple[str, ...], cache: Dict[Optional[str], Tuple[str, ...]], preferred: Optional[str] = None) -> Tuple[str, ...]:
        if preferred in cache:
            return cache[preferred]
        if preferred and preferred in base_room_ids:
            ordered = (preferred,) + tuple(room_id for room_id in base_room_ids if room_id != preferred)
        else:
            ordered = base_room_ids
        cache[preferred] = ordered
        return ordered

    def _lecture_room_ids(self, preferred: Optional[str] = None) -> Tuple[str, ...]:
        return self._ordered_room_ids(self._base_lecture_room_ids, self._lecture_room_ids_cache, preferred)

    def _lab_room_ids(self, preferred: Optional[str] = None) -> Tuple[str, ...]:
        return self._ordered_room_ids(self._base_lab_room_ids, self._lab_room_ids_cache, preferred)

    def _standard_candidates(self, request: StandardRequest, limit: Optional[int] = None) -> Tuple[List[PlacementCandidate], Dict[str, Tuple[int, ConstraintIssue]]]:
        issue_counts: Dict[str, Tuple[int, ConstraintIssue]] = {}
        candidates: List[PlacementCandidate] = []
        rooms = self._lecture_room_ids(request.preferred_room_id)
        for day_index, day in enumerate(self.context.days):
            for period_index, period_id in enumerate(self.context.period_ids):
                if not self.evaluator.has_room_scoped_constraints:
                    ok, base_penalty, issues = self.evaluator.evaluate_standard_base(request, day, period_id, self.state)
                    if not ok:
                        self._record_issue(issue_counts, issues)
                        continue
                    balance_penalty = self._division_day_balance_penalty(request.division_id, day, [period_id], self.state)
                    available_rooms = [room_id for room_id in rooms if not self.state.room_slots.get((room_id, day, period_id))]
                    if not available_rooms:
                        self._record_issue(issue_counts, [ConstraintIssue("room_overlap", f"No lecture room is available on {day} {period_id}", {"day": day, "period_id": period_id})])
                        continue
                    for room_id in available_rooms:
                        penalty = base_penalty + balance_penalty
                        if request.preferred_room_id and room_id != request.preferred_room_id:
                            penalty += 1
                        slot = SlotRecord(
                            division_id=request.division_id,
                            day=day,
                            period_id=period_id,
                            subject_id=request.subject_id,
                            teacher_id=request.teacher_id,
                            room_id=room_id,
                            slot_type=request.slot_type,
                            session_id=request.request_id,
                            occupancy_key="division",
                        )
                        candidates.append(PlacementCandidate(slots=[slot], penalty=penalty, sort_key=(penalty, day_index, period_index, room_id)))
                    continue
                for room_id in rooms:
                    ok, penalty, issues = self.evaluator.evaluate_standard(request, day, period_id, room_id, self.state)
                    if not ok:
                        self._record_issue(issue_counts, issues)
                        continue
                    slot = SlotRecord(
                        division_id=request.division_id,
                        day=day,
                        period_id=period_id,
                        subject_id=request.subject_id,
                        teacher_id=request.teacher_id,
                        room_id=room_id,
                        slot_type=request.slot_type,
                        session_id=request.request_id,
                        occupancy_key="division",
                    )
                    total_penalty = penalty + self._division_day_balance_penalty(request.division_id, day, [period_id], self.state)
                    candidates.append(PlacementCandidate(slots=[slot], penalty=total_penalty, sort_key=(total_penalty, day_index, period_index, room_id)))
        candidates.sort(key=lambda candidate: candidate.sort_key)
        if limit is not None:
            candidates = candidates[:limit]
        return candidates, issue_counts

    def _assign_distinct_rooms(self, options_payload: List[Tuple[int, List[Tuple[str, int]]]], max_results: Optional[int] = None) -> List[Tuple[Dict[int, str], int]]:
        assignments: List[Tuple[Dict[int, str], int]] = []
        ordered_payload = sorted(
            [(slot_index, sorted(options, key=lambda item: (item[1], item[0]))) for slot_index, options in options_payload],
            key=lambda item: (len(item[1]), item[0]),
        )

        def backtrack(index: int, used: Set[str], current: Dict[int, str], penalty: int):
            if max_results is not None and len(assignments) >= max_results:
                return
            if index == len(options_payload):
                assignments.append((dict(current), penalty))
                return
            slot_index, options = ordered_payload[index]
            for room_id, extra_penalty in options:
                if room_id in used:
                    continue
                current[slot_index] = room_id
                used.add(room_id)
                backtrack(index + 1, used, current, penalty + extra_penalty)
                used.remove(room_id)
                del current[slot_index]

        backtrack(0, set(), {}, 0)
        return assignments

    def _parallel_candidates(self, request: ParallelRequest, limit: Optional[int] = None) -> Tuple[List[PlacementCandidate], Dict[str, Tuple[int, ConstraintIssue]]]:
        issue_counts: Dict[str, Tuple[int, ConstraintIssue]] = {}
        candidates: List[PlacementCandidate] = []
        room_ids = self._lecture_room_ids()
        member_teachers = [member.teacher_id for member in request.members if member.teacher_id]
        if len(set(member_teachers)) != len(member_teachers):
            self._record_issue(
                issue_counts,
                [
                    ConstraintIssue(
                        "teacher_overlap",
                        f"Parallel group {request.group_name} reuses a teacher in the same period",
                        {"division_id": request.division_id, "parallel_group_id": request.group_id},
                    )
                ],
            )
            return [], issue_counts
        for day_index, day in enumerate(self.context.days):
            for period_index, period_id in enumerate(self.context.period_ids):
                if self.state.division_entries(request.division_id, day, period_id):
                    self._record_issue(issue_counts, [ConstraintIssue("division_occupied", f"Division {request.division_id} is already occupied on {day} {period_id}")])
                    continue
                available_rooms = None
                if not self.evaluator.has_room_scoped_constraints:
                    available_rooms = [room_id for room_id in room_ids if not self.state.room_slots.get((room_id, day, period_id))]
                    if len(available_rooms) < len(request.members):
                        self._record_issue(issue_counts, [ConstraintIssue("distinct_rooms_unavailable", f"Not enough distinct lecture rooms for parallel group {request.group_name}")])
                        continue
                member_options = []
                local_issues: List[ConstraintIssue] = []
                failed_here = False
                for member in request.members:
                    options = []
                    if not self.evaluator.has_room_scoped_constraints:
                        ok, base_penalty, issues = self.evaluator.evaluate_parallel_member_base(request.division_id, member, day, period_id, self.state)
                        if not ok:
                            local_issues.extend(issues)
                        else:
                            for room_id in available_rooms or ():
                                options.append((room_id, base_penalty))
                    else:
                        for room_id in room_ids:
                            ok, penalty, issues = self.evaluator.evaluate_parallel_member(request.division_id, member, day, period_id, room_id, self.state)
                            if ok:
                                options.append((room_id, penalty))
                            else:
                                local_issues.extend(issues)
                    if not options:
                        failed_here = True
                        break
                    member_options.append((member, options))
                if failed_here:
                    self._record_issue(issue_counts, local_issues)
                    continue
                options_payload = [(idx, options) for idx, (_, options) in enumerate(member_options)]
                allocation_limit = None if limit is None else max(limit * 2, len(member_options))
                allocations = self._assign_distinct_rooms(options_payload, max_results=allocation_limit)
                if not allocations:
                    self._record_issue(issue_counts, [ConstraintIssue("distinct_rooms_unavailable", f"Not enough distinct lecture rooms for parallel group {request.group_name}")])
                    continue
                for allocation, total_penalty in allocations:
                    slots = []
                    for idx, (member, _) in enumerate(member_options):
                        room_id = allocation[idx]
                        slots.append(
                            SlotRecord(
                                division_id=request.division_id,
                                day=day,
                                period_id=period_id,
                                subject_id=member.subject_id,
                                teacher_id=member.teacher_id,
                                room_id=room_id,
                                slot_type=member.slot_type,
                                session_id=member.session_id,
                                occupancy_key=f"parallel:{request.group_id}:{member.subject_id}",
                                parallel_group_id=request.group_id,
                            )
                        )
                    balance_penalty = self._division_day_balance_penalty(request.division_id, day, [period_id], self.state)
                    combined_penalty = total_penalty + balance_penalty
                    candidates.append(
                        PlacementCandidate(
                            slots=slots,
                            penalty=combined_penalty,
                            sort_key=(combined_penalty, day_index, period_index, tuple(sorted(allocation.values()))),
                        )
                    )
        candidates.sort(key=lambda candidate: candidate.sort_key)
        if limit is not None:
            candidates = candidates[:limit]
        return candidates, issue_counts

    def _lab_block_batch_options(self, request: LabBlockRequest, day: str) -> Tuple[Dict[str, List[LabBatchItem]], Dict[str, Tuple[int, ConstraintIssue]]]:
        issue_counts: Dict[str, Tuple[int, ConstraintIssue]] = {}
        remaining = self._remaining_lab_session_counts(request.division_id)
        options_by_batch: Dict[str, List[LabBatchItem]] = {}
        for batch_id in request.batch_ids:
            options: List[LabBatchItem] = []
            for subject_id in sorted(self.context.division_subjects.get(request.division_id, [])):
                if remaining.get((batch_id, subject_id), 0) <= 0:
                    continue
                if self._lab_subject_day_start_periods(request.division_id, subject_id, day, self.state):
                    self._record_issue(
                        issue_counts,
                        [
                            ConstraintIssue(
                                "same_day_repeat",
                                f"{subject_id} lab cannot repeat on {day}",
                                {"division_id": request.division_id, "subject_id": subject_id, "day": day},
                            )
                        ],
                    )
                    continue
                subject = self.context.subjects.get(subject_id) or {}
                teacher_id = self.context.batch_teachers.get((batch_id, subject_id)) or subject.get("lab_teacher_id")
                options.append(
                    LabBatchItem(
                        batch_id=batch_id,
                        subject_id=subject_id,
                        teacher_id=teacher_id,
                        session_id=f"LAB:{request.division_id}:{subject_id}:{batch_id}:{request.occurrence_index}",
                        preferred_room_id=subject.get("lab_room_id"),
                    )
                )
            options_by_batch[batch_id] = options
        return options_by_batch, issue_counts

    def _enumerate_lab_batch_assignments(
        self,
        request: LabBlockRequest,
        options_by_batch: Dict[str, List[LabBatchItem]],
        max_results: Optional[int] = None,
    ) -> List[List[LabBatchItem]]:
        assignments: List[List[LabBatchItem]] = []
        ordered_batches = sorted(request.batch_ids, key=lambda batch_id: (len(options_by_batch.get(batch_id, [])), batch_id))

        def backtrack(index: int, current: List[LabBatchItem]):
            if max_results is not None and len(assignments) >= max_results:
                return
            if index == len(ordered_batches):
                assignments.append(list(current))
                return
            batch_id = ordered_batches[index]
            for item in sorted(options_by_batch.get(batch_id, []), key=lambda row: (row.subject_id, row.batch_id, row.teacher_id or "")):
                current.append(item)
                backtrack(index + 1, current)
                current.pop()

        backtrack(0, [])
        return assignments

    def _lab_candidates(self, request: LabBlockRequest, limit: Optional[int] = None) -> Tuple[List[PlacementCandidate], Dict[str, Tuple[int, ConstraintIssue]]]:
        issue_counts: Dict[str, Tuple[int, ConstraintIssue]] = {}
        candidates: List[PlacementCandidate] = []
        room_ids = self._lab_room_ids()
        if len(request.batch_ids) > len(room_ids):
            self._record_issue(
                issue_counts,
                [
                    ConstraintIssue(
                        "grouped_lab_room_shortage",
                        f"Division {request.division_id} needs {len(request.batch_ids)} lab rooms for a parallel lab block, but only {len(room_ids)} are available",
                        {"division_id": request.division_id, "required_rooms": len(request.batch_ids), "available_rooms": len(room_ids)},
                    )
                ],
            )
            return [], issue_counts
        max_start = len(self.context.period_ids) - 2
        end_of_day_start_idx = self.evaluator.end_of_day_lab_start_idx()
        covered_days = self.evaluator.scheduled_end_of_day_lab_days(self.state)
        day_sequence = sorted(enumerate(self.context.days), key=lambda item: (item[1] in covered_days, item[0]))
        start_indices = list(range(max_start + 1))
        if end_of_day_start_idx is not None and end_of_day_start_idx in start_indices:
            start_indices.remove(end_of_day_start_idx)
            start_indices.insert(0, end_of_day_start_idx)
        for day_index, day in day_sequence:
            options_by_batch, batch_issue_counts = self._lab_block_batch_options(request, day)
            for code, item in batch_issue_counts.items():
                count, sample = issue_counts.get(code, (0, item[1]))
                issue_counts[code] = (count + item[0], sample)
            if any(not options_by_batch.get(batch_id) for batch_id in request.batch_ids):
                continue
            batch_assignment_limit = None if limit is None else max(limit * 2, len(request.batch_ids))
            batch_assignments = self._enumerate_lab_batch_assignments(request, options_by_batch, max_results=batch_assignment_limit)
            if not batch_assignments:
                self._record_issue(
                    issue_counts,
                    [
                        ConstraintIssue(
                            "grouped_lab_teacher_overlap",
                            f"Division {request.division_id} does not have a distinct-teacher subject combination for all batches on {day}",
                            {"division_id": request.division_id, "day": day},
                        )
                    ],
                )
                continue
            for start_idx in start_indices:
                if self.evaluator.session_crosses_break(start_idx, 2):
                    self._record_issue(issue_counts, [ConstraintIssue("lab_break_cross", "Lab crosses a configured break boundary")])
                    continue
                periods = self.context.period_ids[start_idx : start_idx + 2]
                if any(self.state.division_entries(request.division_id, day, period) for period in periods):
                    self._record_issue(issue_counts, [ConstraintIssue("division_occupied", f"Division {request.division_id} is occupied during {day} {'/'.join(periods)}")])
                    continue
                available_lab_rooms = None
                available_lab_room_set = None
                if not self.evaluator.has_room_scoped_constraints:
                    available_lab_rooms = [
                        room_id
                        for room_id in self._base_lab_room_ids
                        if not self.state.room_slots.get((room_id, day, periods[0])) and not self.state.room_slots.get((room_id, day, periods[1]))
                    ]
                    if len(available_lab_rooms) < len(request.batch_ids):
                        self._record_issue(issue_counts, [ConstraintIssue("distinct_rooms_unavailable", f"Not enough distinct lab rooms for division {request.division_id} parallel lab block")])
                        continue
                    available_lab_room_set = set(available_lab_rooms)
                for assigned_items in batch_assignments:
                    item_options = []
                    local_issues: List[ConstraintIssue] = []
                    failed_here = False
                    for item in assigned_items:
                        options = []
                        subject_room_ids = self._lab_room_ids(item.preferred_room_id)
                        if not self.evaluator.has_room_scoped_constraints:
                            ok, base_penalty, issues = self.evaluator.evaluate_lab_item_base(request.division_id, item, day, start_idx, self.state)
                            if not ok:
                                local_issues.extend(issues)
                            else:
                                for room_id in subject_room_ids:
                                    if available_lab_room_set is not None and room_id not in available_lab_room_set:
                                        continue
                                    penalty = base_penalty
                                    if item.preferred_room_id and room_id != item.preferred_room_id:
                                        penalty += 1
                                    options.append((room_id, penalty))
                        else:
                            for room_id in subject_room_ids:
                                ok, penalty, issues = self.evaluator.evaluate_lab_item(request.division_id, item, day, start_idx, room_id, self.state)
                                if ok:
                                    options.append((room_id, penalty))
                                else:
                                    local_issues.extend(issues)
                        if not options:
                            failed_here = True
                            break
                        item_options.append((item, options))
                    if failed_here:
                        self._record_issue(issue_counts, local_issues)
                        continue
                    options_payload = [(idx, options) for idx, (_, options) in enumerate(item_options)]
                    allocation_limit = None if limit is None else max(limit * 2, len(item_options))
                    allocations = self._assign_distinct_rooms(options_payload, max_results=allocation_limit)
                    if not allocations:
                        self._record_issue(issue_counts, [ConstraintIssue("distinct_rooms_unavailable", f"Not enough distinct lab rooms for division {request.division_id} parallel lab block")])
                        continue
                    for allocation, total_penalty in allocations:
                        slots = []
                        subjects_in_block = sorted({item.subject_id for item, _ in item_options})
                        duplicate_subject_penalty = (len(item_options) - len(subjects_in_block)) * 6
                        for idx, (item, _) in enumerate(item_options):
                            room_id = allocation[idx]
                            for period in periods:
                                slots.append(
                                    SlotRecord(
                                        division_id=request.division_id,
                                        day=day,
                                        period_id=period,
                                        subject_id=item.subject_id,
                                        teacher_id=item.teacher_id,
                                        room_id=room_id,
                                        slot_type="lab",
                                        session_id=item.session_id,
                                        occupancy_key=f"batch:{item.batch_id}",
                                        batch_id=item.batch_id,
                                    )
                                )
                        balance_penalty = self._division_day_balance_penalty(request.division_id, day, periods, self.state)
                        day_load_penalty = (self._division_day_lab_block_count(request.division_id, day, self.state) * 4) + balance_penalty
                        if start_idx == end_of_day_start_idx and day not in covered_days:
                            coverage_rank = 0
                        elif start_idx == end_of_day_start_idx:
                            coverage_rank = 1
                        elif day not in covered_days:
                            coverage_rank = 2
                        else:
                            coverage_rank = 3
                        combined_penalty = total_penalty + day_load_penalty + duplicate_subject_penalty
                        candidates.append(
                            PlacementCandidate(
                                slots=slots,
                                penalty=combined_penalty,
                                sort_key=(coverage_rank, combined_penalty, day_index, start_idx, tuple(subjects_in_block), tuple(sorted(allocation.values()))),
                            )
                        )
                        if limit is not None and len(candidates) >= limit:
                            candidates.sort(key=lambda candidate: candidate.sort_key)
                            return candidates[:limit], issue_counts
        candidates.sort(key=lambda candidate: candidate.sort_key)
        if limit is not None:
            candidates = candidates[:limit]
        return candidates, issue_counts

    def _request_priority(self, phase: str, request: Any) -> Tuple[Any, ...]:
        if isinstance(request, StandardRequest):
            teacher = self.context.teachers.get(request.teacher_id, {})
            unavailable_count = sum(len(periods) for periods in (teacher.get("unavailable") or {}).values())
            constraint_count = len(
                self.evaluator._matching_constraints(
                    division_id=request.division_id,
                    subject_id=request.subject_id,
                    teacher_id=request.teacher_id,
                    room_id=request.preferred_room_id,
                )
            )
            return (-constraint_count, -unavailable_count, request.division_id, request.subject_id, request.slot_type, request.occurrence_index)
        if isinstance(request, LabBlockRequest):
            teacher_pressure = 0
            remaining = self._remaining_lab_session_counts(request.division_id)
            remaining_subjects = {
                subject_id
                for (batch_id, subject_id), count in remaining.items()
                if batch_id in request.batch_ids and count > 0
            }
            for batch_id in request.batch_ids:
                for subject_id in remaining_subjects:
                    teacher_id = self.context.batch_teachers.get((batch_id, subject_id)) or (self.context.subjects.get(subject_id) or {}).get("lab_teacher_id")
                    teacher = self.context.teachers.get(teacher_id, {})
                    teacher_pressure += sum(len(periods) for periods in (teacher.get("unavailable") or {}).values())
            return (-len(request.batch_ids), -len(remaining_subjects), -teacher_pressure, request.division_id, request.occurrence_index)
        if isinstance(request, ParallelRequest):
            constraint_count = sum(
                len(
                    self.evaluator._matching_constraints(
                        division_id=request.division_id,
                        subject_id=member.subject_id,
                        teacher_id=member.teacher_id,
                    )
                )
                for member in request.members
            )
            return (-len(request.members), -constraint_count, request.division_id, request.group_name, request.occurrence_index)
        return (request.display_name,)

    def _solve_phase(
        self,
        phase: str,
        requests: List[Any],
        candidate_builder,
        *,
        stage_code: str,
        stage_label: str,
        start_percent: float,
        end_percent: float,
    ) -> bool:
        search_limits = self.search_limits
        base_slots = list(self.state.slots)
        ordered_requests = sorted(list(requests), key=lambda request: self._request_priority(phase, request))
        total_requests = len(ordered_requests)
        best_depth = -1
        timed_out = False
        deadline = time.perf_counter() + self.max_phase_seconds if self.max_phase_seconds else None

        def restore_base_state():
            restored = ScheduleState(self.context.period_ids, self.context.days)
            restored.add_slots(base_slots)
            self.state = restored

        def phase_percent(placed_count: int) -> float:
            if total_requests <= 0:
                return end_percent
            progress = placed_count / total_requests
            return start_percent + ((end_percent - start_percent) * progress)

        def backtrack(pending: List[Any], candidate_limit: Optional[int]) -> bool:
            nonlocal best_depth, timed_out
            if deadline and time.perf_counter() >= deadline:
                timed_out = True
                self.failure = GenerationDiagnostic(
                    phase,
                    "search_timeout",
                    f"{stage_label} exceeded the fast search budget after placing {total_requests - len(pending)} of {total_requests}",
                    {
                        "placed": total_requests - len(pending),
                        "total": total_requests,
                        "candidate_limit": candidate_limit,
                    },
                )
                self.diagnostics.append(self.failure)
                return False
            if not pending:
                return True
            placed_count = total_requests - len(pending)
            if placed_count > best_depth:
                best_depth = placed_count
                self._emit_progress(
                    stage_code,
                    f"{stage_label}: placed {placed_count} of {total_requests}",
                    phase_percent(placed_count),
                    phase=phase,
                    placed=placed_count,
                    total=total_requests,
                    candidate_limit=candidate_limit,
                )
            ranked = []
            candidate_window = pending[: min(6, len(pending))]
            for request in candidate_window:
                candidates, issue_counts = candidate_builder(request, candidate_limit)
                if not candidates:
                    return self._fail(phase, request.display_name, self._best_issue(issue_counts), request_id=request.request_id)
                ranked.append((len(candidates), request.display_name, request, candidates))
            ranked.sort(key=lambda row: (row[0], row[1]))
            _, _, request, candidates = ranked[0]
            remaining = [item for item in pending if item is not request]
            for candidate in candidates:
                self.state.add_slots(candidate.slots)
                if backtrack(remaining, candidate_limit):
                    return True
                self.state.remove_slots(candidate.slots)
                if timed_out:
                    return False
            return self._fail(phase, request.display_name, ConstraintIssue("dead_end", f"All candidate placements for {request.display_name} lead to a dead end"), request_id=request.request_id)

        diagnostics_start = len(self.diagnostics)
        if total_requests == 0:
            self._emit_progress(stage_code, f"{stage_label}: no sessions required", end_percent, phase=phase, placed=0, total=0)
            return True
        self._emit_progress(stage_code, f"{stage_label}: starting", start_percent, phase=phase, placed=0, total=total_requests)
        phase_limits = search_limits.get(phase, [24, None])
        for index, candidate_limit in enumerate(phase_limits):
            restore_base_state()
            self.failure = None
            del self.diagnostics[diagnostics_start:]
            self._emit_progress(
                stage_code,
                f"{stage_label}: exploring candidates"
                + (f" (limit {candidate_limit})" if candidate_limit is not None else " (full search)"),
                phase_percent(max(best_depth, 0)),
                phase=phase,
                placed=max(best_depth, 0),
                total=total_requests,
                candidate_limit=candidate_limit,
            )
            if backtrack(list(ordered_requests), candidate_limit):
                self._emit_progress(
                    stage_code,
                    f"{stage_label}: completed",
                    end_percent,
                    phase=phase,
                    placed=total_requests,
                    total=total_requests,
                    candidate_limit=candidate_limit,
                )
                return True
            if timed_out:
                self._emit_progress(
                    stage_code,
                    f"{stage_label}: fast search timed out, switching to draft mode",
                    phase_percent(max(best_depth, 0)),
                    phase=phase,
                    placed=max(best_depth, 0),
                    total=total_requests,
                    candidate_limit=candidate_limit,
                )
                return False
            if index == len(phase_limits) - 1:
                return False
        return False

    def _solve_phase_partial(
        self,
        phase: str,
        requests: List[Any],
        candidate_builder,
        *,
        stage_code: str,
        stage_label: str,
        start_percent: float,
        end_percent: float,
    ) -> None:
        ordered_requests = sorted(list(requests), key=lambda request: self._request_priority(phase, request))
        total_requests = len(ordered_requests)
        if total_requests == 0:
            self._emit_progress(stage_code, f"{stage_label}: no sessions required", end_percent, phase=phase, placed=0, total=0)
            return
        self._emit_progress(stage_code, f"{stage_label}: drafting partial schedule", start_percent, phase=phase, placed=0, total=total_requests)
        span = max(end_percent - start_percent, 1)
        for index, request in enumerate(ordered_requests, start=1):
            candidates, issue_counts = candidate_builder(request, 24)
            placed = False
            last_issue = self._best_issue(issue_counts)
            for candidate in candidates:
                self.state.add_slots(candidate.slots)
                validation_issues = self.evaluator.validate_state(self.state)
                if not validation_issues:
                    placed = True
                    break
                self.state.remove_slots(candidate.slots)
                last_issue = ConstraintIssue(
                    validation_issues[0].code,
                    validation_issues[0].message,
                    validation_issues[0].entities,
                )
            if not placed:
                self._record_unscheduled_request(phase, request, last_issue)
            progress = start_percent + (span * (index / total_requests))
            self._emit_progress(
                stage_code,
                f"{stage_label}: processed {index} of {total_requests}",
                progress,
                phase=phase,
                placed=index - (len([item for item in self.unscheduled if item["phase"] == phase])),
                total=total_requests,
                unscheduled=len([item for item in self.unscheduled if item["phase"] == phase]),
            )

    def solve(self) -> bool:
        self._emit_progress("validating_inputs", "Validating teachers, rooms, subjects, and constraints", 5)
        input_errors = self.validate_inputs()
        if input_errors:
            self.failure = input_errors[0]
            self.diagnostics.extend(input_errors)
            return False
        self._emit_progress("planning_requests", "Preparing deterministic scheduling requests", 12)
        parallel_requests, consumed_parallel = self._parallel_plans()
        standard_requests = self._build_standard_requests(consumed_parallel)
        lab_requests = self._build_lab_requests()
        self._emit_progress(
            "planning_requests",
            "Prepared lecture, lab, and parallel request queues",
            18,
            lecture_requests=len(standard_requests),
            lab_requests=len(lab_requests),
            parallel_requests=len(parallel_requests),
        )

        if not self._solve_phase(
            "labs",
            lab_requests,
            self._lab_candidates,
            stage_code="scheduling_labs",
            stage_label="Scheduling grouped labs",
            start_percent=18,
            end_percent=46,
        ):
            return False
        if not self._solve_phase(
            "parallel",
            parallel_requests,
            self._parallel_candidates,
            stage_code="scheduling_parallel",
            stage_label="Scheduling parallel groups",
            start_percent=46,
            end_percent=64,
        ):
            return False
        if not self._solve_phase(
            "lectures",
            standard_requests,
            self._standard_candidates,
            stage_code="scheduling_lectures",
            stage_label="Scheduling lectures and tutorials",
            start_percent=64,
            end_percent=92,
        ):
            return False

        self._emit_progress("validating_timetable", "Validating final timetable consistency", 94)
        issues = self.evaluator.validate_state(self.state)
        if issues:
            self.failure = issues[0]
            self.diagnostics.extend(issues)
            return False
        self._emit_progress("coverage_validation", "Checking subject and lab coverage", 97)
        coverage_ok, coverage_report = self.validate_required_coverage()
        self.coverage_report = coverage_report
        if not coverage_ok:
            gap = coverage_report["gaps"][0]
            self.failure = GenerationDiagnostic("coverage", "coverage_shortfall", gap["message"], gap)
            self.diagnostics.append(self.failure)
            return False
        end_lab_issue = self.evaluator.validate_end_of_day_lab_coverage(self.state)
        if end_lab_issue:
            self.failure = end_lab_issue
            self.diagnostics.append(end_lab_issue)
            return False

        self.assignments = [slot for slot in self.state.slots if slot.slot_type != "lab"]
        self.lab_assignments = [slot for slot in self.state.slots if slot.slot_type == "lab"]
        self._emit_progress(
            "generation_complete",
            "Timetable generation completed successfully",
            99,
            lecture_slots=len(self.assignments),
            lab_slots=len(self.lab_assignments),
        )
        return True

    def solve_partial(self) -> bool:
        self.partial = True
        self.unscheduled = []
        fatal_input_codes = {"teachers_missing", "lecture_rooms_missing", "subjects_missing", "divisions_missing", "division_subjects_missing"}
        input_errors = self.validate_inputs()
        fatal_errors = [issue for issue in input_errors if issue.code in fatal_input_codes]
        if fatal_errors:
            self.failure = fatal_errors[0]
            self.diagnostics.extend(input_errors)
            return False
        self.diagnostics.extend(input_errors)
        self._emit_progress("partial_mode", "Strict generation failed; building the best valid draft timetable", 8)
        parallel_requests, consumed_parallel = self._parallel_plans()
        standard_requests = self._build_standard_requests(consumed_parallel)
        lab_requests = self._build_lab_requests()
        self._solve_phase_partial(
            "labs",
            lab_requests,
            self._lab_candidates,
            stage_code="scheduling_labs",
            stage_label="Scheduling grouped labs",
            start_percent=12,
            end_percent=44,
        )
        self._solve_phase_partial(
            "parallel",
            parallel_requests,
            self._parallel_candidates,
            stage_code="scheduling_parallel",
            stage_label="Scheduling parallel groups",
            start_percent=44,
            end_percent=62,
        )
        self._solve_phase_partial(
            "lectures",
            standard_requests,
            self._standard_candidates,
            stage_code="scheduling_lectures",
            stage_label="Scheduling lectures and tutorials",
            start_percent=62,
            end_percent=90,
        )
        issues = self.evaluator.validate_state(self.state)
        if issues:
            self.failure = issues[0]
            self.diagnostics.extend(issues)
            return False
        _, coverage_report = self.validate_required_coverage()
        self.coverage_report = coverage_report
        self.assignments = [slot for slot in self.state.slots if slot.slot_type != "lab"]
        self.lab_assignments = [slot for slot in self.state.slots if slot.slot_type == "lab"]
        self._emit_progress(
            "generation_complete",
            "Partial timetable draft completed",
            98,
            lecture_slots=len(self.assignments),
            lab_slots=len(self.lab_assignments),
            unscheduled=len(self.unscheduled),
        )
        return True

    def build_docs(self) -> List[Dict[str, Any]]:
        docs = []
        for slot in sorted(self.state.slots, key=lambda row: (row.division_id, row.day, self.context.period_ids.index(row.period_id), row.subject_id, row.batch_id or "", row.session_id)):
            slot.int_id = next_seq(self.db, "timetable_slots")
            docs.append(slot.to_doc())
        return docs

    def validate_persistable_docs(self, docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        conflicts = []
        seen_occupancy = {}
        for doc in docs:
            occupancy_key = (doc["division_id"], doc["day"], doc["period_id"], doc.get("occupancy_key"))
            existing = seen_occupancy.get(occupancy_key)
            if existing is not None:
                conflicts.append({
                    "type": "division_occupancy_duplicate",
                    "division_id": doc["division_id"],
                    "day": doc["day"],
                    "period_id": doc["period_id"],
                    "occupancy_key": doc.get("occupancy_key"),
                    "subject_ids": sorted({existing["subject_id"], doc["subject_id"]}),
                })
            else:
                seen_occupancy[occupancy_key] = doc
        return conflicts

    def schedule_mdm_parallel(self) -> Tuple[bool, List[str]]:
        return True, []

    def schedule_labs(self) -> Tuple[bool, List[str]]:
        return True, []

    def validate_required_coverage(self) -> Tuple[bool, Dict[str, Any]]:
        lecture_rows = []
        lab_rows = []
        gaps = []
        lecture_actual: Dict[Tuple[str, str, str], int] = defaultdict(int)
        seen_sessions = set()
        for slot in self.state.slots:
            if slot.slot_type not in {"lecture", "tutorial"} or slot.session_id in seen_sessions:
                continue
            seen_sessions.add(slot.session_id)
            lecture_actual[(slot.division_id, slot.subject_id, slot.slot_type)] += 1
        for division_id, subject_ids in sorted(self.context.division_subjects.items()):
            for subject_id in sorted(subject_ids):
                subject = self.context.subjects.get(subject_id)
                if not subject:
                    continue
                for slot_type, required in [
                    ("lecture", int(subject.get("lectures_per_week", 0) or 0)),
                    ("tutorial", int(subject.get("tutorials_per_week", 0) or 0) if subject.get("has_tutorial") else 0),
                ]:
                    if slot_type == "tutorial" and not subject.get("has_tutorial"):
                        continue
                    actual = lecture_actual.get((division_id, subject_id, slot_type), 0)
                    lecture_rows.append({"division_id": division_id, "subject_id": subject_id, "slot_type": slot_type, "required": required, "actual": actual})
                    if actual != required:
                        gaps.append({"division_id": division_id, "subject_id": subject_id, "slot_type": slot_type, "required": required, "actual": actual, "message": f"{slot_type.title()} coverage mismatch for {division_id}/{subject_id}: {actual}/{required}"})

        lab_actual: Dict[Tuple[str, str, str], int] = defaultdict(int)
        for slot in self.state.slots:
            if slot.slot_type == "lab" and slot.batch_id:
                lab_actual[(slot.division_id, slot.subject_id, slot.batch_id)] += 1
        for division_id, subject_ids in sorted(self.context.division_subjects.items()):
            for subject_id in sorted(subject_ids):
                subject = self.context.subjects.get(subject_id)
                if not subject or not subject.get("has_lab"):
                    continue
                required = int(subject.get("lab_hours_per_week", 0) or 0)
                for batch in self.context.batches.get(division_id, []):
                    actual = lab_actual.get((division_id, subject_id, batch["id"]), 0)
                    lab_rows.append({"division_id": division_id, "subject_id": subject_id, "batch_id": batch["id"], "required_hours": required, "actual_hours": actual})
                    if actual != required:
                        gaps.append({"division_id": division_id, "subject_id": subject_id, "batch_id": batch["id"], "required": required, "actual": actual, "message": f"Lab coverage mismatch for {division_id}/{subject_id}/{batch['id']}: {actual}/{required}"})
        return not gaps, {"lecture_tutorial": lecture_rows, "lab_hours": lab_rows, "gaps": gaps}

    def save_to_db(self) -> int:
        docs = self.build_docs()
        conflicts = self.validate_persistable_docs(docs)
        if conflicts:
            sample = conflicts[0]
            raise ValueError(
                f"Cannot persist timetable because duplicate occupancy was detected for "
                f"{sample['division_id']} {sample['day']} {sample['period_id']} ({sample['occupancy_key']})"
            )
        return replace_active_timetable(self.db, docs)


def load_active_slot_records(db=None, context: Optional[SchedulingContext] = None) -> List[SlotRecord]:
    if db is None:
        db = get_db()
    context = context or SchedulingContext.from_db(db)
    rows = db.timetable_slots.find(active_timetable_filter(db), {"_id": 0})
    records = []
    for row in rows:
        records.append(
            SlotRecord(
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
        )
    return records


def build_state_from_active_timetable(db=None, context: Optional[SchedulingContext] = None) -> ScheduleState:
    resolved_db = db if db is not None else get_db()
    context = context or SchedulingContext.from_db(resolved_db)
    state = ScheduleState(context.period_ids, context.days)
    state.add_slots(load_active_slot_records(resolved_db, context))
    return state


def validate_active_timetable(db=None) -> List[GenerationDiagnostic]:
    if db is None:
        db = get_db()
    context = SchedulingContext.from_db(db)
    state = build_state_from_active_timetable(db, context)
    evaluator = ConstraintEvaluator(context)
    return evaluator.validate_state(state)
