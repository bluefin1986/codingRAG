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

from config import BM25_MAX_CHUNKS

# Reuse pure helpers that don't depend on module-level config
from indexer.retriever import (
    format_context,
    identifier_boost,
    path_boost,
    rerank_results,
    rrf_fuse,
    _STOPWORDS,
)
from indexer.local_bm25_searcher import LocalBM25Searcher
from indexer.es_searcher import ESSearcher

logger = logging.getLogger(__name__)

# ── Per-domain keyword searcher cache ──
# Keyed by domain name; each entry is (keyword_searcher, chunks_list) or (None, []).
_KEYWORD_SEARCHER_CACHE: Dict[str, tuple] = {}


def _qdrant_headers() -> Dict[str, str]:
    """Build Qdrant request headers, including API key when configured."""
    api_key = os.getenv("CODING_RAG_QDRANT_API_KEY")
    return {"api-key": api_key} if api_key else {}


def _load_keyword_searcher_for_domain(cfg: Dict[str, Any]) -> tuple:
    """Load and cache keyword searcher for a specific domain."""
    domain = cfg["domain"]
    backend = os.getenv("CODING_RAG_KEYWORD_BACKEND", cfg.get("keyword_backend", "local_bm25")).strip().lower()
    cache_key = f"{domain}:{backend}"

    if cache_key in _KEYWORD_SEARCHER_CACHE:
        return _KEYWORD_SEARCHER_CACHE[cache_key]

    if backend in ("elasticsearch", "opensearch", "es"):
        base_url = os.getenv("CODING_RAG_ES_URL", cfg.get("es_url", "")).strip()
        if not base_url:
            logger.warning(
                "keyword backend=%s selected for domain=%s but CODING_RAG_ES_URL is empty; falling back to local BM25",
                backend,
                domain,
            )
        else:
            index_name = os.getenv(
                f"CODING_RAG_ES_INDEX_{domain.upper()}",
                cfg.get("es_index") or f"codingrag_{domain}_docs",
            )
            searcher = ESSearcher(
                domain=domain,
                index_name=index_name,
                base_url=base_url,
                api_key=os.getenv("CODING_RAG_ES_API_KEY"),
                config=cfg,
            )
            logger.info(
                "keyword searcher loaded for domain %s backend=%s index=%s url=%s",
                domain,
                backend,
                index_name,
                base_url,
            )
            _KEYWORD_SEARCHER_CACHE[cache_key] = (searcher, [])
            return searcher, []

    chunks = _load_chunks_for_keyword_search(cfg)
    if not chunks:
        logger.warning("no chunks available for domain %s, keyword search disabled", domain)
        _KEYWORD_SEARCHER_CACHE[cache_key] = (None, [])
        return None, []

    searcher = LocalBM25Searcher(domain=domain, chunks=chunks, config=cfg)
    logger.info("keyword searcher loaded for domain %s backend=local_bm25 chunks=%d", domain, len(chunks))

    _KEYWORD_SEARCHER_CACHE[cache_key] = (searcher, chunks)
    return searcher, chunks


def _load_chunks_for_keyword_search(cfg: Dict[str, Any]) -> list:
    """Load source records used by keyword search."""
    chunks_path = cfg["output_dir"] / "chunks.jsonl"
    domain = cfg["domain"]

    if chunks_path.exists():
        started = time.perf_counter()
        logger.info("local BM25 chunks load start domain=%s path=%s", domain, chunks_path)
        chunks: list = []
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                meta = record.get("metadata", {}) or {}
                text = record.get("text", "") or ""
                keyword_text = " ".join([
                    str(meta.get("context", "")),
                    str(meta.get("source_file", "")),
                    text[:1200],
                ])
                compact_record = dict(record)
                compact_record["text"] = keyword_text
                compact_record["original_text"] = text
                chunks.append(compact_record)

        # ── BM25 内存安全阈值 ──
        # 当 chunk 数量超过 BM25_MAX_CHUNKS（默认 50000）时，
        # 仅保留前 N 条，避免 HarmonyOS ~90k chunks 导致 OOM。
        if len(chunks) > BM25_MAX_CHUNKS:
            logger.warning(
                "local BM25 chunk count %d exceeds safety cap %d for domain=%s; "
                "truncating to first %d chunks. Set CODING_RAG_BM25_MAX_CHUNKS to override.",
                len(chunks), BM25_MAX_CHUNKS, domain, BM25_MAX_CHUNKS,
            )
            chunks = chunks[:BM25_MAX_CHUNKS]

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info("local BM25 chunks load done domain=%s chunks=%d elapsedMs=%d", domain, len(chunks), elapsed_ms)
        return chunks

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
    return chunks


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


