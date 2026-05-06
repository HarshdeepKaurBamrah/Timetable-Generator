"""
database.py - MongoDB helpers, startup validation, and timetable versioning.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import ServerSelectionTimeoutError

from constraints import normalize_constraint_document

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "timetable_db")

_client = None
_db = None
_startup_report: Dict[str, Any] = {}


class DatabaseConnectionError(RuntimeError):
    pass


def _build_client():
    uri = os.environ.get("MONGO_URI", MONGO_URI)
    if uri.startswith("mongomock://") or os.environ.get("MONGO_USE_MOCK") == "1":
        import mongomock

        return mongomock.MongoClient()
    return MongoClient(uri, serverSelectionTimeoutMS=5000)


def reset_db_connection():
    global _client, _db
    _client = None
    _db = None


def set_db_for_tests(db):
    global _client, _db
    _client = None
    _db = db


def get_db():
    global _client, _db
    if _db is None:
        _client = _build_client()
        _db = _client[os.environ.get("MONGO_DB", MONGO_DB)]
    return _db


def _windows_mongo_service_hint() -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["sc.exe", "query", "MongoDB"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    upper = output.upper()
    if "FAILED 1060" in upper or "DOES NOT EXIST" in upper:
        return "MongoDB service is not installed as a Windows service on this machine."
    if "STATE" in upper and "STOPPED" in upper:
        return (
            "MongoDB Windows service is installed but stopped. Start 'MongoDB Server (MongoDB)' "
            "from Services, or run an Administrator PowerShell and execute: Start-Service MongoDB"
        )
    if "STATE" in upper and "RUNNING" in upper:
        return "MongoDB Windows service appears to be running, but the app still cannot reach it on the configured URI."
    return None


def verify_db_connection(db=None):
    db = db if db is not None else get_db()
    if os.environ.get("MONGO_USE_MOCK") == "1" or os.environ.get("MONGO_URI", MONGO_URI).startswith("mongomock://"):
        return db
    try:
        db.client.admin.command("ping")
    except ServerSelectionTimeoutError as exc:
        uri = os.environ.get("MONGO_URI", MONGO_URI)
        lines = [f"Could not connect to MongoDB at {uri}."]
        hint = _windows_mongo_service_hint()
        if hint:
            lines.append(hint)
        lines.append("Start MongoDB and retry.")
        lines.append("For a temporary in-memory dev database, run PowerShell with: $env:MONGO_USE_MOCK='1'; python run.py")
        raise DatabaseConnectionError("\n".join(lines)) from exc
    return db


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def next_seq(db, name: str) -> int:
    result = db.sequences.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(result["seq"])


def get_config_value(db, key: str, default=None):
    row = db.config.find_one({"key": key})
    if not row:
        return default
    value = row.get("value")
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def set_config_value(db, key: str, value: Any):
    encoded = value if isinstance(value, str) else json.dumps(value)
    db.config.update_one({"key": key}, {"$set": {"value": encoded}}, upsert=True)


def get_active_timetable_version(db=None) -> Optional[int]:
    if db is None:
        db = get_db()
    value = get_config_value(db, "active_timetable_version")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def set_active_timetable_version(db, version_id: int):
    set_config_value(db, "active_timetable_version", int(version_id))


def active_timetable_filter(db=None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if db is None:
        db = get_db()
    flt = dict(extra or {})
    active_version = get_active_timetable_version(db)
    if active_version is not None:
        flt["version_id"] = active_version
    return flt


def replace_active_timetable(db, docs: List[Dict[str, Any]]) -> int:
    new_version = next_seq(db, "timetable_versions")
    if docs:
        prepared = []
        for doc in docs:
            cloned = dict(doc)
            cloned["version_id"] = new_version
            prepared.append(cloned)
        db.timetable_slots.insert_many(prepared, ordered=True)
    set_active_timetable_version(db, new_version)
    db.timetable_slots.delete_many({"version_id": {"$ne": new_version}})
    return new_version


def clear_active_timetable(db) -> int:
    return replace_active_timetable(db, [])


def get_startup_report() -> Dict[str, Any]:
    return dict(_startup_report)


def _ensure_indexes(db):
    allowed_timetable_indexes = {"_id_", "uniq_timetable_occupancy", "idx_timetable_teacher", "idx_timetable_room"}
    for name in list(db.timetable_slots.index_information().keys()):
        if name not in allowed_timetable_indexes:
            db.timetable_slots.drop_index(name)
    db.users.create_index("email", unique=True, sparse=True)
    db.teachers.create_index("id", unique=True)
    db.rooms.create_index("id", unique=True)
    db.subjects.create_index("id", unique=True)
    db.divisions.create_index("id", unique=True)
    db.batches.create_index("id", unique=True)
    db.batch_teachers.create_index(
        [("batch_id", ASCENDING), ("subject_id", ASCENDING)], unique=True
    )
    db.timetable_slots.create_index(
        [
            ("version_id", ASCENDING),
            ("division_id", ASCENDING),
            ("day", ASCENDING),
            ("period_id", ASCENDING),
            ("occupancy_key", ASCENDING),
        ],
        unique=True,
        name="uniq_timetable_occupancy",
    )
    db.timetable_slots.create_index(
        [("version_id", ASCENDING), ("teacher_id", ASCENDING), ("day", ASCENDING), ("period_id", ASCENDING)],
        name="idx_timetable_teacher",
    )
    db.timetable_slots.create_index(
        [("version_id", ASCENDING), ("room_id", ASCENDING), ("day", ASCENDING), ("period_id", ASCENDING)],
        name="idx_timetable_room",
    )
    db.config.create_index("key", unique=True)
    db.user_constraints.create_index("int_id", unique=True, sparse=True)
    db.user_constraints.create_index("constraint_type")
    db.auth_tokens.create_index("token", unique=True, sparse=True)
    db.auth_tokens.create_index([("user_int_id", ASCENDING), ("created_at", ASCENDING)])


def _ensure_defaults(db):
    defaults = [
        ("period_ids", ["P1", "P2", "P3", "P4", "P5", "P6"]),
        (
            "period_labels",
            {
                "P1": "8:00-9:00",
                "P2": "9:00-10:00",
                "P3": "10:15-11:15",
                "P4": "11:15-12:15",
                "P5": "13:00-14:00",
                "P6": "14:00-15:00",
            },
        ),
        ("days", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]),
        ("department_name", "Department of Computer Engineering"),
        ("academic_year", "AY 2024-25"),
        ("semester", "Semester II"),
        ("break_after_periods", ["P2", "P4"]),
        ("timetable_orientation", "days_horizontal"),
    ]
    for key, value in defaults:
        db.config.update_one(
            {"key": key},
            {"$setOnInsert": {"key": key, "value": json.dumps(value) if not isinstance(value, str) else value}},
            upsert=True,
        )

    if db.users.count_documents({"role": "coordinator"}) == 0:
        uid = next_seq(db, "users")
        db.users.insert_one(
            {
                "int_id": uid,
                "email": "admin@college.edu",
                "password": hash_pw("admin123"),
                "role": "coordinator",
                "name": "Timetable Coordinator",
                "teacher_id": None,
            }
        )


def _repair_teacher_limits(db) -> Dict[str, Any]:
    report = {"updated": 0, "teachers": []}
    for teacher in db.teachers.find({}, {"_id": 0, "id": 1, "name": 1, "max_hrs_per_day": 1, "max_hrs_per_week": 1}):
        day_limit = int(teacher.get("max_hrs_per_day", 4) or 4)
        week_limit = int(teacher.get("max_hrs_per_week", 18) or 18)
        repaired = {}
        if day_limit <= 0:
            repaired["max_hrs_per_day"] = 4
        if week_limit <= 0:
            repaired["max_hrs_per_week"] = 18
        if repaired:
            db.teachers.update_one({"id": teacher["id"]}, {"$set": repaired})
            report["updated"] += 1
            report["teachers"].append(teacher["name"])
    return report


def _repair_user_teacher_links(db) -> Dict[str, Any]:
    report = {"linked": 0, "created_users": 0}
    for teacher in db.teachers.find({}, {"_id": 0}):
        email = str(teacher.get("email") or "").strip().lower()
        if not email:
            continue
        user = db.users.find_one({"email": email})
        if user:
            if user.get("teacher_id") != teacher["id"]:
                db.users.update_one({"email": email}, {"$set": {"teacher_id": teacher["id"], "name": teacher["name"]}})
                report["linked"] += 1
            continue
        uid = next_seq(db, "users")
        db.users.insert_one(
            {
                "int_id": uid,
                "email": email,
                "password": hash_pw("faculty123"),
                "role": "faculty",
                "name": teacher["name"],
                "teacher_id": teacher["id"],
            }
        )
        report["created_users"] += 1
    return report


def _repair_constraints(db) -> Dict[str, Any]:
    report = {"normalized": 0, "deactivated": 0, "errors": []}
    for row in db.user_constraints.find({}, {"_id": 0}):
        normalized, errors = normalize_constraint_document(row, strict=True)
        if normalized is None:
            db.user_constraints.update_one(
                {"int_id": row.get("int_id")},
                {
                    "$set": {
                        "active": 0,
                        "updated_at": row.get("updated_at"),
                        "normalization_errors": errors,
                    }
                },
            )
            report["deactivated"] += 1
            report["errors"].append({"int_id": row.get("int_id"), "errors": errors})
            continue
        if errors:
            normalized["normalization_errors"] = errors
        db.user_constraints.update_one({"int_id": row.get("int_id")}, {"$set": normalized}, upsert=True)
        report["normalized"] += 1
    return report


def _repair_timetable_slots(db) -> Dict[str, Any]:
    report = {"versioned": 0, "updated_slots": 0}
    period_ids = get_config_value(db, "period_ids", ["P1", "P2", "P3", "P4", "P5", "P6"])
    period_index = {pid: idx for idx, pid in enumerate(period_ids)}
    active_version = get_active_timetable_version(db)
    if db.timetable_slots.count_documents({}) and active_version is None:
        active_version = next_seq(db, "timetable_versions")
        db.timetable_slots.update_many({"version_id": {"$exists": False}}, {"$set": {"version_id": active_version}})
        set_active_timetable_version(db, active_version)
        report["versioned"] = db.timetable_slots.count_documents({"version_id": active_version})

    if active_version is None:
        return report

    docs = list(db.timetable_slots.find({"version_id": active_version}, {"_id": 0}))
    by_division_period: Dict[tuple, List[Dict[str, Any]]] = {}
    for doc in docs:
        by_division_period.setdefault((doc["division_id"], doc["day"], doc["period_id"]), []).append(doc)

    lab_groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for doc in docs:
        if doc.get("slot_type") == "lab":
            key = (
                doc["division_id"],
                doc.get("subject_id"),
                doc.get("batch_id"),
                doc.get("day"),
                doc.get("teacher_id"),
                doc.get("room_id"),
            )
            lab_groups.setdefault(key, []).append(doc)

    lab_session_ids: Dict[int, str] = {}
    for key, items in lab_groups.items():
        ordered = sorted(items, key=lambda item: period_index.get(item.get("period_id"), 999))
        used = set()
        for idx, item in enumerate(ordered):
            if item["int_id"] in used:
                continue
            current_idx = period_index.get(item.get("period_id"), -999)
            partner = None
            for other in ordered[idx + 1 :]:
                other_idx = period_index.get(other.get("period_id"), -999)
                if other["int_id"] in used:
                    continue
                if other_idx == current_idx + 1:
                    partner = other
                    break
            token = f"LAB:{key[0]}:{key[1]}:{key[2]}:{key[3]}:{current_idx}"
            lab_session_ids[item["int_id"]] = token
            used.add(item["int_id"])
            if partner is not None:
                lab_session_ids[partner["int_id"]] = token
                used.add(partner["int_id"])

    for doc in docs:
        updates = {}
        if "version_id" not in doc:
            updates["version_id"] = active_version
        if not doc.get("session_id"):
            if doc.get("slot_type") == "lab":
                updates["session_id"] = lab_session_ids.get(doc["int_id"], f"LAB-LEGACY:{doc['int_id']}")
            else:
                updates["session_id"] = f"SLOT:{doc['int_id']}"
        if not doc.get("occupancy_key"):
            if doc.get("batch_id"):
                updates["occupancy_key"] = f"batch:{doc['batch_id']}"
            elif len(by_division_period.get((doc["division_id"], doc["day"], doc["period_id"]), [])) > 1:
                updates["parallel_group_id"] = doc.get("parallel_group_id") or (
                    f"legacy-parallel:{doc['division_id']}:{doc['day']}:{doc['period_id']}"
                )
                updates["occupancy_key"] = f"parallel:{doc['subject_id']}:{doc['int_id']}"
            else:
                updates["occupancy_key"] = "division"
        if "is_locked" not in doc:
            updates["is_locked"] = 0
        if not doc.get("status"):
            updates["status"] = "active"
        if not doc.get("source"):
            updates["source"] = "legacy"
        if updates:
            db.timetable_slots.update_one({"int_id": doc["int_id"]}, {"$set": updates})
            report["updated_slots"] += 1
    return report


def run_startup_validation(db=None) -> Dict[str, Any]:
    if db is None:
        db = get_db()
    report = {
        "teacher_limits": _repair_teacher_limits(db),
        "teacher_links": _repair_user_teacher_links(db),
        "constraints": _repair_constraints(db),
        "timetable_slots": _repair_timetable_slots(db),
    }
    global _startup_report
    _startup_report = report
    return report


def init_db():
    db = verify_db_connection(get_db())
    _ensure_defaults(db)
    run_startup_validation(db)
    _ensure_indexes(db)
    return db
