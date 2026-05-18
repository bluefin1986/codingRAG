"""Local BM25 keyword search backend.

This backend keeps the current in-process BM25 behavior, but isolates it behind
KeywordSearcher so the query engine can later switch to Elasticsearch/OpenSearch
without changing hybrid retrieval logic.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import jieba
from rank_bm25 import BM25Okapi

from indexer.keyword_searcher import KeywordSearcher, KeywordSearchResult


logger = logging.getLogger(__name__)


class LocalBM25Searcher(KeywordSearcher):
    """In-memory BM25 keyword searcher based on jieba + rank_bm25."""

    def __init__(
        self,
        *,
        domain: str,
        chunks: List[Dict[str, Any]],
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(domain=domain, config=config)
        self.chunks = chunks
        self._bm25: Optional[BM25Okapi] = None
        self._tokenized_corpus: Optional[List[List[str]]] = None
        self._build_index()

    def _build_index(self) -> None:
        """Build BM25 index from chunk text."""
        if not self.chunks:
            self._bm25 = None
            self._tokenized_corpus = []
            logger.info("local BM25 build skipped domain=%s chunks=0", self.domain)
            return

        started = time.perf_counter()
        logger.info("local BM25 build start domain=%s chunks=%d", self.domain, len(self.chunks))

        tokenize_started = time.perf_counter()
        self._tokenized_corpus = [
            list(jieba.cut(str(chunk.get("text") or "")))
            for chunk in self.chunks
        ]
        tokenize_ms = int((time.perf_counter() - tokenize_started) * 1000)
        logger.info("local BM25 tokenized domain=%s chunks=%d elapsedMs=%d", self.domain, len(self.chunks), tokenize_ms)

        index_started = time.perf_counter()
        self._bm25 = BM25Okapi(self._tokenized_corpus)
        index_ms = int((time.perf_counter() - index_started) * 1000)
        total_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "local BM25 build done domain=%s chunks=%d tokenizeMs=%d indexMs=%d totalMs=%d",
            self.domain,
            len(self.chunks),
            tokenize_ms,
            index_ms,
            total_ms,
        )

    def search(
        self,
        query: str,
        top_k: int = 20,
        category: Optional[str] = None,
        has_code: Optional[bool] = None,
    ) -> List[KeywordSearchResult]:
        """Search local BM25 index and return normalized keyword results."""
        if not query or not self._bm25 or not self.chunks:
            return []

        tokenized_query = list(jieba.cut(query))
        scores = self._bm25.get_scores(tokenized_query)

        ranked: List[Tuple[int, float]] = sorted(
            enumerate(scores),
            key=lambda item: item[1],
            reverse=True,
        )[:top_k]

        results: List[KeywordSearchResult] = []
        for index, score in ranked:
            if score <= 0:
                continue

            chunk = self.chunks[index]
            metadata = dict(chunk.get("metadata") or {})
            if category and metadata.get("category") != category:
                continue
            if has_code is not None and bool(metadata.get("has_code", False)) != has_code:
                continue
            metadata["chunk_pos"] = index
            results.append(
                KeywordSearchResult(
                    text=str(chunk.get("text") or ""),
                    score=float(score),
                    metadata=metadata,
                )
            )

        return results
