"""scripts/set_secrets.sh must never overwrite a secret that already has a value by accident.

The script is run for real, against a fake `aws` command that keeps its state in a folder, so no AWS access is needed."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("scripts/set_secrets.sh")
pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "jq", "openssl")), reason="needs bash, jq and openssl"
)

FAKE_AWS = r"""#!/usr/bin/env bash
# A stand-in for the AWS CLI: records calls and keeps one "version" per secret in $FAKE_AWS_DIR.
set -eu
d="$FAKE_AWS_DIR"
[ "${FAKE_AWS_FAIL:-}" = "denied" ] && { echo "An error occurred (AccessDeniedException) when calling the DescribeSecret operation" >&2; exit 254; }
sub="$2"; shift 2
secret=""; file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --secret-id) secret="$2"; shift ;;
    --secret-string) file="${2#file://}"; shift ;;
  esac
  shift
done
key="$(echo "$secret" | tr '/' '_')"
case "$sub" in
  describe-secret)
    echo "describe $secret" >> "$d/calls.log"
    if [ -f "$d/$key.version" ]; then
      v="$(cat "$d/$key.version")"
      printf '{"Name":"%s","VersionIdsToStages":{"%s":["AWSCURRENT"],"old-%s":["AWSPREVIOUS"]}}\n' "$secret" "$v" "$v"
    else
      printf '{"Name":"%s"}\n' "$secret"
    fi ;;
  put-secret-value)
    echo "put $secret" >> "$d/calls.log"
    n=$(( $(cat "$d/counter" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$d/counter"
    echo "v$n" > "$d/$key.version"
    cp "$file" "$d/$key.json"
    echo "v$n" ;;
  *) echo "unexpected aws call: $sub" >&2; exit 99 ;;
esac
"""


@pytest.fixture
def aws(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "aws"
    fake.write_text(FAKE_AWS)
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_AWS_DIR": str(state)}
    env.pop("FAKE_AWS_FAIL", None)

    class Ctx:
        def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(  # noqa: S603
                ["bash", str(SCRIPT), *args],
                capture_output=True,
                text=True,
                env={**env, **extra},
                check=False,  # noqa: S607
            )

        def puts(self) -> list[str]:
            log = state / "calls.log"
            return (
                [line.split(" ", 1)[1] for line in log.read_text().splitlines() if line.startswith("put")]
                if log.exists()
                else []
            )

        def value(self, secret: str) -> dict:
            return json.loads((state / f"{secret.replace('/', '_')}.json").read_text())

        def has_value(self, secret: str) -> bool:
            return (state / f"{secret.replace('/', '_')}.version").exists()

        def preset(self, secret: str, version: str = "v-existing") -> None:
            (state / f"{secret.replace('/', '_')}.version").write_text(version)
            (state / f"{secret.replace('/', '_')}.json").write_text('{"ORIGINAL":"keep-me"}')

    return Ctx()


def test_the_script_never_reads_a_secret_value():
    text = SCRIPT.read_text()
    assert "get-secret-value" not in text and "describe-secret" in text


def test_a_fresh_environment_gets_all_three_secrets_with_the_right_shape(aws):
    r = aws.run("qa")
    assert r.returncode == 0, r.stderr
    assert aws.puts() == [
        "qa/onboarding/pg-secret",
        "qa/onboarding/app-secret",
        "qa/onboarding/langfuse-keys",
    ]
    pg = aws.value("qa/onboarding/pg-secret")
    assert set(pg) == {
        "POSTGRES_PASSWORD",
        "ONBOARDING_OWNER_PASSWORD",
        "ONBOARDING_APP_PASSWORD",
        "MOCKBANK_PASSWORD",
    }
    assert (
        all(re.fullmatch(r"[0-9a-f]{48}", v) for v in pg.values()) and len(set(pg.values())) == 4
    )  # URL-safe, all different
    app = aws.value("qa/onboarding/app-secret")
    assert set(app) == {"ONBOARDING_TOKENS", "SESSION_SECRET", "SMOKE_TOKEN"}
    tokens = json.loads(app["ONBOARDING_TOKENS"])
    assert [(t["id"], t["role"]) for t in tokens] == [
        ("officer-1", "officer"),
        ("officer-2", "officer"),
        ("submitter-1", "submitter"),
        ("smoke-bot", "submitter"),
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", t["sha256"]) for t in tokens)
    assert aws.value("qa/onboarding/langfuse-keys") == {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}
    assert (
        "NOT SET   qa/onboarding/kyc-api-key" in r.stdout
    )  # no key in the environment: nothing is written, nothing guessed


def test_the_printed_tokens_match_the_stored_hashes_and_nothing_else_secret_is_printed(aws):
    import hashlib

    r = aws.run("qa")
    printed = dict(re.findall(r"^\s+(officer-1|officer-2|submitter-1):\s+([0-9a-f]{48})$", r.stdout, re.M))
    assert set(printed) == {"officer-1", "officer-2", "submitter-1"}
    stored = {
        t["id"]: t["sha256"] for t in json.loads(aws.value("qa/onboarding/app-secret")["ONBOARDING_TOKENS"])
    }
    for who, token in printed.items():
        assert hashlib.sha256(token.encode()).hexdigest() == stored[who]
    everything = r.stdout + r.stderr
    for secret_value in [
        *aws.value("qa/onboarding/pg-secret").values(),
        aws.value("qa/onboarding/app-secret")["SMOKE_TOKEN"],
        aws.value("qa/onboarding/app-secret")["SESSION_SECRET"],
    ]:
        assert secret_value not in everything


def test_secrets_that_already_have_a_value_are_skipped_not_overwritten(aws):
    for s in ("pg-secret", "app-secret", "langfuse-keys"):
        aws.preset(f"qa/onboarding/{s}", f"v-{s}")
    r = aws.run("qa")
    assert r.returncode == 0
    assert aws.puts() == []
    assert r.stdout.count("SKIPPED") == 3 and "v-pg-secret" in r.stdout and "--force pg-secret" in r.stdout
    assert aws.value("qa/onboarding/pg-secret") == {"ORIGINAL": "keep-me"}
    assert "Sign-in tokens" not in r.stdout  # nothing new was created, so there are no new tokens to show


def test_running_it_for_prod_after_qa_does_not_touch_qa(aws):
    aws.run("qa")
    before = aws.value("qa/onboarding/pg-secret")
    r = aws.run("prod")
    assert r.returncode == 0 and aws.puts()[3:] == [
        f"prod/onboarding/{s}" for s in ("pg-secret", "app-secret", "langfuse-keys")
    ]
    assert aws.value("qa/onboarding/pg-secret") == before
    assert aws.value("prod/onboarding/pg-secret") != before


def test_only_the_empty_ones_are_written_when_some_have_values(aws):
    aws.preset("qa/onboarding/pg-secret")
    r = aws.run("qa")
    assert r.returncode == 0 and aws.puts() == ["qa/onboarding/app-secret", "qa/onboarding/langfuse-keys"]
    assert "SKIPPED   qa/onboarding/pg-secret" in r.stdout and aws.value("qa/onboarding/pg-secret") == {
        "ORIGINAL": "keep-me"
    }


def test_force_replaces_one_secret_and_prints_the_old_version_and_the_rollback_command(aws):
    for s in ("pg-secret", "app-secret", "langfuse-keys"):
        aws.preset(f"qa/onboarding/{s}", f"v-{s}")
    r = aws.run("qa", "--force", "app-secret")
    assert r.returncode == 0
    assert aws.puts() == ["qa/onboarding/app-secret"]
    assert "REPLACED  qa/onboarding/app-secret: old version v-app-secret, new version v1." in r.stdout
    assert (
        "--version-stage AWSCURRENT --move-to-version-id v-app-secret --remove-from-version-id v1" in r.stdout
    )
    assert aws.value("qa/onboarding/pg-secret") == {"ORIGINAL": "keep-me"}  # untouched
    assert "Sign-in tokens" in r.stdout  # the new tokens are shown once


def test_pg_secret_needs_a_second_explicit_flag_even_with_force(aws):
    aws.preset("qa/onboarding/pg-secret", "v-pg")
    refused = aws.run("qa", "--force", "pg-secret")
    assert refused.returncode == 3 and "locks the API and mock bank out of Postgres" in refused.stderr
    assert "qa/onboarding/pg-secret" not in aws.puts() and aws.value("qa/onboarding/pg-secret") == {
        "ORIGINAL": "keep-me"
    }
    allowed = aws.run("qa", "--force", "pg-secret", "--confirm-pg-secret-overwrite")
    assert allowed.returncode == 0 and "REPLACED  qa/onboarding/pg-secret: old version v-pg" in allowed.stdout


def test_a_first_time_pg_secret_needs_no_confirmation(aws):
    assert aws.run("qa").returncode == 0 and "qa/onboarding/pg-secret" in aws.puts()


def test_dry_run_changes_nothing(aws):
    aws.preset("qa/onboarding/app-secret", "v-app")
    r = aws.run("qa", "--dry-run", "--force", "app-secret")
    assert r.returncode == 0 and aws.puts() == []
    assert "DRY RUN   would write qa/onboarding/app-secret (replacing version v-app)" in r.stdout
    assert "DRY RUN   would write qa/onboarding/pg-secret" in r.stdout


def test_a_lookup_failure_stops_everything_instead_of_guessing(aws):
    r = aws.run("qa", FAKE_AWS_FAIL="denied")
    assert r.returncode != 0 and aws.puts() == [] and "could not look up qa/onboarding/pg-secret" in r.stderr
    assert not aws.has_value("qa/onboarding/pg-secret")


@pytest.mark.parametrize(
    "args", [(), ("staging",), ("qa", "--force", "nope"), ("qa", "--force"), ("qa", "--bogus")]
)
def test_bad_arguments_are_refused_before_any_aws_call(aws, args):
    r = aws.run(*args)
    assert r.returncode == 2 and aws.puts() == []
    assert not (Path(os.environ.get("FAKE_AWS_DIR", "/nonexistent")) / "calls.log").exists()


def test_the_api_keys_come_from_the_environment_and_are_never_printed(aws):
    r = aws.run("qa", KYC_API_KEY="kyc-key-value-123", ANTHROPIC_API_KEY="anthropic-key-value-456")
    assert r.returncode == 0
    assert aws.value("qa/onboarding/kyc-api-key") == {"KYC_API_KEY": "kyc-key-value-123"}
    assert aws.value("qa/onboarding/anthropic-api-key") == {"ANTHROPIC_API_KEY": "anthropic-key-value-456"}
    assert (
        "kyc-key-value-123" not in r.stdout + r.stderr
        and "anthropic-key-value-456" not in r.stdout + r.stderr
    )


def test_an_existing_api_key_is_not_replaced_without_force(aws):
    aws.preset("qa/onboarding/kyc-api-key", "v-kyc")
    r = aws.run("qa", KYC_API_KEY="new-value")
    assert "SKIPPED   qa/onboarding/kyc-api-key" in r.stdout and aws.value("qa/onboarding/kyc-api-key") == {
        "ORIGINAL": "keep-me"
    }
    r = aws.run("qa", "--force", "kyc-api-key", KYC_API_KEY="new-value")
    assert "REPLACED  qa/onboarding/kyc-api-key: old version v-kyc" in r.stdout
    assert aws.value("qa/onboarding/kyc-api-key") == {"KYC_API_KEY": "new-value"}
