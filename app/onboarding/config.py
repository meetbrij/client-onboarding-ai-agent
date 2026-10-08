"""Configuration from environment variables only (no config files, no secrets in the image)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _bool(value: str | None, default: bool) -> bool:
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str = "dev"  # dev | test | qa | prod
    database_url: str = ""  # the application role, never the owner
    kyc_base_url: str = "http://localhost:8002"
    kyc_api_key: str | None = None
    mock_bank_url: str = "http://localhost:8001"
    llm_backend: str = "fake"  # fake | bedrock | anthropic
    llm_enabled: bool = True  # the kill switch: false uses templates only
    annotate_hits: bool = True
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_api_key: str | None = None
    bedrock_model_id: str = ""  # a model id or a cross-region inference profile id
    aws_region: str = "ap-south-1"
    prompt_label: str = "production"  # Langfuse prompt label: production | staging
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    max_info_rounds: int = 2
    llm_retry_attempts: int = 5  # Bedrock attempts per call before falling back to templates
    tokens_json: str = ""  # [{"id": "...", "role": "officer|submitter", "sha256": "<hex of the token>"}]
    session_secret: str = ""
    doc_max_bytes: int = 10 * 1024 * 1024
    checkpoint_retention_days: int = 30
    document_store: str = "none"  # none | local | s3 (qa and prod require s3)
    document_dir: str = "/data/documents"  # local store only
    document_bucket: str = ""
    document_prefix: str = ""
    document_kms_key_id: str = ""  # empty: AES-256 server-side encryption
    secure_cookies: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        s = cls(
            environment=e.get("ENVIRONMENT", "dev"),
            database_url=e.get("DATABASE_URL", ""),
            kyc_base_url=e.get("KYC_BASE_URL", "http://localhost:8002"),
            kyc_api_key=e.get("KYC_API_KEY") or None,
            mock_bank_url=e.get("MOCK_BANK_URL", "http://localhost:8001"),
            llm_backend=e.get("LLM_BACKEND", "fake"),
            llm_enabled=_bool(e.get("LLM_ENABLED"), True),
            annotate_hits=_bool(e.get("ANNOTATE_HITS"), True),
            anthropic_model=e.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            anthropic_api_key=e.get("ANTHROPIC_API_KEY") or None,
            bedrock_model_id=e.get("BEDROCK_MODEL_ID", ""),
            aws_region=e.get("AWS_REGION", "ap-south-1"),
            prompt_label=e.get("PROMPT_LABEL", "production"),
            langfuse_host=e.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            langfuse_public_key=e.get("LANGFUSE_PUBLIC_KEY", ""),
            langfuse_secret_key=e.get("LANGFUSE_SECRET_KEY", ""),
            max_info_rounds=int(e.get("MAX_INFO_ROUNDS", "2")),
            llm_retry_attempts=int(e.get("LLM_RETRY_ATTEMPTS", "5")),
            tokens_json=e.get("ONBOARDING_TOKENS", ""),
            session_secret=e.get("SESSION_SECRET", ""),
            doc_max_bytes=int(e.get("DOC_MAX_BYTES", str(10 * 1024 * 1024))),
            checkpoint_retention_days=int(e.get("CHECKPOINT_RETENTION_DAYS", "30")),
            document_store=e.get("DOCUMENT_STORE", "none"),
            document_dir=e.get("DOCUMENT_DIR", "/data/documents"),
            document_bucket=e.get("DOCUMENT_BUCKET", ""),
            document_prefix=e.get("DOCUMENT_PREFIX", ""),
            document_kms_key_id=e.get("DOCUMENT_KMS_KEY_ID", ""),
            secure_cookies=_bool(e.get("SECURE_COOKIES"), e.get("ENVIRONMENT", "dev") in {"qa", "prod"}),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.environment not in {"dev", "test", "qa", "prod"}:
            raise ConfigError(f"ENVIRONMENT must be dev, test, qa or prod, not {self.environment!r}")
        if self.llm_backend not in {"fake", "bedrock", "anthropic"}:
            raise ConfigError("LLM_BACKEND must be fake, bedrock or anthropic")
        if self.llm_backend == "anthropic" and self.llm_enabled and not self.anthropic_api_key:
            raise ConfigError("ANTHROPIC_API_KEY is required when LLM_BACKEND=anthropic")
        if self.llm_backend == "bedrock" and self.llm_enabled and not self.bedrock_model_id:
            raise ConfigError("BEDROCK_MODEL_ID is required when LLM_BACKEND=bedrock")
        if self.environment == "prod" and self.llm_backend == "fake":
            raise ConfigError("the fake LLM backend is not allowed in prod")
        if self.environment in {"qa", "prod"}:
            missing = [
                n
                for n, v in (("ONBOARDING_TOKENS", self.tokens_json), ("SESSION_SECRET", self.session_secret))
                if not v
            ]
            if missing:
                raise ConfigError(f"{', '.join(missing)} must be set in {self.environment}")
        if self.document_store not in {"none", "local", "s3"}:
            raise ConfigError("DOCUMENT_STORE must be none, local or s3")
        if self.document_store == "s3" and not self.document_bucket:
            raise ConfigError("DOCUMENT_BUCKET is required when DOCUMENT_STORE=s3")
        if self.environment in {"qa", "prod"} and self.document_store != "s3":
            raise ConfigError(
                "DOCUMENT_STORE must be s3 in qa and prod: officers need the original documents"
            )
        if self.llm_retry_attempts < 1:
            raise ConfigError("LLM_RETRY_ATTEMPTS must be at least 1")
        if self.prompt_label not in {"production", "staging"}:
            raise ConfigError("PROMPT_LABEL must be production or staging")
