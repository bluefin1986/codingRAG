"""Per-domain RAG query engine.

Handles per-request domain switching by maintaining its own config and BM25
index cache, independent of the module-level globals in config.py / retriever.py.

Reuses pure helper functions (rrf_fuse, path_boost, rerank_results, format_context)
from indexer.retriever — no code duplication.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from config import DOMAIN_REGISTRY, get_domain_config

# Reuse pure helpers that don't depend on module-level config
from indexer.retriever import (
    format_context,
    identifier_boost,
    path_boost,
    rerank_results,
    rrf_fuse,
)

logger = logging.getLogger(__name__)

# ── Per-domain BM25 index cache ──
# Keyed by domain name; each entry is (bm25_instance, chunks_list) or None.
_BM25_CACHE: Dict[str, tuple] = {}


def _bm25_search_text(record: dict) -> str:
    """Build a compact BM25 document to avoid tokenizing huge chunks online."""
    meta = record.get("metadata", {}) or {}
    text = record.get("text", "") or ""
    return " ".join([
        str(meta.get("context", "")),
        str(meta.get("source_file", "")),
        text[:1200],
    ])


def _load_bm25_for_domain(cfg: Dict[str, Any]) -> tuple:
    """Load and cache BM25 index for a specific domain."""
    domain = cfg["domain"]
    if domain in _BM25_CACHE:
        return _BM25_CACHE[domain]

    chunks_path = cfg["output_dir"] / "chunks.jsonl"
    if not chunks_path.exists():
        logger.warning("chunks file not found for domain %s: %s", domain, chunks_path)
        _BM25_CACHE[domain] = (None, [])
        return None, []

    import jieba
    from rank_bm25 import BM25Okapi

    t0 = time.time()
    chunks: list = []
    texts: list = []
    with open(chunks_path, encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            chunks.append(record)
            texts.append(_bm25_search_text(record))

    tokenized = [list(jieba.cut(t)) for t in texts]
    bm25 = BM25Okapi(tokenized)
    elapsed = time.time() - t0
    logger.info("BM25 index loaded for domain %s: %d chunks in %.1fs", domain, len(chunks), elapsed)

    _BM25_CACHE[domain] = (bm25, chunks)
    return bm25, chunks


class DomainQueryEngine:
    """RAG query engine that operates on a specific domain config.

    Unlike the module-level retriever, this class carries its own config dict,
    so multiple domains can coexist without import-time conflicts.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.domain = cfg["domain"]
        self.collection = cfg["collection"]
        self.embedding_api_base = cfg.get("embedding_api_base", "http://localhost:8030")
        self.embedding_model_name = cfg["embedding_model_name"]
        self.rerank_api_base = cfg.get("rerank_api_base", self.embedding_api_base)
        self.rerank_model_name = cfg["rerank_model_name"]
        self.qdrant_host = cfg.get("qdrant_host", "localhost")
        self.qdrant_port = cfg.get("qdrant_port", 6333)
        self.prompt_role = cfg["prompt_role"]
        self.bm25_enabled = bool(cfg.get("bm25_enabled", True))
        self.bm25_weight = float(cfg.get("bm25_weight", 0.7))
        self.path_boost_per_match = float(cfg.get("path_boost_per_match", 0.2))

    # ── Embedding ──

    def embed_query(self, query: str) -> List[float]:
        import re
        # Normalize whitespace: collapse newlines/spaces, strip
        query = re.sub(r'\s+', ' ', query).strip()
        client = httpx.Client(timeout=60.0)
        resp = client.post(
            f"{self.embedding_api_base}/api/v1/embeddings",
            json={"input": [query], "model": self.embedding_model_name},
        )
        resp.raise_for_status()
        client.close()
        return resp.json()["data"][0]["embedding"]

    # ── BM25 ──

    def bm25_search(self, query: str, top_k: int = 20) -> List[dict]:
        import jieba

        if not self.bm25_enabled:
            logger.info("BM25 disabled for domain=%s", self.domain)
            return []

        bm25, chunks = _load_bm25_for_domain(self.cfg)
        if bm25 is None:
            return []

        query_tokens = list(jieba.cut(query))
        scores = bm25.get_scores(query_tokens)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

        results = []
        for idx in top_indices:
            if scores[idx] <= 0:
                break
            record = chunks[idx]
            meta = record.get("metadata", {})
            results.append({
                "bm25_score": float(scores[idx]),
                "domain": meta.get("domain", self.domain),
                "text": record["text"],
                "context": meta.get("context", ""),
                "source_file": meta.get("source_file", ""),
                "has_code": meta.get("has_code", False),
                "chunk_index": idx,
            })
        return results

    # ── Semantic search ──

    def semantic_search(
        self,
        query: str,
        top_k: int = 20,
        category: Optional[str] = None,
        has_code: Optional[bool] = None,
    ) -> List[dict]:
        query_vector = self.embed_query(query)

        must_filters = []
        if category:
            must_filters.append({"key": "category", "match": {"value": category}})
        if has_code is not None:
            must_filters.append({"key": "has_code", "match": {"value": has_code}})

        search_body: dict[str, Any] = {
            "vector": query_vector,
            "limit": top_k,
            "with_payload": True,
            "with_vector": False,
        }
        if must_filters:
            search_body["filter"] = {"must": must_filters}

        client = httpx.Client(timeout=30.0)
        resp = client.post(
            f"http://{self.qdrant_host}:{self.qdrant_port}/collections/{self.collection}/points/search",
            json=search_body,
        )
        resp.raise_for_status()
        client.close()

        results = []
        for hit in resp.json()["result"]:
            results.append({
                "semantic_score": hit["score"],
                "domain": hit["payload"].get("domain", self.domain),
                "text": hit["payload"]["text"],
                "context": hit["payload"].get("context", ""),
                "source_file": hit["payload"].get("source_file", ""),
                "has_code": hit["payload"].get("has_code", False),
            })
        return results

    # ── Search orchestration ──

    def search(
        self,
        query: str,
        top_k: int = 5,
        category: Optional[str] = None,
        has_code: Optional[bool] = None,
        method: str = "hybrid",
    ) -> List[dict]:
        logger.info(
            "RAG_SEARCH_START domain=%s collection=%s method=%s top_k=%s category=%s has_code=%s query=%r",
            self.domain,
            self.collection,
            method,
            top_k,
            category,
            has_code,
            query[:200],
        )

        if method == "semantic":
            results = self.semantic_search(query, top_k=top_k, category=category, has_code=has_code)
            for r in results:
                r["score"] = r.pop("semantic_score")
            self._log_stage("semantic", results)
            return results

        if method == "bm25":
            results = self.bm25_search(query, top_k=top_k)
            for r in results:
                r["score"] = r.pop("bm25_score")
            self._log_stage("bm25", results)
            return results

        # hybrid or rerank: run both, fuse, then optionally rerank
        candidate_k = max(top_k * 4, 20)
        sem_results = self.semantic_search(query, top_k=candidate_k, category=category, has_code=has_code)
        bm25_results = self.bm25_search(query, top_k=candidate_k)
        self._log_stage("semantic_candidates", sem_results)
        self._log_stage("bm25_candidates", bm25_results)

        if not bm25_results:
            fused = []
            for r in sem_results[:candidate_k]:
                item = r.copy()
                item["score"] = item.pop("semantic_score", item.get("score", 0.0))
                fused.append(item)
        else:
            fused = rrf_fuse(
                semantic_results=sem_results,
                bm25_results=bm25_results,
                k=30,
                semantic_weight=1.0,
                bm25_weight=self.bm25_weight,
                top_k=candidate_k,
                bm25_only_boost=1.0,
            )
            fused = path_boost(fused, query, boost_per_match=self.path_boost_per_match)
            fused = identifier_boost(fused, query, boost_per_match=1.0)
        self._log_stage("fusion", fused)

        if method in ("rerank", "hybrid_rerank"):
            reranked = rerank_results(query, fused, top_k=top_k)
            self._log_stage("rerank", reranked)
            return reranked
        return fused[:top_k]

    # ── Diagnostics ──

    def _log_stage(self, stage: str, results: List[dict], limit: int = 5) -> None:
        """Log compact retrieval diagnostics without dumping full document text."""
        summary = []
        for i, r in enumerate(results[:limit], 1):
            summary.append({
                "rank": i,
                "score": round(float(r.get("score", r.get("semantic_score", r.get("bm25_score", 0.0))) or 0.0), 4),
                "context": r.get("context", "")[:120],
                "source_file": r.get("source_file", ""),
                "text_len": len(r.get("text", "") or ""),
            })
        logger.info("RAG_STAGE domain=%s stage=%s count=%d top=%s", self.domain, stage, len(results), summary)

    # ── Full RAG query ──

    def rag_query(
        self,
        question: str,
        top_k: int = 5,
        category: Optional[str] = None,
        has_code: Optional[bool] = None,
        method: str = "hybrid",
    ) -> dict:
        results = self.search(question, top_k=top_k, category=category, has_code=has_code, method=method)
        context = format_context(results)
        logger.info(
            "RAG_CONTEXT domain=%s method=%s result_count=%d context_chars=%d",
            self.domain,
            method,
            len(results),
            len(context),
        )

        prompt = f"""你是{self.prompt_role}。基于以下参考文档回答用户问题。

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
