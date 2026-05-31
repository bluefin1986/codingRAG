#!/usr/bin/env python3
"""Run reindex worker. All config from .env file."""
import os
import sys
from pathlib import Path

# Load .env BEFORE any imports
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in os.environ:
                os.environ[key] = value.strip()

# Now import and run
from api.registry import DocumentRegistry
from indexer.reindex_worker import run_reindex_worker

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=getattr(logging, args.log_level))

    registry = DocumentRegistry()
    run_reindex_worker(registry, args.job_id)
