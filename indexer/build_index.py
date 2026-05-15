

"""Build external keyword index for codingRAG domains.

This script is intentionally focused on Elasticsearch/OpenSearch keyword index
maintenance. Qdrant vector indexing remains in the existing vector indexing
pipeline; this script can be used after chunks.jsonl has been generated.

Example:
    CODING_RAG_ES_URL=http://localhost:9200 \
    CODING_RAG_KEYWORD_BACKEND=elasticsearch \
    python -m indexer.build_index --domain harmonyos
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Optional

from config import get_domain_config
from indexer.es_indexer import ESIndexer

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _resolve_es_index_name(domain: str, explicit_index: Optional[str] = None) -> str:
    if explicit_index:
        return explicit_index
    return os.getenv(
        f"CODING_RAG_ES_INDEX_{domain.upper()}",
        f"codingrag_{domain}_docs",
    )


def build_keyword_index(
    *,
    domain: str,
    chunks_path: Optional[Path] = None,
    es_url: Optional[str] = None,
    es_index: Optional[str] = None,
    clear_domain: bool = True,
) -> int:
    """Build ES/OpenSearch keyword index from a domain chunks.jsonl file."""
    cfg = get_domain_config(domain)
    resolved_chunks_path = chunks_path or cfg["output_dir"] / "chunks.jsonl"
    resolved_es_url = (es_url or os.getenv("CODING_RAG_ES_URL", "")).strip()

    if not resolved_es_url:
        raise ValueError("CODING_RAG_ES_URL is required to build Elasticsearch/OpenSearch keyword index")

    if not resolved_chunks_path.exists():
        raise FileNotFoundError(f"chunks file not found: {resolved_chunks_path}")

    index_name = _resolve_es_index_name(domain, es_index)
    logger.info(
        "building keyword index domain=%s chunks=%s es_url=%s index=%s clear_domain=%s",
        domain,
        resolved_chunks_path,
        resolved_es_url,
        index_name,
        clear_domain,
    )

    indexer = ESIndexer(
        base_url=resolved_es_url,
        index_name=index_name,
        api_key=os.getenv("CODING_RAG_ES_API_KEY"),
    )
    try:
        count = indexer.index_chunks_file(
            resolved_chunks_path,
            domain=domain,
            clear_domain=clear_domain,
        )
    finally:
        indexer.close()

    logger.info("keyword index build complete domain=%s index=%s count=%d", domain, index_name, count)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Build codingRAG Elasticsearch/OpenSearch keyword index")
    parser.add_argument("--domain", required=True, help="Domain name, for example: harmonyos")
    parser.add_argument("--chunks", type=Path, default=None, help="Optional path to chunks.jsonl")
    parser.add_argument("--es-url", default=None, help="ES/OpenSearch base URL. Defaults to CODING_RAG_ES_URL")
    parser.add_argument("--es-index", default=None, help="ES/OpenSearch index name")
    parser.add_argument(
        "--no-clear-domain",
        action="store_true",
        help="Do not delete existing documents for the domain before indexing",
    )
    args = parser.parse_args()

    build_keyword_index(
        domain=args.domain,
        chunks_path=args.chunks,
        es_url=args.es_url,
        es_index=args.es_index,
        clear_domain=not args.no_clear_domain,
    )


if __name__ == "__main__":
    main()