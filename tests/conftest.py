import os
from pathlib import Path

# Must run before `app.main` is imported anywhere, since its module-level
# db.init_db() call reads db.DB_PATH at import time.
TEST_DB = Path(__file__).parent / "_test.db"
TEST_DB.unlink(missing_ok=True)
os.environ["APP_DB_PATH"] = str(TEST_DB)

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
