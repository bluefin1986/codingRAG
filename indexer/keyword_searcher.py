"""Keyword search abstraction for codingRAG.

This module defines a stable interface for keyword-based retrieval.

Current and future implementations can include:
- Local BM25 based on rank_bm25
- Elasticsearch / OpenSearch backed BM25

The query engine should depend on this abstraction instead of depending on
BM25 implementation details directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class KeywordSearchResult:
    """Normalized keyword search result returned by all keyword backends."""

    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to the dict shape used by the query engine."""
        return {
            "text": self.text,
            "score": self.score,
            "metadata": self.metadata,
        }


class KeywordSearcher(ABC):
    """Base interface for keyword retrieval backends."""

    def __init__(self, *, domain: str, config: Optional[Dict[str, Any]] = None) -> None:
        self.domain = domain
        self.config = config or {}

    @abstractmethod
    def search(self, query: str, top_k: int = 20) -> List[KeywordSearchResult]:
        """Search keyword index and return normalized results."""
        raise NotImplementedError

    def close(self) -> None:
        """Release resources if the backend keeps network/file handles."""
        return None
