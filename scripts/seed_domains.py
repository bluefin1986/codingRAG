#!/usr/bin/env python3
"""Seed configured domains into PostgreSQL.

Run once after first deployment, after the application has created the
``domains`` table. Inserts are idempotent, so rerunning this script is safe.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import CODING_RAG_DATABASE_URL  # noqa: E402
from scripts.seed_data import DOMAIN_REGISTRY  # noqa: E402


def seed_domains() -> int:
    """Insert configured fallback domains and return the inserted row count."""
    if not CODING_RAG_DATABASE_URL:
        raise RuntimeError("CODING_RAG_DATABASE_URL is not configured")

    inserted = 0
    with psycopg.connect(CODING_RAG_DATABASE_URL) as conn, conn.cursor() as cur:
        for domain_key, cfg in DOMAIN_REGISTRY.items():
            cur.execute(
                """
                INSERT INTO domains (
                    domain_key, display_name, language, docs_dir, collection,
                    embedding_model, embedding_model_name, embedding_dim,
                    rerank_model_name, prompt_role, bm25_enabled, bm25_weight,
                    path_boost_per_match, noise_patterns, known_identifiers
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (domain_key) DO NOTHING
                """,
                [
                    domain_key,
                    cfg["display_name"],
                    cfg.get("language", ""),
                    str(cfg["docs_dir"]) if cfg.get("docs_dir") is not None else None,
                    cfg["collection"],
                    cfg.get("embedding_model", "BAAI/bge-m3"),
                    cfg.get("embedding_model_name", "bge-m3"),
                    cfg.get("embedding_dim", 1024),
                    cfg.get("rerank_model_name", "bge-reranker-base"),
                    cfg.get("prompt_role", "技术专家"),
                    cfg.get("bm25_enabled", True),
                    cfg.get("bm25_weight", 0.3),
                    cfg.get("path_boost_per_match", 0.0),
                    Jsonb(cfg.get("noise_patterns", [])),
                    Jsonb(cfg.get("known_identifiers", [])),
                ],
            )
            inserted += cur.rowcount
        conn.commit()
    return inserted


if __name__ == "__main__":
    count = seed_domains()
    print(f"Seeded {count} new domain row(s).")
