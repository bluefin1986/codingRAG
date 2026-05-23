#!/usr/bin/env python3
"""Compare local BM25 vs OpenSearch keyword retrieval for one query.

The script is intentionally generic: it does not add query-specific synonyms,
keyword expansions, or hand-written boosts. It compares two keyword backends on
the same chunk body text:

- Local BM25: jieba + rank_bm25 over chunk["text"]
- OpenSearch: BM25 query against the index `text` field only

Examples:
  python3 scripts/compare_keyword_backends.py \
    --domain harmonyos \
    --query "鸿蒙怎么生成动态uuid" \
    --es-url http://localhost:9200

  python3 scripts/compare_keyword_backends.py \
    --domain harmonyos \
    --query "鸿蒙怎么生成动态uuid" \
    --env-file .env.debug
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def relaunch_with_domain(domain: str) -> None:
    # config.py reads CODING_RAG_DOMAIN at import time.
    if os.environ.get("CODING_RAG_DOMAIN") == domain:
        return
    env = os.environ.copy()
    env["CODING_RAG_DOMAIN"] = domain
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)


def iter_chunks(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_chunks(path: Path, limit: int | None = None) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for idx, record in enumerate(iter_chunks(path)):
        if limit is not None and idx >= limit:
            break
        meta = dict(record.get("metadata") or {})
        meta.setdefault("chunk_pos", idx)
        chunks.append({
            "text": str(record.get("text") or ""),
            "metadata": meta,
        })
    return chunks


def one_line(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def key_of_result(item: Any) -> Tuple[str, int]:
    meta = item.metadata if hasattr(item, "metadata") else item.get("metadata", {})
    return (str(meta.get("source_file", "")), int(meta.get("chunk_index", meta.get("chunk_pos", 0)) or 0))


def print_results(title: str, results: List[Any], other_keys: set[Tuple[str, int]], show_text: int) -> None:
    print("=" * 100)
    print(title)
    print("=" * 100)
    if not results:
        print("<no hits>")
        return

    for idx, item in enumerate(results, 1):
        meta = item.metadata
        key = key_of_result(item)
        overlap = "YES" if key in other_keys else "no"
        print(
            f"[{idx:02d}] score={item.score:.6f} overlap={overlap} "
            f"source={meta.get('source_file', '')}#chunk={meta.get('chunk_index', meta.get('chunk_pos', 0))}"
        )
        print(f"     context={one_line(str(meta.get('context', '')), 180)}")
        print(f"     text={one_line(item.text, show_text)}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare local BM25 and OpenSearch text-only BM25 results")
    parser.add_argument("--domain", default="harmonyos", help="Seeded database domain")
    parser.add_argument("--query", "-q", required=True, help="Query to compare")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--chunks", type=Path, default=None, help="Override chunks.jsonl path")
    parser.add_argument("--chunk-limit", type=int, default=None, help="Load only first N chunks for local BM25 debug")
    parser.add_argument("--es-url", default=None, help="OpenSearch/Elasticsearch base URL; defaults to CODING_RAG_ES_URL")
    parser.add_argument("--es-index", default=None, help="Index name; defaults to CODING_RAG_ES_INDEX_<DOMAIN>")
    parser.add_argument("--es-api-key", default=None, help="API key; defaults to CODING_RAG_ES_API_KEY")
    parser.add_argument("--env-file", type=Path, default=None, help="Optional .env file to load before importing config")
    parser.add_argument("--category", default=None)
    parser.add_argument("--has-code", choices=["true", "false", "any"], default="any")
    parser.add_argument("--show-text", type=int, default=280)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    if args.env_file:
        env_path = args.env_file if args.env_file.is_absolute() else project_root / args.env_file
        load_env_file(env_path)

    relaunch_with_domain(args.domain)

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    import config
    from indexer.es_searcher import ESSearcher
    from indexer.local_bm25_searcher import LocalBM25Searcher

    chunks_path = args.chunks or config.get_domain_config(config.ACTIVE_DOMAIN)["output_dir"] / "chunks.jsonl"
    if not chunks_path.exists():
        print(f"chunks file not found: {chunks_path}", file=sys.stderr)
        return 2

    es_url = args.es_url or os.getenv("CODING_RAG_ES_URL", "").strip()
    if not es_url:
        print("OpenSearch URL missing. Pass --es-url or set CODING_RAG_ES_URL.", file=sys.stderr)
        return 2

    es_index = args.es_index or os.getenv(
        f"CODING_RAG_ES_INDEX_{config.ACTIVE_DOMAIN.upper()}",
        f"codingrag_{config.ACTIVE_DOMAIN}_docs",
    )
    has_code = None if args.has_code == "any" else args.has_code == "true"

    print("codingRAG keyword backend comparison")
    print(f"domain:       {config.ACTIVE_DOMAIN}")
    print(f"query:        {args.query}")
    print(f"chunks:       {chunks_path}")
    print(f"es_url:       {es_url}")
    print(f"es_index:     {es_index}")
    print(f"top_k:        {args.top_k}")
    print(f"scope:        text-only, no query-specific expansion/boost")
    print()

    chunks = load_chunks(chunks_path, limit=args.chunk_limit)
    local = LocalBM25Searcher(domain=config.ACTIVE_DOMAIN, chunks=chunks, config={"domain": config.ACTIVE_DOMAIN})
    es = ESSearcher(
        domain=config.ACTIVE_DOMAIN,
        index_name=es_index,
        base_url=es_url,
        api_key=args.es_api_key or os.getenv("CODING_RAG_ES_API_KEY"),
        config={"domain": config.ACTIVE_DOMAIN},
    )

    local_results = local.search(args.query, top_k=args.top_k, category=args.category, has_code=has_code)
    es_results = es.search(args.query, top_k=args.top_k, category=args.category, has_code=has_code)

    local_keys = {key_of_result(item) for item in local_results}
    es_keys = {key_of_result(item) for item in es_results}
    overlap = local_keys & es_keys

    print(f"local_hits:   {len(local_results)}")
    print(f"es_hits:      {len(es_results)}")
    print(f"overlap@{args.top_k}: {len(overlap)}")
    print()

    print_results("LOCAL BM25 / jieba over chunk text", local_results, es_keys, args.show_text)
    print_results("OPENSEARCH BM25 / text field only", es_results, local_keys, args.show_text)

    es.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
