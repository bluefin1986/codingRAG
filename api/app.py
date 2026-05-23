"""codingRAG HTTP API server.

启动命令：
    cd /Users/niuma/Workspace/ragworkspace/codingRAG
    python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8060

或者指定默认领域：
    CODING_RAG_DOMAIN=harmonyos python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8060

Smoke test:
    curl -s -X POST http://localhost:8060/api/v1/rag/query \\
      -H 'Content-Type: application/json' \\
      -d '{"query": "UIButton 怎么创建", "domain": "ios", "topK": 3}' | python3 -m json.tool
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# Ensure project root is on sys.path so `config` and `indexer` are importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import (
    ACTIVE_DOMAIN,
    EMBEDDING_API_BASE,
    QDRANT_HOST,
    QDRANT_PORT,
    RERANK_API_BASE,
    get_domain_config,
)
from config import DOMAIN_REGISTRY  # noqa: E402

from api.engine import DomainQueryEngine  # noqa: E402
from api.registry import DocumentRegistry, RegistryUnavailable  # noqa: E402
from api.schemas import (  # noqa: E402
    LibraryExportRequest,
    LibraryImportRequest,
    RagQueryRequest,
    RagQueryResponse,
    RagResultItem,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("codingrag.api")

app = FastAPI(
    title="codingRAG API",
    version="0.1.0",
    description="codingRAG 检索接口，供 llmproxy 等上游调用。",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Engine cache: one engine per domain, lazily created ──
_engines: Dict[str, DomainQueryEngine] = {}
_registry: DocumentRegistry | None = None


def _get_engine(domain: str) -> DomainQueryEngine:
    """Get or create a DomainQueryEngine for the given domain."""
    if domain in _engines:
        return _engines[domain]

    cfg = get_domain_config(domain)
    # Populate service endpoints from global config / env vars
    cfg["embedding_api_base"] = EMBEDDING_API_BASE
    cfg["rerank_api_base"] = RERANK_API_BASE
    cfg["qdrant_host"] = QDRANT_HOST
    cfg["qdrant_port"] = QDRANT_PORT

    engine = DomainQueryEngine(cfg)
    _engines[domain] = engine
    logger.info("Created engine for domain=%s (collection=%s)", domain, cfg["collection"])
    return engine


def _get_registry() -> DocumentRegistry:
    global _registry
    if _registry is None:
        _registry = DocumentRegistry()
    return _registry


def _preload_domains(domains_str: str) -> None:
    """Pre-warm engines and BM25 indexes for specified domains at startup.

    Accepts a comma-separated list of domain names from the
    CODING_RAG_PRELOAD_DOMAINS environment variable, e.g.:
        CODING_RAG_PRELOAD_DOMAINS=ios,harmonyos
    """
    domains = [d.strip().lower() for d in domains_str.split(',') if d.strip()]
    if not domains:
        return
    logger.info("Preloading domains: %s", domains)
    for domain in domains:
        if domain not in DOMAIN_REGISTRY:
            logger.warning("Skipping unknown domain for preload: %s", domain)
            continue
        try:
            _get_engine(domain)
            logger.info("Preloaded domain=%s", domain)
        except Exception:
            logger.exception("Failed to preload domain=%s", domain)


@app.on_event("startup")
def _on_startup() -> None:
    """Pre-warm configured domains if CODING_RAG_PRELOAD_DOMAINS is set."""
    preload_env = os.getenv("CODING_RAG_PRELOAD_DOMAINS", "").strip()
    if preload_env:
        _preload_domains(preload_env)
    try:
        _get_registry().init_schema()
        logger.info("Document registry schema initialized")
    except RegistryUnavailable:
        logger.info("Document registry disabled: CODING_RAG_DATABASE_URL is not configured")
    except Exception:
        logger.exception("Document registry schema initialization failed")


# ── Endpoints ──

@app.get("/health")
def health():
    return {
        "status": "ok",
        "default_domain": ACTIVE_DOMAIN,
        "available_domains": sorted(DOMAIN_REGISTRY.keys()),
    }


@app.get("/api/libraries")
def list_libraries():
    try:
        return {"items": _get_registry().list_libraries()}
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_libraries failed")
        raise HTTPException(status_code=500, detail=f"Failed to list libraries: {e}")


@app.post("/api/docs/scan")
def scan_docs(
    domain: str = Query(..., description="Domain/library code to scan, e.g. ios or redis62"),
    limit: Optional[int] = Query(None, ge=1, le=10000, description="Optional safety limit for sample scans"),
):
    try:
        return _get_registry().scan_domain(domain, limit=limit).__dict__
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("scan_docs failed for domain=%s", domain)
        raise HTTPException(status_code=500, detail=f"Failed to scan docs: {e}")


@app.get("/api/docs")
def list_docs(
    domain: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    try:
        return _get_registry().list_documents(domain=domain, status=status, q=q, limit=limit, offset=offset)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_docs failed")
        raise HTTPException(status_code=500, detail=f"Failed to list docs: {e}")


@app.get("/api/docs/{document_id}")
def get_doc(document_id: str):
    try:
        doc = _get_registry().get_document(document_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("get_doc failed for document_id=%s", document_id)
        raise HTTPException(status_code=500, detail=f"Failed to get doc: {e}")
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@app.get("/api/docs/{document_id}/content")
def get_doc_content(document_id: str, version: Optional[int] = Query(None, ge=1)):
    try:
        content = _get_registry().get_document_content(document_id, version=version)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"Stored content missing: {e}")
    except Exception as e:
        logger.exception("get_doc_content failed for document_id=%s", document_id)
        raise HTTPException(status_code=500, detail=f"Failed to get doc content: {e}")
    if not content:
        raise HTTPException(status_code=404, detail="Document/version not found")
    return content


@app.post("/api/docs/{document_id}/enable")
def enable_doc(document_id: str):
    try:
        doc = _get_registry().set_document_enabled(document_id, True)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("enable_doc failed for document_id=%s", document_id)
        raise HTTPException(status_code=500, detail=f"Failed to enable doc: {e}")
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@app.post("/api/docs/{document_id}/disable")
def disable_doc(document_id: str):
    try:
        doc = _get_registry().set_document_enabled(document_id, False)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("disable_doc failed for document_id=%s", document_id)
        raise HTTPException(status_code=500, detail=f"Failed to disable doc: {e}")
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@app.post("/api/libraries/{library_id}/export")
def export_library(library_id: str, req: Optional[LibraryExportRequest] = None):
    req = req or LibraryExportRequest()
    try:
        return _get_registry().export_library(library_id, archive_format=req.format, output_dir=req.output_dir)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Library not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("export_library failed for library_id=%s", library_id)
        raise HTTPException(status_code=500, detail=f"Failed to export library: {e}")


@app.post("/api/libraries/import/preview")
def preview_library_import(req: LibraryImportRequest):
    try:
        return _get_registry().preview_library_import(req.archive_path, mode=req.mode, new_library_code=req.new_library_code)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("preview_library_import failed")
        raise HTTPException(status_code=500, detail=f"Failed to preview library import: {e}")


@app.post("/api/libraries/import")
def import_library(req: LibraryImportRequest, async_: Optional[bool] = Query(None, alias="async")):
    try:
        use_async = req.async_import if async_ is None else async_
        if use_async:
            return _get_registry().enqueue_library_import(req.archive_path, mode=req.mode, new_library_code=req.new_library_code)
        return _get_registry().import_library(req.archive_path, mode=req.mode, new_library_code=req.new_library_code)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("import_library failed")
        raise HTTPException(status_code=500, detail=f"Failed to import library: {e}")


@app.get("/api/library-transfer-jobs/{job_id}")
def get_library_transfer_job(job_id: str):
    try:
        job = _get_registry().get_transfer_job(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("get_library_transfer_job failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to get transfer job: {e}")
    if not job:
        raise HTTPException(status_code=404, detail="Transfer job not found")
    return job


@app.post("/api/libraries/{library_id}/retention/preview")
def retention_preview(library_id: str, keep: int = Query(2, ge=1, le=100)):
    try:
        return _get_registry().retention_preview(library_id, keep=keep)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Library not found")
    except Exception as e:
        logger.exception("retention_preview failed for library_id=%s", library_id)
        raise HTTPException(status_code=500, detail=f"Failed to build retention preview: {e}")


@app.post("/api/v1/rag/query", response_model=RagQueryResponse)
def rag_query(req: RagQueryRequest):
    """执行 RAG 检索。domain 不填时使用服务端默认领域。"""
    domain = (req.domain or ACTIVE_DOMAIN).strip().lower()
    if domain not in DOMAIN_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown domain={domain!r}; available: {sorted(DOMAIN_REGISTRY.keys())}",
        )

    try:
        engine = _get_engine(domain)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to init engine for domain={domain}: {e}")

    try:
        result = engine.rag_query(
            question=req.query,
            top_k=req.topK,
            category=req.category,
            has_code=req.hasCode,
            method=req.method,
        )
    except Exception as e:
        logger.exception("rag_query failed for domain=%s query=%r", domain, req.query)
        raise HTTPException(status_code=502, detail=f"RAG query failed: {e}")

    items = [RagResultItem(**r) for r in result["results"]]
    return RagQueryResponse(
        query=req.query,
        domain=domain,
        topK=req.topK,
        method=req.method,
        context=result["context"],
        results=items,
    )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("CODINGRAG_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("CODINGRAG_HTTP_PORT", "8060"))
    reload_enabled = os.getenv("CODINGRAG_HTTP_RELOAD", "false").lower() == "true"

    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        reload=reload_enabled,
    )