def _extract_technical_symbols(query: str, cfg: Optional[Dict[str, Any]] = None) -> list[tuple[str, float]]:
    """Extract technical symbols from a query, returning (symbol, weight) pairs.

    If cfg contains a ``known_identifiers`` list, matching tokens get weight 2.0
    instead of the default 1.0.  This is intentionally domain-aware so that
    ArkUI decorators like @Component get a bigger boost than generic camelCase tokens.
    """
    import re

    generic_stop = {
        "http", "https", "request", "error", "code", "docs", "usage", "config",
        "configuration", "command", "directive", "nginx", "redis", "kafka", "ios", "arkui",
    }
    known_set: set[str] = set()
    if cfg:
        for ident in cfg.get("known_identifiers", []):
            known_set.add(ident.lower())
            # Also add the bare word (e.g. "ListComponent" → "list")
            bare = re.sub(r"[^A-Za-z0-9]", "", ident).lower()
            if bare:
                known_set.add(bare)

    raw_tokens = re.findall(r"[@A-Za-z_][A-Za-z0-9_@./:-]{2,}", query)
    symbols: list[tuple[str, float]] = []
    seen: set[str] = set()
    for raw in raw_tokens:
        token = raw.strip(".,;:()[]{}<>`'\"").lower()
        if len(token) < 3 or token in generic_stop or token in seen:
            continue
        has_symbol_shape = (
            "_" in token
            or "." in token
            or "@" in token
            or "/" in token
            or "-" in token
            or any(c.isupper() for c in raw)
            or raw.isupper()
        )
        if has_symbol_shape:
            weight = 2.0 if token in known_set else 1.0
            symbols.append((token, weight))
            seen.add(token)
    return symbols


# Backward-compatible wrapper: returns list[str] as before.
def _extract_technical_symbols_simple(query: str) -> list[str]:
    return [s for s, _w in _extract_technical_symbols(query)]


def expand_query(query: str, domain: str) -> tuple[str, list[str]]:
    """Expand a query with domain-specific synonym / expansion terms.

    Returns (expanded_query_for_bm25, list_of_expanded_terms).
    The original query is preserved for semantic search; expanded terms are
    appended only for BM25 keyword search.
    """
    from api.registry import query_expansion_cache

    expansions_map = query_expansion_cache.get_expansions(domain)

    # Start with the symbols from the query
    symbols = _extract_technical_symbols_simple(query)
    expanded_terms: list[str] = []
    seen_expansions: set[str] = set()

    for symbol in symbols:
        for key, expansion_terms in expansions_map.items():
            if key.lower() == symbol or key.lower() in symbol:
                for term in expansion_terms:
                    term_lower = term.lower()
                    if term_lower not in seen_expansions and term_lower not in {s.lower() for s in symbols}:
                        expanded_terms.append(term)
                        seen_expansions.add(term_lower)

    # Also check raw query tokens against expansion keys (e.g. "ArkUI" in query text)
    import re
    raw_tokens = re.findall(r"[@A-Za-z_][A-Za-z0-9_@./:-]{2,}", query)
    for raw in raw_tokens:
        raw_lower = raw.lower()
        if raw_lower in seen_expansions:
            continue
        for key, expansion_terms in expansions_map.items():
            if key.lower() == raw_lower:
                for term in expansion_terms:
                    term_lower = term.lower()
                    if term_lower not in seen_expansions and term_lower not in {s.lower() for s in symbols}:
                        expanded_terms.append(term)
                        seen_expansions.add(term_lower)

    if not expanded_terms:
        return query, []

    expanded_query = query + " " + " ".join(expanded_terms)
    return expanded_query, expanded_terms


