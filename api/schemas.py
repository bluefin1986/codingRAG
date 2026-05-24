"""Pydantic schemas for codingRAG HTTP API."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RagQueryRequest(BaseModel):
    """POST /api/v1/rag/query request body."""

    query: str = Field(..., min_length=1, description="检索查询文本")
    domain: Optional[str] = Field(None, description="领域名称，如 ios / harmonyos；不填则使用服务端默认领域")
    topK: int = Field(5, ge=1, le=50, description="返回结果数量")
    method: str = Field("hybrid", description="召回方法：hybrid / semantic / bm25")
    rerank: bool = Field(True, description="是否启用 rerank 精排")
    category: Optional[str] = Field(None, description="文档分类过滤")
    hasCode: Optional[bool] = Field(None, description="是否只检索含代码的文档块")
    debug: bool = Field(False, description="启用调试追踪，返回每个检索阶段的详细信息")

    @model_validator(mode="after")
    def normalize_legacy_method(self) -> "RagQueryRequest":
        """Preserve historical method names while exposing independent controls."""
        if self.method in ("rerank", "hybrid_rerank"):
            self.method = "hybrid"
            self.rerank = True
        return self


class RagResultItem(BaseModel):
    """单条检索结果。"""

    score: float = 0.0
    domain: str = ""
    text: str = ""
    context: str = ""
    source_file: str = ""
    has_code: bool = False
    rerank_score: Optional[float] = None
    rerank_model: Optional[str] = None


class TraceStageEntry(BaseModel):
    """Single entry within a retrieval trace stage."""
    rank: int = 0
    score: float = 0.0
    source_file: str = ""
    context: str = Field("", description="First 120 chars of context")
    text_len: int = 0
    symbol_matches: Optional[int] = None
    bm25_rank: Optional[int] = None
    bm25_score: Optional[float] = None


class TraceStage(BaseModel):
    """One retrieval stage in the debug trace."""
    count: int = 0
    top: List[TraceStageEntry] = Field(default_factory=list, description="Top entries (max 10)")


class RetrievalTrace(BaseModel):
    """Debug trace for the full retrieval pipeline."""
    query: str = ""
    domain: str = ""
    method: str = ""
    query_symbols: List[str] = Field(default_factory=list)
    query_expansion: List[str] = Field(default_factory=list, description="Expanded terms added for BM25")
    expanded_bm25_query: str = Field("", description="Full query used for BM25 (original + expansions)")
    semantic_candidates: Optional[TraceStage] = None
    bm25_candidates: Optional[TraceStage] = None
    fusion: Optional[TraceStage] = None
    boosts: Optional[TraceStage] = None
    rerank: Optional[TraceStage] = None
    final: Optional[TraceStage] = None


class RagQueryResponse(BaseModel):
    """POST /api/v1/rag/query response body."""

    query: str
    domain: str
    topK: int
    method: str
    context: str
    results: List[RagResultItem]
    trace: Optional[RetrievalTrace] = Field(None, description="Debug trace, only populated when debug=true")


class LibraryExportRequest(BaseModel):
    """POST /api/libraries/{library_id}/export request body."""

    format: str = Field("tar.gz", description="Archive format: tar.gz or zip")
    output_dir: Optional[str] = Field(None, description="Optional server-side output directory")


class LibraryImportRequest(BaseModel):
    """POST /api/libraries/import(/preview) request body."""

    model_config = ConfigDict(populate_by_name=True)

    archive_path: str = Field(..., min_length=1, description="Server-side archive path from export or upload staging")
    mode: Optional[str] = Field(None, description="skip / upsert / replace-library / rename-library")
    new_library_code: Optional[str] = Field(None, description="Target library code for rename-library imports")
    async_import: bool = Field(True, alias="async", description="Enqueue actual imports as async jobs by default")


class IngestJobCreateRequest(BaseModel):
    """POST /api/knowledge-bases/{domain}/ingest-jobs request body."""

    source_type: Literal["files", "directory", "server_dir", "upload"] = Field(
        "files",
        description="Input source selected for this registration-only job. 'upload' is a legacy alias for 'files'.",
    )
    batch_size: int = Field(100, ge=1, le=1000, description="Number of items processed per worker batch.")


class IngestServerDirRequest(BaseModel):
    """POST /api/ingest-jobs/{job_id}/scan-server-dir request body."""

    limit: Optional[int] = Field(None, ge=1, le=10000, description="Optional sample-scan safety limit.")
    batch_size: Optional[int] = Field(None, ge=1, le=1000, description="Override this job's batch size.")


class ReindexJobCreateRequest(BaseModel):
    """POST /api/reindex-jobs request body."""

    domain: str = Field(..., min_length=1, description="Formal domain whose changed documents should be indexed.")
    changed_only: bool = Field(True, description="Only documents already marked index_required are supported.")


class QueryExpansionRequest(BaseModel):
    """POST /api/query-expansions request body."""

    domain: str = Field(..., min_length=1)
    source_term: str = Field(..., min_length=1)
    expanded_terms: List[str] = Field(..., min_length=1)


class QueryExpansionItem(BaseModel):
    """Persisted query expansion entry."""

    id: str
    domain: str
    source_term: str
    expanded_terms: List[str]
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DomainRequest(BaseModel):
    """POST /api/domains request body."""

    domain_key: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    language: str = ""
    docs_dir: Optional[str] = None
    collection: str = Field(..., min_length=1)
    embedding_model: str = "BAAI/bge-m3"
    embedding_model_name: str = "bge-m3"
    embedding_dim: int = Field(1024, gt=0)
    rerank_model_name: str = "bge-reranker-base"
    prompt_role: str = "技术专家"
    bm25_enabled: bool = True
    bm25_weight: float = 0.3
    path_boost_per_match: float = 0.0
    noise_patterns: List[str] = Field(default_factory=list)
    known_identifiers: List[str] = Field(default_factory=list)


class DomainItem(BaseModel):
    """Persisted or configured fallback domain."""

    domain_key: str
    display_name: str
    language: str
    docs_dir: Optional[str]
    collection: str
    embedding_model: str
    embedding_model_name: str
    embedding_dim: int
    rerank_model_name: str
    prompt_role: str
    bm25_enabled: bool
    bm25_weight: float
    path_boost_per_match: float
    noise_patterns: List[str]
    known_identifiers: List[str]
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
