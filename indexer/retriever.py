"""RAG 查询模块：混合检索（语义 + BM25 + 路径加权） + LLM 回答"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

import httpx
from tqdm import tqdm

from config import (
    ACTIVE_DOMAIN,
    CHUNKS_FILE,
    COLLECTION_NAME,
    EMBEDDING_API_BASE,
    EMBEDDING_MODEL_NAME,
    PROMPT_ROLE,
    QDRANT_HOST,
    QDRANT_PORT,
    RERANK_API_BASE,
    RERANK_MODEL_NAME,
)

from indexer.es_searcher import ESSearcher
from indexer.local_bm25_searcher import LocalBM25Searcher

logger = logging.getLogger(__name__)


# ── Embedding ──

def embed_query(query: str) -> List[float]:
    """将查询文本转为 embedding 向量。"""
    client = httpx.Client(timeout=60.0)
    resp = client.post(
        f"{EMBEDDING_API_BASE}/api/v1/embeddings",
        json={"input": [query], "model": EMBEDDING_MODEL_NAME},
    )
    resp.raise_for_status()
    client.close()
    return resp.json()["data"][0]["embedding"]


# ── Keyword search backend ──


def _keyword_search_text(record: dict) -> str:
    """Build a compact keyword-search document to avoid tokenizing huge chunks online."""
    meta = record.get("metadata", {}) or {}
    text = record.get("text", "") or ""
    return " ".join([
        str(meta.get("context", "")),
        str(meta.get("source_file", "")),
        text[:1200],
    ])


@lru_cache(maxsize=1)
def _load_keyword_searcher():
    """Load keyword search backend for legacy retriever.py entry points."""
    backend = os.getenv("CODING_RAG_KEYWORD_BACKEND", "local_bm25").strip().lower()

    if backend in ("elasticsearch", "opensearch", "es"):
        base_url = os.getenv("CODING_RAG_ES_URL", "").strip()
        if base_url:
            index_name = os.getenv(
                f"CODING_RAG_ES_INDEX_{ACTIVE_DOMAIN.upper()}",
                f"codingrag_{ACTIVE_DOMAIN}_docs",
            )
            searcher = ESSearcher(
                domain=ACTIVE_DOMAIN,
                index_name=index_name,
                base_url=base_url,
                api_key=os.getenv("CODING_RAG_ES_API_KEY"),
                config={"domain": ACTIVE_DOMAIN},
            )
            logger.info(
                "keyword searcher loaded for legacy retriever backend=%s index=%s url=%s",
                backend,
                index_name,
                base_url,
            )
            return searcher, []

        logger.warning(
            "keyword backend=%s selected but CODING_RAG_ES_URL is empty; falling back to local BM25",
            backend,
        )

    chunks_path = CHUNKS_FILE
    if not chunks_path.exists():
        logger.warning("chunks file not found: %s, keyword search disabled", chunks_path)
        return None, []

    chunks: List[Dict[str, Any]] = []
    with open(chunks_path, encoding="utf-8") as f:
        for line in tqdm(f, desc="Keyword loading chunks", unit="chunk"):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            compact_record = dict(record)
            compact_record["original_text"] = record.get("text", "") or ""
            compact_record["text"] = _keyword_search_text(record)
            chunks.append(compact_record)

    if not chunks:
        logger.warning("no chunks found in %s, keyword search disabled", chunks_path)
        return None, []

    searcher = LocalBM25Searcher(domain=ACTIVE_DOMAIN, chunks=chunks, config={"domain": ACTIVE_DOMAIN})
    logger.info("keyword searcher loaded for legacy retriever backend=local_bm25 chunks=%d", len(chunks))
    return searcher, chunks


def bm25_search(query: str, top_k: int = 20) -> List[dict]:
    """BM25 keyword retrieval through the configured keyword backend."""
    searcher, chunks = _load_keyword_searcher()
    if searcher is None:
        return []

    keyword_results = searcher.search(query, top_k=top_k)
    results = []
    for item in keyword_results:
        meta = item.metadata or {}
        chunk_pos = meta.get("chunk_pos")
        text = item.text
        if chunks and chunk_pos is not None:
            try:
                text = chunks[int(chunk_pos)].get("original_text") or item.text
            except (IndexError, TypeError, ValueError):
                text = item.text

        results.append({
            "bm25_score": float(item.score),
            "domain": meta.get("domain", ACTIVE_DOMAIN),
            "text": text,
            "context": meta.get("context", ""),
            "source_file": meta.get("source_file", ""),
            "has_code": meta.get("has_code", False),
            "chunk_index": meta.get("chunk_index", chunk_pos if chunk_pos is not None else 0),
        })

    return results


# ── 语义检索 ──

def semantic_search(
    query: str,
    top_k: int = 20,
    category: Optional[str] = None,
    has_code: Optional[bool] = None,
) -> List[dict]:
    """语义检索，返回最相关的 chunks。"""
    query_vector = embed_query(query)

    must_filters = []
    # 默认靠 domain 对应 collection 隔离；payload 仍写入 domain，便于后续共享 collection 时过滤。
    if category:
        must_filters.append({"key": "category", "match": {"value": category}})
    if has_code is not None:
        must_filters.append({"key": "has_code", "match": {"value": has_code}})

    search_body = {
        "vector": query_vector,
        "limit": top_k,
        "with_payload": True,
        "with_vector": False,
    }
    if must_filters:
        search_body["filter"] = {"must": must_filters}

    client = httpx.Client(timeout=30.0)
    resp = client.post(
        f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{COLLECTION_NAME}/points/search",
        json=search_body,
    )
    resp.raise_for_status()
    client.close()

    results = []
    for hit in resp.json()["result"]:
        results.append({
            "semantic_score": hit["score"],
            "domain": hit["payload"].get("domain", ACTIVE_DOMAIN),
            "text": hit["payload"]["text"],
            "context": hit["payload"].get("context", ""),
            "source_file": hit["payload"].get("source_file", ""),
            "has_code": hit["payload"].get("has_code", False),
        })

    return results


# ── RRF 融合 ──

def rrf_fuse(
    semantic_results: List[dict],
    bm25_results: List[dict],
    k: int = 30,
    semantic_weight: float = 1.0,
    bm25_weight: float = 0.1,
    top_k: int = 20,
    bm25_only_boost: float = 1.0,
) -> List[dict]:
    """
    Reciproral Rank Fusion 融合语义 + BM25 两路结果。
    RRF score = Σ weight / (k + rank)
    """
    def _key(r):
        return (r.get("source_file", ""), r.get("chunk_index", 0))

    sem_keys = set()
    bm25_keys = set()
    rrf_scores: dict[tuple, float] = {}
    merged: dict[tuple, dict] = {}

    for rank, r in enumerate(semantic_results):
        key = _key(r)
        sem_keys.add(key)
        rrf_scores[key] = rrf_scores.get(key, 0) + semantic_weight / (k + rank + 1)
        if key not in merged:
            merged[key] = r

    for rank, r in enumerate(bm25_results):
        key = _key(r)
        bm25_keys.add(key)
        rrf_scores[key] = rrf_scores.get(key, 0) + bm25_weight / (k + rank + 1)
        if key not in merged:
            merged[key] = r

    # Optional boost for BM25-only hits. Default is neutral because it can push
    # exact-keyword but semantically weak API enum pages above better semantic hits.
    for key in bm25_keys - sem_keys:
        rrf_scores[key] *= bm25_only_boost

    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)

    results = []
    for key in sorted_keys[:top_k]:
        r = merged[key].copy()
        r["score"] = rrf_scores[key]
        r.pop("bm25_score", None)
        r.pop("semantic_score", None)
        results.append(r)

    return results


# ── 路径加权 ──

_STOPWORDS = frozenset(('怎么', '如何', '什么', '为何', '为什么', '是否', '怎样', '的', '了', '和', '与', '或', '在', '是', '有', '一个', '几个', '用来', '还有'))


def path_boost(results: List[dict], query: str, boost_per_match: float = 0.8) -> List[dict]:
    """
    路径/标题加权：query 分词后，关键词在 source_file 或 context 中出现则加权。
    """
    import jieba

    query_tokens = list(jieba.cut(query))
    query_keywords = [t.lower() for t in query_tokens if len(t) >= 2 and t not in _STOPWORDS]

    for r in results:
        path_text = (r.get("source_file", "") + " " + r.get("context", "")).lower()
        match_count = sum(1 for w in query_keywords if w in path_text)
        if match_count > 0:
            r["score"] *= (1 + boost_per_match * match_count)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results




def identifier_boost(results: List[dict], query: str, boost_per_match: float = 1.0) -> List[dict]:
    """Boost exact code/API identifiers from the query in title/path/text head."""
    import re
    stop = {"http", "https", "request", "error", "code", "arkts", "ios"}
    raw_tokens = re.findall(r"[@A-Za-z_][A-Za-z0-9_@.:-]{2,}", query)
    tokens = []
    for t in raw_tokens:
        token = t.strip(".,;:()[]{}<>`'\"").lower()
        if token in stop:
            continue
        if len(token) >= 6 or "." in token or "@" in token or any(c.isupper() for c in t):
            tokens.append(token)
    if not tokens:
        return results

    for r in results:
        haystack = " ".join([
            r.get("context", ""),
            r.get("source_file", ""),
            (r.get("text", "") or "")[:2000],
        ]).lower()
        match_count = sum(1 for token in tokens if token in haystack)
        if match_count:
            r["score"] *= (1 + boost_per_match * match_count)
            r["identifier_matches"] = match_count

    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return results

# ── 混合检索（主入口）──

def search(
    query: str,
    top_k: int = 5,
    category: Optional[str] = None,
    has_code: Optional[bool] = None,
    method: str = "hybrid",
) -> List[dict]:
    """
    混合检索。

    method:
        "hybrid"    - BM25 + 语义 RRF 融合 + 路径加权（默认，推荐）
        "semantic"  - 纯语义
        "bm25"      - 纯 BM25
        "rerank"    - hybrid 后调用当前 domain 对应 rerank 模型重排
    """
    if method == "semantic":
        return _semantic_only(query, top_k, category, has_code)
    elif method == "bm25":
        return _bm25_only(query, top_k)
    elif method in ("rerank", "hybrid_rerank"):
        # Keep a wider first-stage pool so exact API docs are not dropped before rerank.
        candidates = _hybrid_search(query, max(top_k * 40, 200), category, has_code)
        return rerank_results(query, candidates, top_k=top_k)
    else:
        return _hybrid_search(query, top_k, category, has_code)


def _semantic_only(query, top_k, category, has_code) -> List[dict]:
    """纯语义检索。"""
    results = semantic_search(query, top_k=top_k, category=category, has_code=has_code)
    for r in results:
        r["score"] = r.pop("semantic_score")
    return results


def _bm25_only(query, top_k) -> List[dict]:
    """纯 BM25 检索。"""
    results = bm25_search(query, top_k=top_k)
    for r in results:
        r["score"] = r.pop("bm25_score")
    return results


def _hybrid_search(query, top_k, category, has_code) -> List[dict]:
    """
    混合检索：BM25 + 语义 RRF 融合 + 路径加权。
    """
    candidate_k = max(top_k * 4, 20)

    sem_results = semantic_search(query, top_k=candidate_k, category=category, has_code=has_code)
    bm25_results = bm25_search(query, top_k=candidate_k)

    # RRF 融合
    fused = rrf_fuse(
        semantic_results=sem_results,
        bm25_results=bm25_results,
        k=30,
        semantic_weight=1.0,
        bm25_weight=0.1,
        top_k=candidate_k,
        bm25_only_boost=1.0,
    )

    # 路径/标识符加权
    fused = path_boost(fused, query, boost_per_match=0.0)
    fused = identifier_boost(fused, query, boost_per_match=1.0)

    return fused[:top_k]


# ── Rerank ──

def rerank_results(query: str, results: List[dict], top_k: int = 5) -> List[dict]:
    """调用 aimodels rerank 接口，对候选文档重排。"""
    if not results:
        return []

    documents = [r["text"] for r in results]
    client = httpx.Client(timeout=120.0)
    resp = client.post(
        f"{RERANK_API_BASE}/api/v1/rerank",
        json={
            "query": query,
            "documents": documents,
            "top_k": min(top_k, len(documents)),
            "return_documents": False,
            # 当前 aimodels rerank endpoint 使用默认模型；这里保留字段，便于后续支持多 rerank 路由。
            "model": RERANK_MODEL_NAME,
        },
    )
    resp.raise_for_status()
    client.close()

    items = resp.json().get("items") or resp.json().get("data", {}).get("items", [])
    reranked: List[dict] = []
    for item in items:
        idx = item["index"]
        if 0 <= idx < len(results):
            r = results[idx].copy()
            r["rerank_score"] = float(item["score"])
            r["score"] = float(item["score"])
            r["rerank_model"] = RERANK_MODEL_NAME
            reranked.append(r)
    return reranked


# ── Prompt 构建 ──

def format_context(results: List[dict]) -> str:
    """将检索结果格式化为 LLM 上下文。"""
    parts = []
    for i, r in enumerate(results, 1):
        source = r["source_file"]
        context = r["context"]
        text = _clean_chunk_text(r['text'])
        parts.append(f"---\n[{i}] 来源: {source}\n分类: {context}\n\n{text}")
    return "\n\n".join(parts)


def _clean_chunk_text(text: str) -> str:
    """Remove navigation/frontmatter noise from the beginning of chunk text."""
    import re
    # Remove leading "文档: ..." line and following blank lines
    text = re.sub(r'^文档:.*\n+', '', text)
    # Remove YAML frontmatter blocks (--- ... ---)
    text = re.sub(r'^---\n.*?---\n+', '', text, flags=re.DOTALL)
    # Remove breadcrumb navigation: lines starting with "- " followed by blank line
    # Matches sequences like:
    #   - [UIKit](...)
    #   - [Accessibility](...)
    #   - UIAccessibility
    text = re.sub(r'^(?:- (?:\[.*?\]\(.*?\)|[^\n]+)\n)+\n?', '', text)
    # Remove "Framework" standalone line (common in Apple docs)
    text = re.sub(r'^Framework\n+', '', text)
    # Remove leading blank lines
    text = text.lstrip('\n')
    return text


def rag_query(
    question: str,
    top_k: int = 5,
    category: Optional[str] = None,
    has_code: Optional[bool] = None,
    method: str = "hybrid",
) -> dict:
    """RAG 查询：检索 + 构建 prompt。"""
    results = search(question, top_k=top_k, category=category, has_code=has_code, method=method)
    context = format_context(results)

    prompt = f"""你是{PROMPT_ROLE}。基于以下参考文档回答用户问题。

参考文档：
{context}

用户问题：{question}

请基于参考文档回答。如果文档中有代码示例，请一并给出。如果文档中没有相关信息，请说明。"""

    return {
        "question": question,
        "results": results,
        "context": context,
        "prompt": prompt,
    }


# ── CLI ──

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    question = sys.argv[1] if len(sys.argv) > 1 else "ArkTS 怎么创建一个按钮组件"
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    method = sys.argv[3] if len(sys.argv) > 3 else "hybrid"

    result = rag_query(question, top_k=top_k, method=method)

    print(f"\n问题: {result['question']}")
    print(f"领域: {ACTIVE_DOMAIN}")
    print(f"Embedding: {EMBEDDING_MODEL_NAME}")
    print(f"Rerank: {RERANK_MODEL_NAME}")
    print(f"检索方法: {method}")
    print(f"检索到 {len(result['results'])} 个相关文档:\n")
    for i, r in enumerate(result["results"], 1):
        print(f"[{i}] score={r['score']:.4f} | {r['source_file']}")
        print(f"    context: {r['context']}")
        print(f"    has_code: {r['has_code']}")
        print(f"    text preview: {r['text'][:150]}...")
        print()
