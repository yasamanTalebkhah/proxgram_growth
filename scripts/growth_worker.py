#!/usr/bin/env python3
"""ProxGram growth worker runner.

Standalone process, fully isolated from the main posting bot. Credentials
come exclusively from environment variables (API_ID, API_HASH,
SESSION_STRING) — never from command-line arguments, so they cannot leak
into shell history or process listings.

Usage:
    python scripts/growth_worker.py [--config config/growth_worker.json] \
        [--templates config/growth_templates.json] [--dry-run] [--once]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make the project root importable when run as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from proxgram_growth.config import ConfigError, load_config  # noqa: E402
from proxgram_growth.logging_setup import configure_logging  # noqa: E402
from proxgram_growth.worker import GrowthWorker  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ProxGram growth worker")
    parser.add_argument(
        "--config",
        default="config/growth_worker.json",
        help="Path to worker JSON config (non-secret settings)",
    )
    parser.add_argument(
        "--templates",
        default="config/growth_templates.json",
        help="Path to comment templates JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without posting any comments",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exit immediately after startup checks (smoke test)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    env = dict(**__import__("os").environ)
    if args.dry_run:
        env["GROWTH_DRY_RUN"] = "1"

    try:
        config = load_config(
            args.config if Path(args.config).exists() else None,
            env=env,
            templates_path=args.templates if Path(args.templates).exists() else None,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read configuration: {exc}", file=sys.stderr)
        return 2

    configure_logging(
        level=config.log_level,
        # Redact credentials from every log record, including tracebacks.
        secrets=(config.session_string, config.api_hash, str(config.api_id)),
    )

    worker = GrowthWorker(config)
    try:
        if args.once:
            asyncio.run(_smoke(worker))
            return 0
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        pass
    return 0


async def _smoke(worker: GrowthWorker) -> None:
    """Connect, resolve targets, then exit — used by CI and --once."""
    await worker.run()
    worker.request_stop()


if __name__ == "__main__":
    sys.exit(main())
