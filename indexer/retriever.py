"""RAG 查询模块：混合检索（语义 + BM25） + LLM 回答"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import httpx

from config import (
    CHUNKS_FILE,
    COLLECTION_NAME,
    EMBEDDING_API_BASE,
    EMBEDDING_MODEL_NAME,
    QDRANT_HOST,
    QDRANT_PORT,
)

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


# ── BM25 本地检索 ──

import jieba
from rank_bm25 import BM25Okapi


@lru_cache(maxsize=1)
def _load_bm25_index():
    """加载 chunks 并构建 BM25 索引（懒加载，只执行一次）。"""
    import time
    t0 = time.time()

    chunks_path = CHUNKS_FILE
    if not chunks_path.exists():
        logger.warning("chunks file not found: %s, BM25 disabled", chunks_path)
        return None, []

    chunks = []
    texts = []
    with open(chunks_path, encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            chunks.append(record)
            # 取 text 字段做分词
            texts.append(record["text"])

    # jieba 分词
    tokenized = [list(jieba.cut(t)) for t in texts]
    bm25 = BM25Okapi(tokenized)

    elapsed = time.time() - t0
    logger.info("BM25 index loaded: %d chunks in %.1fs", len(chunks), elapsed)
    return bm25, chunks


def bm25_search(query: str, top_k: int = 20) -> List[dict]:
    """BM25 关键词检索。"""
    bm25, chunks = _load_bm25_index()
    if bm25 is None:
        return []

    query_tokens = list(jieba.cut(query))
    scores = bm25.get_scores(query_tokens)

    # 取 top_k 的索引
    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] <= 0:
            break
        record = chunks[idx]
        meta = record.get("metadata", {})
        results.append({
            "bm25_score": float(scores[idx]),
            "text": record["text"],
            "context": meta.get("context", ""),
            "source_file": meta.get("source_file", ""),
            "has_code": meta.get("has_code", False),
            "chunk_index": idx,
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
    k: int = 60,
    semantic_weight: float = 1.0,
    bm25_weight: float = 1.0,
    top_k: int = 5,
) -> List[dict]:
    """
    Reciprocal Rank Fusion 融合两路结果。

    RRF score = Σ weight / (k + rank)
    k=60 是论文推荐值。
    """
    # 用 source_file + chunk_index 做去重 key
    def _key(r):
        return (r.get("source_file", ""), r.get("chunk_index", 0))

    # 收集两路各自的 key 集合
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

    # 仅 BM25 命中（语义未命中）的结果额外加权：+20%
    # 这些是关键词精确匹配但语义漂移的文档，往往是真正相关的
    bm25_only_boost = 1.2
    for key in bm25_keys - sem_keys:
        rrf_scores[key] *= bm25_only_boost

    # 按 RRF 分数排序
    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)

    results = []
    for key in sorted_keys[:top_k]:
        r = merged[key].copy()
        r["score"] = rrf_scores[key]
        # 去掉内部字段
        r.pop("bm25_score", None)
        r.pop("semantic_score", None)
        r.pop("chunk_index", None)
        results.append(r)

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
    混合检索：语义 + BM25，RRF 融合。

    method:
        "hybrid"   - 语义 + BM25 融合（默认）
        "semantic" - 纯语义
        "bm25"     - 纯 BM25
    """
    if method == "semantic":
        return _semantic_only(query, top_k, category, has_code)
    elif method == "bm25":
        return _bm25_only(query, top_k)
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
    """混合检索：语义 + BM25 + 路径加权。"""
    candidate_k = max(top_k * 4, 20)

    sem_results = semantic_search(query, top_k=candidate_k, category=category, has_code=has_code)
    bm25_results = bm25_search(query, top_k=candidate_k)

    # RRF 融合
    fused = rrf_fuse(
        semantic_results=sem_results,
        bm25_results=bm25_results,
        k=30,
        semantic_weight=1.0,
        bm25_weight=2.0,
        top_k=candidate_k,
    )

    # 路径/标题加权：用 jieba 分词后，query 关键词在 source_file 或 context 中出现则加权
    import jieba as _jieba
    query_tokens = list(_jieba.cut(query))
    # 去掉停用词和过短的词
    query_keywords = [t for t in query_tokens if len(t) >= 2 and t not in ('怎么', '如何', '什么', '创建', '使用', '实现')]

    for r in fused:
        path_text = r.get("source_file", "") + " " + r.get("context", "")
        match_count = sum(1 for w in query_keywords if w in path_text)
        if match_count > 0:
            r["score"] *= (1 + 0.8 * match_count)  # 每命中一个词 +80%

    # 重新排序
    fused.sort(key=lambda r: r["score"], reverse=True)
    return fused[:top_k]


# ── Prompt 构建 ──

def format_context(results: List[dict]) -> str:
    """将检索结果格式化为 LLM 上下文。"""
    parts = []
    for i, r in enumerate(results, 1):
        source = r["source_file"]
        context = r["context"]
        parts.append(f"---\n[{i}] 来源: {source}\n分类: {context}\n\n{r['text']}")
    return "\n\n".join(parts)


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

    prompt = f"""你是鸿蒙开发专家。基于以下参考文档回答用户问题。

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
    print(f"检索方法: {method}")
    print(f"检索到 {len(result['results'])} 个相关文档:\n")
    for i, r in enumerate(result["results"], 1):
        print(f"[{i}] score={r['score']:.4f} | {r['source_file']}")
        print(f"    context: {r['context']}")
        print(f"    has_code: {r['has_code']}")
        print(f"    text preview: {r['text'][:150]}...")
        print()