# Backward-compatible alias so existing callers that use _extract_technical_symbols(query)
# (with just one arg) still get list[str].
def __extract_technical_symbols_compat(query: str) -> list[str]:
    return _extract_technical_symbols_simple(query)


# Patch the module-level name so legacy callers work transparently.
# New code should call _extract_technical_symbols_simple() for list[str]
# or _extract_technical_symbols(query, cfg) for weighted tuples.


def _query_high_idf_terms(query: str, bm25: Any, min_idf: float = 5.0) -> list[str]:
    """Return discriminative query terms according to the current collection IDF."""
    import jieba
    generic_stop = {"怎么", "如何", "什么", "为何", "为什么", "是否", "怎样", "的", "了", "和", "与", "或", "在", "是", "有", "中"}
    terms: list[str] = []
    seen = set()
    for symbol in _extract_technical_symbols_simple(query):
        terms.append(symbol)
        seen.add(symbol)
    for raw in jieba.cut(query):
        term = raw.strip().lower()
        if len(term) < 2 or term in generic_stop or term in seen:
            continue
        idf = float(getattr(bm25, "idf", {}).get(raw, getattr(bm25, "idf", {}).get(term, 0.0)) or 0.0)
        if idf >= min_idf:
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


def _field_match_text(r: dict) -> str:
    return " ".join([
        str(r.get("context", "")),
        str(r.get("source_file", "")),
        str(r.get("text", "") or "")[:2000],
    ]).lower()


def _technical_symbol_match_count(r: dict, symbols: list[str]) -> int:
    if not symbols:
        return 0
    field_text = _field_match_text(r)
    return sum(1 for symbol in symbols if symbol in field_text)


def _protect_symbol_bm25_hits(results: List[dict], bm25_results: List[dict], query: str, limit: int = 8) -> List[dict]:
    """Keep exact technical-symbol BM25 hits in the candidate pool.

    Generic hybrid protection: if a query contains exact technical symbols and
    BM25 found chunks whose title/path/text contain them, those chunks should not
    be completely displaced by broader semantic neighbors before rerank/final top-k.
    """
    symbols = _extract_technical_symbols_simple(query)
    if not symbols or not bm25_results:
        return results

    existing = {_result_key(r) for r in results}
    protected: list[dict] = []
    for rank, raw in enumerate(bm25_results, 1):
        if len(protected) >= limit:
            break
        if _result_key(raw) in existing:
            continue
        match_count = _technical_symbol_match_count(raw, symbols)
        if not match_count:
            continue
        item = raw.copy()
        item["score"] = float(item.get("bm25_score", item.get("score", 0.0)) or 0.0) + match_count * 10.0 + 20.0 / rank
        item["bm25_rank"] = rank
        item["symbol_matches"] = match_count
        protected.append(item)
        existing.add(_result_key(item))

    if not protected:
        return results
    merged = results + protected
    merged.sort(key=lambda r: float(r.get("score", 0.0) or 0.0), reverse=True)
    return merged


