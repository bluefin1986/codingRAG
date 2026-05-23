"""Pydantic schemas for codingRAG HTTP API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict


class RagQueryRequest(BaseModel):
    """POST /api/v1/rag/query request body."""

    query: str = Field(..., min_length=1, description="检索查询文本")
    domain: Optional[str] = Field(None, description="领域名称，如 ios / harmonyos；不填则使用服务端默认领域")
    topK: int = Field(5, ge=1, le=50, description="返回结果数量")
    method: str = Field("hybrid", description="检索方法：hybrid / semantic / bm25 / rerank")
    category: Optional[str] = Field(None, description="文档分类过滤")
    hasCode: Optional[bool] = Field(None, description="是否只检索含代码的文档块")


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


class RagQueryResponse(BaseModel):
    """POST /api/v1/rag/query response body."""

    query: str
    domain: str
    topK: int
    method: str
    context: str
    results: List[RagResultItem]


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
