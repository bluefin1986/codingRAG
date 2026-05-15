"""Per-domain RAG query engine.

Handles per-request domain switching by maintaining its own config and BM25
index cache, independent of the module-level globals in config.py / retriever.py.

Reuses pure helper functions (rrf_fuse, path_boost, rerank_results, format_context)
from indexer.retriever — no code duplication.
"""
from __future__ import annotations

import json
import logging
import os
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
    _STOPWORDS,
)

logger = logging.getLogger(__name__)

# ── Per-domain BM25 index cache ──
# Keyed by domain name; each entry is (bm25_instance, chunks_list) or None.
_BM25_CACHE: Dict[str, tuple] = {}


def _qdrant_headers() -> Dict[str, str]:
    """Build Qdrant request headers, including API key when configured."""
    api_key = os.getenv("CODING_RAG_QDRANT_API_KEY")
    return {"api-key": api_key} if api_key else {}


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

    import jieba
    from rank_bm25 import BM25Okapi

    t0 = time.time()
    chunks: list = []
    texts: list = []

    if chunks_path.exists():
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                chunks.append(record)
                texts.append(_bm25_search_text(record))
    else:
        logger.warning(
            "chunks file not found for domain %s: %s; falling back to Qdrant payload scroll",
            domain,
            chunks_path,
        )
        chunks = _load_chunks_from_qdrant(cfg)
        if chunks:
            chunks_path.parent.mkdir(parents=True, exist_ok=True)
            with open(chunks_path, "w", encoding="utf-8") as f:
                for record in chunks:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info("persisted %d Qdrant payload chunks to %s", len(chunks), chunks_path)
        texts = [_bm25_search_text(record) for record in chunks]

    if not chunks:
        logger.warning("no chunks available for domain %s, BM25 disabled", domain)
        _BM25_CACHE[domain] = (None, [])
        return None, []

    logger.info("BM25 tokenizing start for domain %s: %d chunks", domain, len(texts))
    t_tokenize = time.time()
    tokenized = []
    for i, text in enumerate(texts, 1):
        tokenized.append(list(jieba.cut(text)))
        if i % 10000 == 0 or i == len(texts):
            logger.info(
                "BM25 tokenizing progress domain=%s chunks=%d/%d elapsed=%.1fs",
                domain,
                i,
                len(texts),
                time.time() - t_tokenize,
            )

    logger.info(
        "BM25 building index start for domain %s: %d tokenized chunks tokenized_elapsed=%.1fs",
        domain,
        len(tokenized),
        time.time() - t_tokenize,
    )
    t_build = time.time()
    bm25 = BM25Okapi(tokenized)
    elapsed = time.time() - t0
    logger.info(
        "BM25 index loaded for domain %s: %d chunks in %.1fs build_elapsed=%.1fs",
        domain,
        len(chunks),
        elapsed,
        time.time() - t_build,
    )

    _BM25_CACHE[domain] = (bm25, chunks)
    return bm25, chunks


