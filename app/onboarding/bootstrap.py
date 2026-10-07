"""Build a fully wired CaseService from settings (what the API and the CLI commands use)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from sqlalchemy import create_engine

from onboarding.audit.postgres import PostgresAuditLog
from onboarding.config import Settings
from onboarding.db import postgres_checkpointer
from onboarding.graph.nodes import Deps
from onboarding.llm.client import BedrockLlm, FakeLlm, LlmClient
from onboarding.llm.prompts import LocalPromptStore, PromptStore
from onboarding.llm.service import LlmService
from onboarding.locks import PostgresAdvisoryLocks
from onboarding.observability import NoopTracer, Tracer
from onboarding.paths import PROMPTS_DIR, SANCTIONS_DIR
from onboarding.rules.reference import Reference
from onboarding.screening.scorer import ScreeningConfig
from onboarding.screening.unlist import SanctionsIndex
from onboarding.service import CaseService
from onboarding.store import CaseStore
from onboarding.tools.bank import BankClient
from onboarding.tools.buffer import DocumentBuffer
from onboarding.tools.kyc import KycClient


def build_llm(s: Settings) -> LlmClient | None:
    if not s.llm_enabled:
        return None
    if s.llm_backend == "bedrock":
        return BedrockLlm(s.bedrock_model_id, s.aws_region, max_attempts=s.llm_retry_attempts)
    return FakeLlm()


def build_prompts(s: Settings) -> tuple[PromptStore, Tracer]:
    local = LocalPromptStore(PROMPTS_DIR)
    if not s.tracing_enabled:
        return local, NoopTracer()
    from onboarding.langfuse_tracing import LangfusePromptStore, LangfuseTracer, make_client

    client = make_client(s)
    registry: dict[str, object] = {}
    return LangfusePromptStore(client, s.prompt_label, local, registry=registry), LangfuseTracer(
        client, s.environment, registry
    )


@contextmanager
def build_service_from_env(settings: Settings | None = None) -> Iterator[CaseService]:
    s = settings or Settings.from_env()
    if not s.database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_engine(s.database_url, pool_pre_ping=True)
    prompts, tracer = build_prompts(s)
    audit = PostgresAuditLog(engine)
    deps = Deps(
        audit=audit,
        kyc=KycClient(client=httpx.Client(base_url=s.kyc_base_url, timeout=30.0), api_key=s.kyc_api_key),
        index=SanctionsIndex.load(SANCTIONS_DIR),
        screening_cfg=ScreeningConfig.load(),
        reference=Reference.load(),
        llm=LlmService(
            build_llm(s), prompts, audit, enabled=s.llm_enabled, annotate_hits=s.annotate_hits, tracer=tracer
        ),
        buffer=DocumentBuffer(),
        bank=BankClient(client=httpx.Client(base_url=s.mock_bank_url, timeout=15.0)),
        max_info_rounds=s.max_info_rounds,
        tracer=tracer,
    )
    with postgres_checkpointer(s.database_url) as saver:
        store = CaseStore(engine)
        yield CaseService(deps, store, saver, PostgresAdvisoryLocks(engine), s.max_info_rounds)
    engine.dispose()
