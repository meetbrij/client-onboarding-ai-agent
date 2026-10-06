"""Mock core-banking service: creates a customer record, idempotent on `Idempotency-Key`.

This stands in for an external system, so it must not import anything from `onboarding`
(tests/test_mock_bank_boundary.py enforces that). It never logs customer details, only the
idempotency key (the case id) and the customer id.

Contract
- POST /customers  needs an `Idempotency-Key` header.
  - first use of a key: 201 and the new customer.
  - same key, same payload: 200, the stored customer, header `Idempotent-Replayed: true`.
  - same key, different payload: 409.
- GET /customers/{customer_id}, GET /healthz.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

log = logging.getLogger("mock_bank")

metadata = MetaData()
customers = Table(
    "customers",
    metadata,
    Column("customer_id", String(32), primary_key=True),
    Column("idempotency_key", String(128), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False),
    Column("request_json", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
)


class CustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    date_of_birth: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    nationality: str = Field(min_length=1, max_length=100)
    residence_country: str = Field(min_length=1, max_length=100)
    occupation: str = Field(min_length=1, max_length=200)
    risk_rating: str = Field(pattern=r"^(low|medium|high)$")


class CustomerOut(CustomerRequest):
    customer_id: str
    created_at: str


def _hash(req: CustomerRequest) -> str:
    canonical = json.dumps(req.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _out(row: Any) -> CustomerOut:
    return CustomerOut(
        customer_id=row.customer_id,
        created_at=row.created_at.isoformat() + "Z",
        **json.loads(row.request_json),
    )


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def wait_for_db(engine: Engine, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.0)


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        eng = engine
        if eng is None:
            eng = make_engine(os.environ["MOCK_BANK_DATABASE_URL"])
            wait_for_db(eng)
        metadata.create_all(eng)
        app.state.engine = eng
        yield

    app = FastAPI(title="Mock core banking (SPECIMEN)", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz(response: Response) -> dict[str, str]:
        try:
            with app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except OperationalError:
            response.status_code = 503
            return {"status": "database unavailable"}
        return {"status": "ok"}

    @app.post("/customers", response_model=CustomerOut, status_code=201)
    def create_customer(
        body: CustomerRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CustomerOut:
        if not idempotency_key or len(idempotency_key) > 128:
            raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        request_hash = _hash(body)
        eng: Engine = app.state.engine

        def existing() -> CustomerOut | None:
            with eng.connect() as conn:
                row = conn.execute(
                    select(customers).where(customers.c.idempotency_key == idempotency_key)
                ).first()
            if row is None:
                return None
            if row.request_hash != request_hash:
                raise HTTPException(
                    status_code=409, detail="Idempotency-Key was used with a different payload"
                )
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
            return _out(row)

        found = existing()
        if found is not None:
            return found

        customer_id = "CUST-" + uuid.uuid4().hex[:12].upper()
        now = datetime.now(UTC).replace(tzinfo=None)
        try:
            with eng.begin() as conn:
                conn.execute(
                    customers.insert().values(
                        customer_id=customer_id,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        request_json=json.dumps(body.model_dump(), sort_keys=True),
                        created_at=now,
                    )
                )
        except IntegrityError:
            # A concurrent request with the same key won the insert; answer from its row.
            found = existing()
            if found is None:  # pragma: no cover - the unique violation implies the row exists
                raise
            return found
        log.info("customer created", extra={"idempotency_key": idempotency_key, "customer_id": customer_id})
        return CustomerOut(customer_id=customer_id, created_at=now.isoformat() + "Z", **body.model_dump())

    @app.get("/customers/{customer_id}", response_model=CustomerOut)
    def get_customer(customer_id: str) -> CustomerOut:
        with app.state.engine.connect() as conn:
            row = conn.execute(select(customers).where(customers.c.customer_id == customer_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="customer not found")
        return _out(row)

    return app


def get_app() -> FastAPI:
    """`uvicorn mock_bank.main:get_app --factory`."""
    return create_app()
