#!/usr/bin/env python3
"""Run safe data repairs and schema normalization for the timetable database."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from database import init_db, run_startup_validation, get_db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Normalize timetable MongoDB collections")
    parser.add_argument("--mongo", default="mongodb://localhost:27017", help="MongoDB URI")
    parser.add_argument("--db", default="timetable_db", help="MongoDB database name")
    parser.add_argument("--json", action="store_true", help="Print the migration report as JSON")
    args = parser.parse_args()

    os.environ["MONGO_URI"] = args.mongo
    os.environ["MONGO_DB"] = args.db
    init_db()
    report = run_startup_validation(get_db())
    if args.json:
        print(json.dumps(report, indent=2))
        return

    print("Migration completed.")
    for key, value in report.items():
        print(f"- {key}: {value}")


if __name__ == "__main__":
    main()