def _field_match_rerank(results: List[dict], query: str, bm25: Any) -> List[dict]:
    """Promote results whose title/path/text matches discriminative query terms.

    This is collection-driven via BM25 IDF plus shape-based technical symbols;
    it is intentionally domain-agnostic.
    """
    terms = _query_high_idf_terms(query, bm25)
    if not terms:
        return results

    def field_score(r: dict) -> int:
        field_text = _field_match_text(r)
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
    _, chunks = _load_keyword_searcher_for_domain(cfg)
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

    def bm25_search(
        self,
        query: str,
        top_k: int = 20,
        category: Optional[str] = None,
        has_code: Optional[bool] = None,
    ) -> List[dict]:
        if not self.bm25_enabled:
            logger.info("BM25 disabled for domain=%s", self.domain)
            return []

        searcher, chunks = _load_keyword_searcher_for_domain(self.cfg)
        if searcher is None:
            return []

        keyword_results = searcher.search(query, top_k=top_k, category=category, has_code=has_code)
        results = []
        for result in keyword_results:
            meta = result.metadata or {}
            chunk_pos = meta.get("chunk_pos")
            original_text = result.text
            if chunks and chunk_pos is not None:
                try:
                    original_text = chunks[int(chunk_pos)].get("original_text") or result.text
                except (IndexError, TypeError, ValueError):
                    original_text = result.text
            results.append({
                "bm25_score": float(result.score),
                "domain": meta.get("domain", self.domain),
                "text": original_text,
                "context": meta.get("context", ""),
                "source_file": meta.get("source_file", ""),
                "has_code": meta.get("has_code", False),
                "chunk_index": meta.get("chunk_index", chunk_pos if chunk_pos is not None else 0),
                "chunk_pos": chunk_pos,
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

    @staticmethod
    def _snapshot_stage(results: List[dict], limit: int = 10) -> dict:
        """Create a trace stage snapshot for debug output."""
        entries = []
        for i, r in enumerate(results[:limit], 1):
            entries.append({
                "rank": i,
                "score": round(float(r.get("score", r.get("semantic_score", r.get("bm25_score", 0.0))) or 0.0), 4),
                "source_file": r.get("source_file", ""),
                "context": r.get("context", "")[:120],
                "text_len": len(r.get("text", "") or ""),
                "symbol_matches": r.get("symbol_matches"),
                "bm25_rank": r.get("bm25_rank"),
                "bm25_score": r.get("bm25_score") or r.get("bm25_score"),
            })
        return {"count": len(results), "top": entries}

    # ── Search orchestration ──

    def search(
        self,
        query: str,
        top_k: int = 5,
        category: Optional[str] = None,
        has_code: Optional[bool] = None,
        method: str = "hybrid",
        rerank: bool = True,
        debug: bool = False,
    ) -> tuple[List[dict], Optional[dict]]:
        """Execute search and return (results, trace_dict_or_None).

        When debug=True, trace_dict contains per-stage snapshots.
        When debug=False, trace_dict is None and behavior is unchanged.
        """
        if method in ("rerank", "hybrid_rerank"):
            method = "hybrid"
            rerank = True

        trace: Optional[dict] = None
        if debug:
            symbols_list = _extract_technical_symbols_simple(query)
            expanded_bm25_query, expanded_terms = expand_query(query, self.domain)
            trace = {
                "query": query,
                "domain": self.domain,
                "method": method,
                "query_symbols": symbols_list,
                "query_expansion": expanded_terms,
                "expanded_bm25_query": expanded_bm25_query,
            }

        logger.info(
            "RAG_SEARCH_START domain=%s collection=%s method=%s rerank=%s top_k=%s category=%s has_code=%s query=%r debug=%s",
            self.domain,
            self.collection,
            method,
            rerank,
            top_k,
            category,
            has_code,
            query[:200],
            debug,
        )

        if method == "semantic":
            symbols = _extract_technical_symbols_simple(query)
            candidate_k = top_k * 20 if rerank else (max(top_k * 20, 100) if symbols else top_k)
            results = self.semantic_search(query, top_k=candidate_k, category=category, has_code=has_code)
            for r in results:
                r["score"] = r.pop("semantic_score")
            if symbols:
                for r in results:
                    matches = _technical_symbol_match_count(r, symbols)
                    if matches:
                        r["symbol_matches"] = matches
                        r["score"] = float(r.get("score", 0.0) or 0.0) + matches * 2.0
                results.sort(key=lambda r: float(r.get("score", 0.0) or 0.0), reverse=True)
            if rerank:
                self._log_stage("semantic_candidates", results)
                if trace:
                    trace["semantic_candidates"] = self._snapshot_stage(results)
                candidates = results
            else:
                results = results[:top_k]
                self._log_stage("semantic", results)
                if trace:
                    trace["final"] = self._snapshot_stage(results)
                return results, trace

        elif method == "bm25":
            # When debug, use expanded query for BM25
            bm25_query = query
            if debug and trace:
                bm25_query = trace.get("expanded_bm25_query", query)
            elif not debug:
                # Even without debug, use expanded query for BM25 (non-breaking improvement)
                bm25_query, _ = expand_query(query, self.domain)
            candidate_k = top_k * 20 if rerank else top_k
            results = self.bm25_search(bm25_query, top_k=candidate_k, category=category, has_code=has_code)
            for r in results:
                r["score"] = r.pop("bm25_score")
            if rerank:
                self._log_stage("bm25_candidates", results)
                if trace:
                    trace["bm25_candidates"] = self._snapshot_stage(results)
                candidates = results
            else:
                self._log_stage("bm25", results)
                if trace:
                    trace["final"] = self._snapshot_stage(results)
                return results, trace

        else:
            # Hybrid runs both recall paths, fuses them, and optionally reranks.
            expanded_bm25_query, _ = expand_query(query, self.domain)
            bm25_query = expanded_bm25_query if (expanded_bm25_query != query) else query

            candidate_k = max(top_k * 40, 200) if rerank else max(top_k * 4, 20)
            sem_results = self.semantic_search(query, top_k=candidate_k, category=category, has_code=has_code)
            bm25_results = self.bm25_search(bm25_query, top_k=candidate_k, category=category, has_code=has_code)
            bm25_meta = {
                _result_key(r): {"bm25_rank": rank, "bm25_score": r.get("bm25_score", 0.0)}
                for rank, r in enumerate(bm25_results, 1)
            }
            self._log_stage("semantic_candidates", sem_results)
            self._log_stage("bm25_candidates", bm25_results)
            if trace:
                trace["semantic_candidates"] = self._snapshot_stage(sem_results)
                trace["bm25_candidates"] = self._snapshot_stage(bm25_results)

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
                # Build symbol_weights from known_identifiers for weighted boost
                symbol_weights = self._build_symbol_weights()
                fused = identifier_boost(fused, query, boost_per_match=1.0, symbol_weights=symbol_weights)
                fused = _protect_symbol_bm25_hits(fused, bm25_results, query, limit=max(top_k, 8))
            for item in fused:
                item.update(bm25_meta.get(_result_key(item), {}))
            self._log_stage("fusion", fused)
            if trace:
                trace["fusion"] = self._snapshot_stage(fused)

            if not rerank:
                final_results = fused[:top_k]
                if trace:
                    trace["final"] = self._snapshot_stage(final_results)
                return final_results, trace
            candidates = fused

        is_comparison = _is_comparison_query(query)
        reranked = rerank_results(
            query,
            candidates,
            top_k=max(top_k * 3, 15),
            api_base=self.rerank_api_base,
            model_name=self.rerank_model_name,
        )
        keyword_searcher, _ = _load_keyword_searcher_for_domain(self.cfg)
        bm25 = getattr(keyword_searcher, "_bm25", None) if keyword_searcher is not None else None
        if bm25 is not None and not _is_explanatory_query(query) and not is_comparison:
            reranked = _field_match_rerank(reranked, query, bm25)
        # Always filter obvious noise regardless of query type.
        reranked = _drop_obvious_noise(reranked, query)
        # Deduplicate by source_file
        seen_sources: set[str] = set()
        deduped: list[dict] = []
        for r in reranked:
            src = r.get("source_file", "")
            if src not in seen_sources:
                seen_sources.add(src)
                deduped.append(r)
        reranked = deduped[:top_k]
        self._log_stage("rerank", reranked)
        if trace:
            trace["rerank"] = self._snapshot_stage(reranked)
            trace["final"] = self._snapshot_stage(reranked)
        return reranked, trace

    def _build_symbol_weights(self) -> Dict[str, float]:
        """Build a symbol→weight mapping from domain's known_identifiers config."""
        known = self.cfg.get("known_identifiers", [])
        if not known:
            return {}
        weights: Dict[str, float] = {}
        for ident in known:
            key = ident.lower()
            weights[key] = 2.0
            # Also add bare alphanumeric version (e.g. @Component → component)
            import re
            bare = re.sub(r"[^A-Za-z0-9]", "", ident).lower()
            if bare and bare != key:
                weights[bare] = 2.0
        return weights

    # ── Full RAG query ──

    def rag_query(
        self,
        question: str,
        top_k: int = 5,
        category: Optional[str] = None,
        has_code: Optional[bool] = None,
        method: str = "hybrid",
        rerank: bool = True,
        debug: bool = False,
    ) -> dict:
        results, trace = self.search(
            question, top_k=top_k, category=category, has_code=has_code,
            method=method, rerank=rerank, debug=debug,
        )
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

        result = {
            "question": question,
            "results": results,
            "context": context,
            "prompt": prompt,
        }
        if trace is not None:
            result["trace"] = trace
        return result
