#!/usr/bin/env python3
"""Run pending codingRAG library import jobs.

Usage:
  python3 scripts/library_import_worker.py --once
  python3 scripts/library_import_worker.py --job-id <uuid>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.registry import DocumentRegistry  # noqa: E402
from config import CODING_RAG_IMPORT_BATCH_SIZE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="codingRAG async library import worker")
    parser.add_argument("--job-id", help="Run one specific pending/failed import job")
    parser.add_argument("--once", action="store_true", help="Run pending jobs once and exit")
    parser.add_argument("--limit", type=int, default=1, help="Max pending jobs per pass")
    parser.add_argument("--batch-size", type=int, default=CODING_RAG_IMPORT_BATCH_SIZE, help="Documents per DB batch")
    parser.add_argument("--sleep", type=float, default=5.0, help="Seconds between polling passes when not --once")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    registry = DocumentRegistry()

    if args.job_id:
        result = registry.run_import_job(args.job_id, batch_size=args.batch_size)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    while True:
        results = registry.run_pending_import_jobs(limit=args.limit, batch_size=args.batch_size)
        if results:
            print(json.dumps(results, ensure_ascii=False, default=str))
        if args.once:
            return 0
        time.sleep(max(1.0, args.sleep))


if __name__ == "__main__":
    raise SystemExit(main())
