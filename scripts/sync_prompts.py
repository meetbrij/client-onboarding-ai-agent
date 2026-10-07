"""Push the checked-in prompts in prompts/ to Langfuse prompt management (needs LANGFUSE_* keys).

    uv run python scripts/sync_prompts.py                    # create or update with the `staging` label
    uv run python scripts/sync_prompts.py --label production # promote what is in the repo to production

A new version is created only when the text differs from the current version under that label. Review
prompt changes in Langfuse (and re-run the evals) before moving the `production` label.
"""

from __future__ import annotations

import argparse
import os
import sys

from onboarding.config import Settings
from onboarding.langfuse_tracing import make_client
from onboarding.llm.prompts import PROMPT_NAMES, LocalPromptStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", default="staging", choices=["staging", "production"])
    args = parser.parse_args()
    settings = Settings.from_env(
        {
            **os.environ,
            "LANGFUSE_PUBLIC_KEY": os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            "LANGFUSE_SECRET_KEY": os.environ.get("LANGFUSE_SECRET_KEY", ""),
        }
    )
    if not settings.tracing_enabled:
        print("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY", file=sys.stderr)
        return 2
    client = make_client(settings)
    local = LocalPromptStore()
    for name in PROMPT_NAMES:
        text = local.get(name).template
        try:
            current = client.get_prompt(
                name, label=args.label, type="text", cache_ttl_seconds=0, max_retries=0
            )
            if current.prompt == text:
                print(f"{name}: v{current.version} already matches ({args.label})")
                continue
        except Exception:  # noqa: BLE001 - the prompt does not exist yet under this label
            print(f"{name}: not found under {args.label}; creating it")
        created = client.create_prompt(
            name=name,
            prompt=text,
            labels=[args.label],
            type="text",
            commit_message=f"synced from the repository ({args.label})",
        )
        print(f"{name}: created v{created.version} with label {args.label}")
    client.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
