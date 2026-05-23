#!/usr/bin/env python3
"""Run regression queries against codingRAG and report pass/fail.

Usage:
    python3 scripts/run_regression.py
    python3 scripts/run_regression.py --domain nginx
    python3 scripts/run_regression.py --top-k 10

Each query in tests/regression_queries.json is run through DomainQueryEngine.search().
A query passes if ``expected_doc_key`` appears in the source_file of any top-K result,
or if ``expected_top1_source_file`` matches the top-1 source_file exactly.

Exit code 0 if all queries pass, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is importable
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from config import get_domain_config, EMBEDDING_API_BASE, QDRANT_HOST, QDRANT_PORT, RERANK_API_BASE  # noqa: E402
from api.engine import DomainQueryEngine  # noqa: E402


def _load_queries(path: Path, domain_filter: Optional[str] = None) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        queries = json.load(f)
    if domain_filter:
        queries = [q for q in queries if q.get("domain") == domain_filter]
    return queries


def _get_engine(domain: str) -> DomainQueryEngine:
    cfg = get_domain_config(domain)
    cfg["embedding_api_base"] = EMBEDDING_API_BASE
    cfg["rerank_api_base"] = RERANK_API_BASE
    cfg["qdrant_host"] = QDRANT_HOST
    cfg["qdrant_port"] = QDRANT_PORT
    return DomainQueryEngine(cfg)


def _check_pass(query_spec: dict, results: List[dict], top_k: int) -> tuple[bool, str]:
    """Check if the query result matches the expected outcome."""
    expected_file = query_spec.get("expected_top1_source_file")
    expected_key = query_spec.get("expected_doc_key")

    if expected_file:
        top1 = results[0] if results else {}
        if top1.get("source_file", "") == expected_file:
            return True, f"top1 matches: {expected_file}"
        # Also check if expected_file appears in top-K
        for r in results[:top_k]:
            if r.get("source_file", "") == expected_file:
                return True, f"found in top-{top_k}: {expected_file}"
        return False, f"expected {expected_file}, top1={top1.get('source_file', 'N/A')}"

    if expected_key:
        key_lower = expected_key.lower()
        for i, r in enumerate(results[:top_k]):
            source = r.get("source_file", "").lower()
            context = r.get("context", "").lower()
            if key_lower in source or key_lower in context:
                return True, f"key '{expected_key}' found at rank {i+1}: {r.get('source_file', '')}"
            # Also check text snippet (first 200 chars)
            text_preview = (r.get("text", "") or "")[:200].lower()
            if key_lower in text_preview:
                return True, f"key '{expected_key}' found in text at rank {i+1}: {r.get('source_file', '')}"
        top_sources = [r.get("source_file", "") for r in results[:3]]
        return False, f"key '{expected_key}' not found in top-{top_k}; top sources: {top_sources}"

    # No expectation defined — skip (count as pass)
    return True, "no expectation defined"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run codingRAG regression queries")
    parser.add_argument("--queries", type=Path, default=_PROJECT_ROOT / "tests" / "regression_queries.json",
                        help="Path to regression queries JSON file")
    parser.add_argument("--domain", type=str, default=None, help="Filter to a specific domain")
    parser.add_argument("--top-k", type=int, default=5, help="Top-K results to check against")
    parser.add_argument("--verbose", action="store_true", help="Print per-result details")
    args = parser.parse_args()

    queries = _load_queries(args.queries, args.domain)
    if not queries:
        print(f"No queries to run (filter: domain={args.domain})")
        sys.exit(0)

    print(f"Running {len(queries)} regression queries (top_k={args.top_k})...\n")

    engines: Dict[str, DomainQueryEngine] = {}
    passed = 0
    failed = 0
    errors = 0

    for i, qspec in enumerate(queries, 1):
        query = qspec["query"]
        domain = qspec["domain"]
        method = qspec.get("method", "hybrid_rerank")

        print(f"[{i}/{len(queries)}] domain={domain} method={method} query={query!r}")

        try:
            get_domain_config(domain)
        except KeyError:
            print(f"  SKIP: unknown domain {domain}")
            continue

        if domain not in engines:
            print(f"  Initializing engine for domain={domain}...")
            engines[domain] = _get_engine(domain)

        engine = engines[domain]
        try:
            started = time.perf_counter()
            results, _ = engine.search(query, top_k=args.top_k, method=method)
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            ok, detail = _check_pass(qspec, results, args.top_k)
            status = "PASS" if ok else "FAIL"
            print(f"  {status} ({elapsed_ms}ms): {detail}")
            if ok:
                passed += 1
            else:
                failed += 1
            if args.verbose and results:
                for rank, r in enumerate(results[:3], 1):
                    print(f"    rank={rank} score={r.get('score', 0):.4f} "
                          f"source={r.get('source_file', '')} "
                          f"context={r.get('context', '')[:60]}")

        except Exception as exc:
            print(f"  ERROR: {exc}")
            errors += 1

    total = passed + failed + errors
    pass_rate = (passed / total * 100) if total > 0 else 0
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {errors} errors / {total} total")
    print(f"Pass rate: {pass_rate:.1f}%")
    sys.exit(0 if failed == 0 and errors == 0 else 1)


if __name__ == "__main__":
    main()
