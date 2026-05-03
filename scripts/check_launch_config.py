"""Static launch configuration sanity check.

Does not call the database or external providers and never prints secret
values. It validates the currently loaded environment by default.

Examples:
  uv run python scripts/check_launch_config.py
  uv run python scripts/check_launch_config.py --profile production --require-scheduler
  uv run python scripts/check_launch_config.py --profile production --allow-fake-embeddings
"""
from __future__ import annotations

import argparse
import json

from app.core.config import settings
from app.core.launch_config import (
    has_errors,
    issue_counts,
    validate_launch_config,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Vifaras launch env without exposing secrets."
    )
    parser.add_argument(
        "--profile",
        choices=["current", "dev", "production"],
        default="current",
        help="Rule profile to apply. 'current' uses APP_ENV.",
    )
    parser.add_argument(
        "--require-scheduler",
        action="store_true",
        help="Fail production validation if ENABLE_AGENT_SCHEDULER is false.",
    )
    parser.add_argument(
        "--allow-fake-embeddings",
        action="store_true",
        help="Allow EMBEDDING_BACKEND=fake for an intentional Anthropic-only rehearsal.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    profile = None if args.profile == "current" else args.profile
    issues = validate_launch_config(
        settings,
        profile=profile,
        require_scheduler=args.require_scheduler,
        allow_fake_embeddings=args.allow_fake_embeddings,
    )
    counts = issue_counts(issues)
    ok = not has_errors(issues)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "profile": args.profile,
                    "app_env": settings.app_env,
                    **counts,
                    "issues": [
                        {
                            "severity": issue.severity,
                            "code": issue.code,
                            "message": issue.message,
                        }
                        for issue in issues
                    ],
                },
                indent=2,
            )
        )
    else:
        label = "OK" if ok else "FAILED"
        print(
            "Launch config "
            f"{label}: {counts['errors']} error(s), {counts['warnings']} warning(s)"
        )
        print(f"profile={args.profile} app_env={settings.app_env}")
        for issue in issues:
            print(f"[{issue.severity}] {issue.code}: {issue.message}")

    return 1 if has_errors(issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
