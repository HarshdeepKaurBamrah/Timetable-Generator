import os
import sys
from pathlib import Path

import mongomock
import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database  # noqa: E402


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    test_db = client["timetable_test_db"]
    database.set_db_for_tests(test_db)
    database.init_db()
    yield test_db
    database.set_db_for_tests(client["cleanup"])


@pytest.fixture
def client(db):
    import app as app_module  # noqa: E402

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as flask_client:
        yield flask_client
