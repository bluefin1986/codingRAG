

"""Elasticsearch / OpenSearch keyword search backend.

This backend provides BM25-based keyword retrieval using an external search
engine instead of in-process rank_bm25.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from indexer.keyword_searcher import KeywordSearcher, KeywordSearchResult


class ESSearcher(KeywordSearcher):
    """Keyword search backend powered by Elasticsearch/OpenSearch."""

    def __init__(
        self,
        *,
        domain: str,
        index_name: str,
        base_url: str,
        api_key: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(domain=domain, config=config)
        self.index_name = index_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(timeout=30.0)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        return headers

    def _build_query(self, query: str, top_k: int) -> Dict[str, Any]:
        """Build Elasticsearch/OpenSearch BM25 query."""
        return {
            "size": top_k,
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": [
                        "text^1",
                        "context^2",
                        "source_file^3",
                    ],
                    "type": "best_fields",
                }
            },
        }

    def search(self, query: str, top_k: int = 20) -> List[KeywordSearchResult]:
        """Execute BM25 search against Elasticsearch/OpenSearch."""
        if not query:
            return []

        payload = self._build_query(query, top_k)

        response = self.client.post(
            f"{self.base_url}/{self.index_name}/_search",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        hits = data.get("hits", {}).get("hits", [])

        results: List[KeywordSearchResult] = []
        for hit in hits:
            source = hit.get("_source", {}) or {}
            metadata = {
                "domain": source.get("domain", self.domain),
                "context": source.get("context", ""),
                "source_file": source.get("source_file", ""),
                "has_code": source.get("has_code", False),
                "chunk_index": source.get("chunk_index", 0),
                "chunk_pos": source.get("chunk_pos"),
            }

            results.append(
                KeywordSearchResult(
                    text=source.get("text", ""),
                    score=float(hit.get("_score") or 0.0),
                    metadata=metadata,
                )
            )

        return results

    def close(self) -> None:
        self.client.close()