def _load_chunks_from_qdrant(cfg: Dict[str, Any]) -> list:
    """Load BM25 source records from Qdrant payloads when local chunks.jsonl is absent."""
    collection = cfg["collection"]
    host = cfg.get("qdrant_host", "localhost")
    port = cfg.get("qdrant_port", 6333)
    url = f"http://{host}:{port}/collections/{collection}/points/scroll"
    chunks: list = []
    offset = None

    with httpx.Client(timeout=60.0) as client:
        while True:
            body: Dict[str, Any] = {
                "limit": 4096,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset

            resp = client.post(url, json=body, headers=_qdrant_headers())
            resp.raise_for_status()
            data = resp.json().get("result", {})
            points = data.get("points", [])

            for point in points:
                payload = point.get("payload") or {}
                text = payload.get("text") or ""
                if not text:
                    continue
                chunks.append({
                    "text": text,
                    "metadata": {
                        "domain": payload.get("domain", cfg["domain"]),
                        "context": payload.get("context", ""),
                        "source_file": payload.get("source_file", ""),
                        "chunk_index": payload.get("chunk_index", 0),
                        "has_code": payload.get("has_code", False),
                        "category": payload.get("category", ""),
                    },
                })

            offset = data.get("next_page_offset")
            if not offset:
                break

    logger.info("loaded %d chunks from Qdrant collection=%s for BM25", len(chunks), collection)
    return chunks


def _result_key(r: dict) -> tuple:
    return (r.get("source_file", ""), r.get("chunk_index", r.get("context", "")))


def _query_high_idf_terms(query: str, bm25: Any, min_idf: float = 5.0) -> list[str]:
    """Return discriminative query terms according to the current collection IDF."""
    import jieba
    generic_stop = {"怎么", "如何", "什么", "为何", "为什么", "是否", "怎样", "的", "了", "和", "与", "或", "在", "是", "有", "中"}
    terms: list[str] = []
    seen = set()
    for raw in jieba.cut(query):
        term = raw.strip().lower()
        if len(term) < 2 or term in generic_stop:
            continue
        idf = float(getattr(bm25, "idf", {}).get(raw, getattr(bm25, "idf", {}).get(term, 0.0)) or 0.0)
        if idf >= min_idf and term not in seen:
            terms.append(term)
            seen.add(term)
    return terms


def _is_explanatory_query(query: str) -> bool:
    q = query.lower()
    markers = ("为什么", "为何", "原因", "原理", "背景", "体系", "架构", "不再支持", "不支持")
    return any(m in q for m in markers)


def _is_comparison_query(query: str) -> bool:
    q = query.lower()
    markers = ("区别", "差异", "对比", "比较", " vs ", " versus ")
    return any(m in q for m in markers)


def _drop_obvious_noise(results: List[dict], query: str = "", threshold: float = -3.0) -> List[dict]:
    """Drop very-negative rerank results that are clearly off-topic.

    Keeps results with rerank_score >= threshold, or whose source_file path
    contains any non-stopword from the query (covers the "other side" of
    comparison queries where the reranker underranks one concept).
    """
    import re as _re
    q_terms = set(_re.findall(r'[\w]+', query.lower())) - _STOPWORDS if query else set()
    def _path_hits(r: dict) -> bool:
        src = r.get("source_file", "").lower()
        return bool(q_terms and any(t in src for t in q_terms))
    kept = [
        r for r in results
        if float(r.get("rerank_score", r.get("score", 0.0)) or 0.0) >= threshold
        or int(r.get("field_match_terms", 0) or 0) > 0
        or _path_hits(r)
    ]
    return kept or results[:1]


def _field_match_rerank(results: List[dict], query: str, bm25: Any) -> List[dict]:
    """Promote results whose title/path matches discriminative query terms.

    This is collection-driven via BM25 IDF, not a domain-specific stopword list.
    It separates title/path API hits from broad body-only examples.
    """
    terms = _query_high_idf_terms(query, bm25)
    if not terms:
        return results

    def field_score(r: dict) -> int:
        field_text = f"{r.get('context', '')} {r.get('source_file', '')}".lower()
        return sum(1 for t in terms if t in field_text)

    for r in results:
        r["field_match_terms"] = field_score(r)

    if not any(r.get("field_match_terms", 0) for r in results):
        return results

    for r in results:
        base = float(r.get("rerank_score", r.get("score", 0.0)) or 0.0)
        field = int(r.get("field_match_terms", 0) or 0)
        bm25_rank = r.get("bm25_rank")
        bm25_rank_bonus = (20.0 / float(bm25_rank)) if field and bm25_rank else 0.0
        # Keep score monotonic for logs/response while preserving rerank_score separately.
        r["score"] = base + field * 2.0 + bm25_rank_bonus

    results.sort(key=lambda r: float(r.get("score", 0.0) or 0.0), reverse=True)
    return results


def _chunk_lookup(chunks: list) -> tuple[dict[tuple, dict], dict[str, list[dict]]]:
    by_key: dict[tuple, dict] = {}
    by_source: dict[str, list[dict]] = {}
    for pos, record in enumerate(chunks):
        meta = record.get("metadata", {}) or {}
        source = meta.get("source_file", "")
        chunk_idx = meta.get("chunk_index", pos)
        item = {"record": record, "pos": pos, "source_file": source, "chunk_index": chunk_idx}
        by_key[(source, chunk_idx)] = item
        by_source.setdefault(source, []).append(item)
    for items in by_source.values():
        items.sort(key=lambda x: (x.get("chunk_index", 0), x.get("pos", 0)))
    return by_key, by_source


def _expand_result_context(results: List[dict], cfg: Dict[str, Any], window: int = 1, max_chars_per_result: int = 6000) -> List[dict]:
    """Expand each hit with adjacent chunks from the same source document.

    The API still returns topK results, but each result.text becomes a more useful
    local document window for the LLM. This avoids forcing clients to read md files.
    """
    _, chunks = _load_bm25_for_domain(cfg)
    if not chunks:
        return results

    by_key, by_source = _chunk_lookup(chunks)
    expanded: list[dict] = []
    for r in results:
        source = r.get("source_file", "")
        chunk_idx = r.get("chunk_index")
        item = by_key.get((source, chunk_idx)) if chunk_idx is not None else None
        if item is None:
            # Fallback for legacy results without chunk_index.
            for candidate in by_source.get(source, []):
                meta = candidate["record"].get("metadata", {}) or {}
                if meta.get("context", "") == r.get("context", ""):
                    item = candidate
                    break
        if item is None:
            expanded.append(r)
            continue

        source_items = by_source.get(source, [])
        center_i = next((i for i, candidate in enumerate(source_items) if candidate is item), -1)
        if center_i < 0:
            expanded.append(r)
            continue

        selected = source_items[max(0, center_i - window): center_i + window + 1]
        texts = []
        contexts = []
        total = 0
        for selected_item in selected:
            record = selected_item["record"]
            text = record.get("text", "") or ""
            meta = record.get("metadata", {}) or {}
            if not text:
                continue
            if total + len(text) > max_chars_per_result and texts:
                break
            texts.append(text)
            contexts.append(meta.get("context", ""))
            total += len(text)

        new_r = r.copy()
        if texts:
            new_r["text"] = "\n\n".join(texts)
            new_r["expanded_chunks"] = len(texts)
            new_r["expanded_contexts"] = contexts
        expanded.append(new_r)

    logger.info(
        "RAG_CONTEXT_EXPAND domain=%s results=%d expanded=%s",
        cfg.get("domain"),
        len(expanded),
        [
            {
                "source_file": r.get("source_file", ""),
                "context": r.get("context", "")[:80],
                "expanded_chunks": r.get("expanded_chunks", 1),
                "text_len": len(r.get("text", "") or ""),
            }
            for r in expanded[:5]
        ],
    )
    return expanded


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
        endpoint = f"{self.embedding_api_base}/api/v1/embeddings"
        payload = {"input": [query], "model": self.embedding_model_name}

        logger.info(
            "EMBED_QUERY_REQUEST endpoint=%s domain=%s collection=%s model=%s query=%r",
            endpoint,
            self.domain,
            self.collection,
            self.embedding_model_name,
            query,
        )

        with httpx.Client(timeout=60.0) as client:
            try:
                resp = client.post(endpoint, json=payload)
                logger.info(
                    "EMBED_QUERY_RESPONSE endpoint=%s status=%s model=%s body_preview=%s",
                    endpoint,
                    resp.status_code,
                    self.embedding_model_name,
                    resp.text[:500],
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                response = exc.response
                logger.error(
                    "EMBED_QUERY_HTTP_ERROR endpoint=%s status=%s model=%s domain=%s body_preview=%s",
                    endpoint,
                    response.status_code,
                    self.embedding_model_name,
                    self.domain,
                    response.text[:1000],
                )
                raise
            except httpx.RequestError as exc:
                logger.error(
                    "EMBED_QUERY_REQUEST_ERROR endpoint=%s model=%s domain=%s error=%s",
                    endpoint,
                    self.embedding_model_name,
                    self.domain,
                    exc,
                )
                raise

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
                "chunk_index": meta.get("chunk_index", idx),
                "chunk_pos": idx,
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
            headers=_qdrant_headers(),
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
                "chunk_index": hit["payload"].get("chunk_index", 0),
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

        # hybrid or rerank: run both, fuse, then optionally rerank.
        # Rerank needs a wider candidate pool; otherwise weak first-stage fusion can
        # drop exact API docs before the reranker sees them.
        candidate_k = max(top_k * 40, 200) if method in ("rerank", "hybrid_rerank") else max(top_k * 4, 20)
        sem_results = self.semantic_search(query, top_k=candidate_k, category=category, has_code=has_code)
        bm25_results = self.bm25_search(query, top_k=candidate_k)
        bm25_meta = {
            _result_key(r): {"bm25_rank": rank, "bm25_score": r.get("bm25_score", 0.0)}
            for rank, r in enumerate(bm25_results, 1)
        }
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
        for item in fused:
            item.update(bm25_meta.get(_result_key(item), {}))
        self._log_stage("fusion", fused)

        if method in ("rerank", "hybrid_rerank"):
            # Ask reranker for extra candidates, then filter noise + deduplicate.
            # Comparison queries need balanced docs from both sides; skip field boost
            # to avoid title-only API matches crowding out the paired concept guide.
            is_comparison = _is_comparison_query(query)
            reranked = rerank_results(query, fused, top_k=max(top_k * 3, 15))
            bm25, _ = _load_bm25_for_domain(self.cfg)
            if bm25 is not None and not _is_explanatory_query(query) and not is_comparison:
                reranked = _field_match_rerank(reranked, query, bm25)
            # Always filter obvious noise regardless of query type.
            reranked = _drop_obvious_noise(reranked, query)
            # Deduplicate by source_file — keep only the best scoring chunk per file
            # so the top-k covers more distinct documents.
            seen_sources: set[str] = set()
            deduped: list[dict] = []
            for r in reranked:
                src = r.get("source_file", "")
                if src not in seen_sources:
                    seen_sources.add(src)
                    deduped.append(r)
            reranked = deduped[:top_k]
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
        results = _expand_result_context(results, self.cfg)
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
