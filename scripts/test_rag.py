#!/usr/bin/env python3
"""Quick RAG smoke test for codingRAG.

Examples:
  python3 scripts/test_rag.py --domain ios --query "Objective-C 怎么创建 UIButton" --method hybrid
  python3 scripts/test_rag.py --domain ios --query "隐藏导航栏返回按钮" --method rerank --top-k 5
  python3 scripts/test_rag.py --domain harmonyos --query "ArkTS 怎么创建按钮组件"
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def relaunch_with_domain(domain: str) -> None:
    """config.py reads CODING_RAG_DOMAIN at import time, so set it before importing retriever."""
    if os.environ.get("CODING_RAG_DOMAIN") == domain:
        return
    env = os.environ.copy()
    env["CODING_RAG_DOMAIN"] = domain
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)


def one_line(text: str, limit: int = 260) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def check_http(url: str) -> bool:
    try:
        import httpx

        resp = httpx.get(url, timeout=3.0)
        return resp.status_code < 500
    except Exception:
        return False


def check_qdrant_collection(host: str, port: int, collection: str) -> tuple[bool, str]:
    try:
        import httpx
        import os

        url = f"http://{host}:{port}/collections/{collection}"
        api_key = os.getenv("CODING_RAG_QDRANT_API_KEY", os.getenv("QDRANT_API_KEY", ""))
        headers = {"api-key": api_key} if api_key else None
        resp = httpx.get(url, timeout=5.0, headers=headers)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json().get("result", {})
        points = data.get("points_count") or data.get("vectors_count")
        return True, f"points={points}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate codingRAG retrieval quality")
    parser.add_argument("--domain", default="ios", help="Domain from config.DOMAIN_REGISTRY, e.g. ios/harmonyos")
    parser.add_argument("--query", "-q", default="Objective-C 怎么创建 UIButton 并响应点击事件", help="Query text")
    parser.add_argument("--method", choices=["hybrid", "rerank", "hybrid_rerank", "semantic", "bm25"], default="hybrid")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--category", default=None)
    parser.add_argument("--has-code", action="store_true", help="Only retrieve chunks with code")
    parser.add_argument("--show-text", type=int, default=700, help="Characters to print per result")
    parser.add_argument("--show-prompt", action="store_true", help="Print assembled RAG prompt")
    args = parser.parse_args()

    relaunch_with_domain(args.domain)

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    import config
    from indexer.retriever import rag_query

    print("=" * 88)
    print("codingRAG smoke test")
    print("=" * 88)
    print(f"domain:      {config.ACTIVE_DOMAIN}")
    print(f"docs_dir:    {config.DOCS_DIR}")
    print(f"chunks:      {config.CHUNKS_FILE} exists={config.CHUNKS_FILE.exists()}")
    print(f"collection:  {config.COLLECTION_NAME}")
    print(f"embedding:   {config.EMBEDDING_MODEL_NAME} ({config.EMBEDDING_DIM}d)")
    print(f"rerank:      {config.RERANK_MODEL_NAME}")
    print(f"method:      {args.method}")
    print(f"query:       {args.query}")

    aimodels_ok = check_http(f"{config.AIMODELS_API_BASE}/")
    qdrant_ok, qdrant_info = check_qdrant_collection(config.QDRANT_HOST, config.QDRANT_PORT, config.COLLECTION_NAME)
    print(f"aimodels:    {'OK' if aimodels_ok else 'FAIL'} {config.AIMODELS_API_BASE}")
    print(f"qdrant:      {'OK' if qdrant_ok else 'FAIL'} {qdrant_info}")
    print("-" * 88)

    result = rag_query(
        question=args.query,
        top_k=args.top_k,
        category=args.category,
        has_code=True if args.has_code else None,
        method=args.method,
    )

    results = result["results"]
    print(f"hits: {len(results)}")
    print()
    for idx, item in enumerate(results, 1):
        print(f"[{idx}] score={item.get('score', 0):.6f} source={item.get('source_file', '')}")
        if "rerank_score" in item:
            print(f"    rerank_score={item['rerank_score']:.6f} model={item.get('rerank_model')}")
        print(f"    context={one_line(item.get('context', ''), 180)}")
        print(f"    domain={item.get('domain')} has_code={item.get('has_code')}")
        print(f"    text={one_line(item.get('text', ''), args.show_text)}")
        print()

    if args.show_prompt:
        print("=" * 88)
        print("PROMPT")
        print("=" * 88)
        print(result["prompt"])

    if not results:
        print("No hits. Check collection indexing, domain, and chunks path.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
