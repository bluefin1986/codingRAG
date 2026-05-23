#!/usr/bin/env python3
"""Seed configured query expansions into PostgreSQL.

Run this script once after the first deployment, after the application has
created the ``query_expansions`` table:

    python3 scripts/seed_query_expansions.py

The inserts are idempotent, so rerunning the script is safe.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import CODING_RAG_DATABASE_URL  # noqa: E402
from scripts.seed_data import DOMAIN_REGISTRY  # noqa: E402


def seed_query_expansions() -> int:
    """Insert configured query expansions and return the inserted row count."""
    if not CODING_RAG_DATABASE_URL:
        raise RuntimeError("CODING_RAG_DATABASE_URL is not configured")

    inserted = 0
    with psycopg.connect(CODING_RAG_DATABASE_URL) as conn, conn.cursor() as cur:
        for domain, cfg in DOMAIN_REGISTRY.items():
            for source_term, expanded_terms in cfg.get("query_expansions", {}).items():
                cur.execute(
                    """
                    INSERT INTO query_expansions (domain, source_term, expanded_terms)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (domain, source_term) DO NOTHING
                    """,
                    [domain, source_term, expanded_terms.split()],
                )
                inserted += cur.rowcount
        conn.commit()
    return inserted


if __name__ == "__main__":
    count = seed_query_expansions()
    print(f"Seeded {count} new query expansion row(s).")
