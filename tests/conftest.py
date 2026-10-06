from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

ADMIN_URL = os.environ.get("TEST_POSTGRES_ADMIN_URL")


@dataclass
class ScratchDb:
    owner: Engine  # superuser connection to the scratch database
    app_url: str  # URL for the restricted application role
    app_role: str


@pytest.fixture
def scratch_db() -> Iterator[ScratchDb]:
    """A throwaway database plus a restricted role, created and dropped around each test.
    Needs TEST_POSTGRES_ADMIN_URL (a superuser URL, e.g. the compose Postgres); otherwise the test is skipped."""
    if not ADMIN_URL:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL (docker compose up -d postgres) to run Postgres tests")
    suffix = uuid.uuid4().hex[:10]
    db, role, password = f"onb_test_{suffix}", f"onb_app_{suffix}", f"pw-{suffix}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{db}"'))
        c.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"))
    url = make_url(ADMIN_URL)
    owner = create_engine(url.set(database=db))
    app_url = url.set(database=db, username=role, password=password).render_as_string(hide_password=False)
    try:
        yield ScratchDb(owner=owner, app_url=app_url, app_role=role)
    finally:
        owner.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
            c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()
