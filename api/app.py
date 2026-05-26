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
from typing import Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
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

from api.engine import DomainQueryEngine  # noqa: E402
from api.registry import (  # noqa: E402
    DocumentRegistry,
    IngestStateConflict,
    RegistryUnavailable,
    domain_cache,
    query_expansion_cache,
)
from indexer.per_doc_indexer import DocumentDisabled, DocumentNotFound, PerDocumentIndexer  # noqa: E402
from api.schemas import (  # noqa: E402
    LibraryExportRequest,
    LibraryImportRequest,
    IngestJobCreateRequest,
    IngestServerDirRequest,
    ReindexJobCreateRequest,
    DomainItem,
    DomainRequest,
    RagQueryRequest,
    RagQueryResponse,
    RagResultItem,
    QueryExpansionItem,
    QueryExpansionRequest,
    RetrievalTrace,
    TraceStage,
    TraceStageEntry,
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
    available_domains = {item["domain_key"] for item in domain_cache.list_domains()}
    for domain in domains:
        if domain not in available_domains:
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
    try:
        _get_registry().init_schema()
        logger.info("Document registry schema initialized")
    except RegistryUnavailable:
        logger.info("Document registry disabled: CODING_RAG_DATABASE_URL is not configured")
    except Exception:
        logger.exception("Document registry schema initialization failed")
    domain_cache.load()
    query_expansion_cache.load()
    preload_env = os.getenv("CODING_RAG_PRELOAD_DOMAINS", "").strip()
    if preload_env:
        _preload_domains(preload_env)


# ── Endpoints ──

@app.get("/health")
def health():
    return {
        "status": "ok",
        "default_domain": ACTIVE_DOMAIN,
        "available_domains": [item["domain_key"] for item in domain_cache.list_domains()],
    }


@app.get("/api/domains", response_model=list[DomainItem])
def list_domains():
    """List all registered domains available to this process."""
    return domain_cache.list_domains()


@app.post("/api/domains/reload")
def reload_domains():
    """Force reload domain configuration cache from PostgreSQL."""
    try:
        _get_registry().init_schema()
        domain_cache.refresh()
        _engines.clear()
        return {"reloaded": True}
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("reload_domains failed")
        raise HTTPException(status_code=500, detail=f"Failed to reload domains: {e}")


@app.get("/api/domains/{domain_key}", response_model=DomainItem)
def get_domain(domain_key: str):
    """Get a single registered domain."""
    normalized = domain_key.strip().lower()
    item = next((domain for domain in domain_cache.list_domains() if domain["domain_key"] == normalized), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown domain={domain_key!r}")
    return item


@app.post("/api/domains", response_model=DomainItem)
def create_or_update_domain(req: DomainRequest):
    """Create or update one persisted domain."""
    try:
        _get_registry().init_schema()
        item = domain_cache.upsert(req.domain_key, req.model_dump(exclude={"domain_key"}))
        _engines.pop(req.domain_key.strip().lower(), None)
        return item
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("create_or_update_domain failed for domain=%s", req.domain_key)
        raise HTTPException(status_code=500, detail=f"Failed to save domain: {e}")


@app.delete("/api/domains/{domain_key}")
def delete_domain(domain_key: str):
    """Disable one persisted domain."""
    normalized = domain_key.strip().lower()
    try:
        _get_registry().init_schema()
        domain_cache.delete(normalized)
        _engines.pop(normalized, None)
        return {"deleted": True, "domain_key": normalized}
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Domain not found")
    except Exception as e:
        logger.exception("delete_domain failed for domain=%s", normalized)
        raise HTTPException(status_code=500, detail=f"Failed to delete domain: {e}")


@app.post("/api/domains/{domain_key}/reindex-all")
def reindex_all_docs(
    domain_key: str,
    index_target: str = Query("both", pattern="^(both|vector|bm25)$"),
):
    """Mark all enabled documents and enqueue a background reindex job."""
    normalized = domain_key.strip().lower()
    try:
        return _get_registry().create_reindex_job(
            normalized,
            changed_only=True,
            mark_all=True,
            index_target=index_target,
        )
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        status_code = 404 if str(e).startswith("Unknown domain=") else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception as e:
        logger.exception("reindex_all_docs failed for domain=%s", normalized)
        raise HTTPException(status_code=500, detail=f"Failed to queue reindex job: {e}")


@app.get("/api/libraries")
def list_libraries():
    try:
        return {"items": _get_registry().list_libraries()}
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_libraries failed")
        raise HTTPException(status_code=500, detail=f"Failed to list libraries: {e}")


@app.get("/api/knowledge-bases")
def list_knowledge_bases():
    """List formal domains with primary library/document and latest ingest state."""
    try:
        return {"items": _get_registry().list_knowledge_bases()}
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_knowledge_bases failed")
        raise HTTPException(status_code=500, detail=f"Failed to list knowledge bases: {e}")


@app.get("/api/knowledge-bases/{domain}/documents")
def list_knowledge_base_documents(
    domain: str,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    try:
        return _get_registry().list_knowledge_base_documents(
            domain,
            status=status,
            q=q,
            limit=limit,
            offset=offset,
        )
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("list_knowledge_base_documents failed for domain=%s", domain)
        raise HTTPException(status_code=500, detail=f"Failed to list knowledge base documents: {e}")


@app.delete("/api/knowledge-bases/{domain}/documents", status_code=202)
def clear_knowledge_base_documents(domain: str):
    """Queue an exclusive background clear; the request never performs index I/O."""
    try:
        return _get_registry().create_knowledge_base_clear_job(domain)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except IngestStateConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        status_code = 404 if str(e).startswith("Unknown domain=") else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception as e:
        logger.exception("clear_knowledge_base_documents failed for domain=%s", domain)
        raise HTTPException(status_code=500, detail=f"Failed to queue knowledge base clear: {e}")


@app.get("/api/knowledge-clear-jobs")
def list_knowledge_base_clear_jobs(domain: Optional[str] = None):
    try:
        return _get_registry().list_knowledge_base_clear_jobs(domain=domain)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_knowledge_base_clear_jobs failed")
        raise HTTPException(status_code=500, detail=f"Failed to list knowledge base clear jobs: {e}")


@app.get("/api/knowledge-clear-jobs/{job_id}")
def get_knowledge_base_clear_job(job_id: str):
    try:
        job = _get_registry().get_knowledge_base_clear_job(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("get_knowledge_base_clear_job failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to get knowledge base clear job: {e}")
    if not job:
        raise HTTPException(status_code=404, detail="Knowledge base clear job not found")
    return job


@app.post("/api/knowledge-bases/{domain}/ingest-jobs")
def create_ingest_job(domain: str, req: Optional[IngestJobCreateRequest] = None):
    """Create an asynchronous registration-only ingest job for one formal domain."""
    req = req or IngestJobCreateRequest()
    try:
        return _get_registry().create_ingest_job(domain, source_type=req.source_type, batch_size=req.batch_size)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("create_ingest_job failed for domain=%s", domain)
        raise HTTPException(status_code=500, detail=f"Failed to create ingest job: {e}")


@app.post("/api/ingest-jobs/{job_id}/files")
async def upload_ingest_files(
    job_id: str,
    files: List[UploadFile] = File(..., description="Uploaded text documents."),
    # FastAPI 0.111 extracts repeated form parts as a list only with a non-Optional list annotation.
    relative_paths: List[str] = Form(
        None,
        description="Relative paths paired with files, including browser webkitRelativePath values.",
    ),
):
    """Stage one upload batch; complete the job separately to allow registration."""
    if relative_paths is not None and len(relative_paths) != len(files):
        raise HTTPException(status_code=400, detail="relative_paths count must match files count")
    payload: list[tuple[str, bytes]] = []
    for index, file in enumerate(files):
        relative_path = relative_paths[index] if relative_paths is not None else (file.filename or "")
        payload.append((relative_path, await file.read()))
    try:
        return _get_registry().stage_ingest_files(job_id, payload)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    except IngestStateConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("upload_ingest_files failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to queue ingest files: {e}")


@app.post("/api/ingest-jobs/{job_id}/complete")
def complete_ingest_upload(job_id: str):
    """Finalize a browser upload job so the ingest worker can consume it."""
    try:
        return _get_registry().complete_ingest_upload(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    except IngestStateConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("complete_ingest_upload failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to complete ingest upload: {e}")


@app.post("/api/ingest-jobs/{job_id}/scan-server-dir")
def scan_ingest_server_dir(job_id: str, req: Optional[IngestServerDirRequest] = None):
    """Queue discovery from the domain's configured docs_dir for the ingest worker."""
    req = req or IngestServerDirRequest()
    try:
        return _get_registry().queue_server_dir_ingest(job_id, limit=req.limit, batch_size=req.batch_size)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("scan_ingest_server_dir failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to queue server directory ingest: {e}")


@app.get("/api/ingest-jobs/{job_id}")
def get_ingest_job(job_id: str):
    try:
        job = _get_registry().get_ingest_job(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("get_ingest_job failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to get ingest job: {e}")
    if not job:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    return job


@app.post("/api/ingest-jobs/{job_id}/retry")
def retry_ingest_job(job_id: str):
    try:
        return _get_registry().retry_ingest_job(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.exception("retry_ingest_job failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to retry ingest job: {e}")


@app.post("/api/ingest-jobs/{job_id}/cancel")
def cancel_ingest_job(job_id: str):
    try:
        return _get_registry().cancel_ingest_job(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.exception("cancel_ingest_job failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to cancel ingest job: {e}")


@app.post("/api/reindex-jobs", status_code=202)
def create_reindex_job(req: ReindexJobCreateRequest):
    """Queue changed documents for background indexing."""
    try:
        return _get_registry().create_reindex_job(
            req.domain,
            changed_only=req.changed_only,
            index_target=req.index_target,
        )
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        status_code = 404 if str(e).startswith("Unknown domain=") else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception as e:
        logger.exception("create_reindex_job failed for domain=%s", req.domain)
        raise HTTPException(status_code=500, detail=f"Failed to create reindex job: {e}")


@app.get("/api/reindex-jobs")
def list_reindex_jobs(
    domain: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    """List recent background reindex jobs, optionally filtered by domain or status."""
    try:
        return _get_registry().list_reindex_jobs(domain=domain, status=status)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_reindex_jobs failed")
        raise HTTPException(status_code=500, detail=f"Failed to list reindex jobs: {e}")


@app.get("/api/reindex-jobs/{job_id}")
def get_reindex_job(job_id: str):
    try:
        job = _get_registry().get_reindex_job(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("get_reindex_job failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to get reindex job: {e}")
    if not job:
        raise HTTPException(status_code=404, detail="Reindex job not found")
    return job


@app.post("/api/reindex-jobs/{job_id}/retry")
def retry_reindex_job(job_id: str):
    try:
        return _get_registry().retry_reindex_job(job_id)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Reindex job not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.exception("retry_reindex_job failed for job_id=%s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to retry reindex job: {e}")


@app.get("/api/query-expansions", response_model=list[QueryExpansionItem])
def list_query_expansions(domain: Optional[str] = Query(None)):
    """List persisted query expansions, optionally filtered by domain."""
    try:
        _get_registry().init_schema()
        return query_expansion_cache.list_all(domain)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_query_expansions failed")
        raise HTTPException(status_code=500, detail=f"Failed to list query expansions: {e}")


@app.post("/api/query-expansions", response_model=QueryExpansionItem)
def create_or_update_query_expansion(req: QueryExpansionRequest):
    """Create or update one query expansion entry."""
    try:
        domain_cache.get_config(req.domain)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown domain={req.domain!r}")
    try:
        _get_registry().init_schema()
        return query_expansion_cache.upsert(req.domain, req.source_term, req.expanded_terms)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("create_or_update_query_expansion failed")
        raise HTTPException(status_code=500, detail=f"Failed to save query expansion: {e}")


@app.delete("/api/query-expansions/{expansion_id}")
def delete_query_expansion(expansion_id: str):
    """Delete one query expansion entry."""
    try:
        _get_registry().init_schema()
        query_expansion_cache.delete(expansion_id)
        return {"deleted": True, "id": expansion_id}
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Query expansion not found")
    except Exception as e:
        logger.exception("delete_query_expansion failed for expansion_id=%s", expansion_id)
        raise HTTPException(status_code=500, detail=f"Failed to delete query expansion: {e}")


@app.post("/api/query-expansions/reload")
def reload_query_expansions():
    """Force reload the process-wide query expansion cache from PostgreSQL."""
    try:
        _get_registry().init_schema()
        query_expansion_cache.refresh()
        return {"reloaded": True}
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("reload_query_expansions failed")
        raise HTTPException(status_code=500, detail=f"Failed to reload query expansions: {e}")


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


@app.post("/api/docs/reindex")
def reindex_changed_docs(
    domain: str = Query(..., description="Domain whose changed documents should be reindexed"),
    changed_only: bool = Query(True, description="Only index enabled documents that require indexing"),
    index_target: str = Query("both", pattern="^(both|vector|bm25)$"),
):
    """Compatibility endpoint that now queues a background reindex job."""
    try:
        return _get_registry().create_reindex_job(
            domain,
            changed_only=changed_only,
            index_target=index_target,
        )
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        status_code = 404 if str(e).startswith("Unknown domain=") else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception as e:
        logger.exception("reindex_changed_docs failed for domain=%s", domain)
        raise HTTPException(status_code=500, detail=f"Failed to queue reindex job: {e}")


@app.post("/api/docs/{document_id}/reindex")
def reindex_doc(
    document_id: str,
    index_target: str = Query("both", pattern="^(both|vector|bm25)$"),
):
    try:
        return PerDocumentIndexer().index_document(document_id, target=index_target)
    except DocumentNotFound:
        raise HTTPException(status_code=404, detail="Document not found")
    except DocumentDisabled as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("reindex_doc failed for document_id=%s", document_id)
        raise HTTPException(status_code=502, detail=f"Failed to reindex document: {e}")


@app.delete("/api/docs/{document_id}/index")
def delete_doc_index(document_id: str):
    try:
        return PerDocumentIndexer().delete_document_index(document_id)
    except DocumentNotFound:
        raise HTTPException(status_code=404, detail="Document not found")
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("delete_doc_index failed for document_id=%s", document_id)
        raise HTTPException(status_code=502, detail=f"Failed to delete document index: {e}")


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


@app.get("/api/docs/{document_id}/chunks")
def list_doc_chunks(
    document_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: Optional[str] = Query(None, description="Qdrant scroll offset returned by next_offset"),
):
    try:
        return PerDocumentIndexer().list_document_chunks(document_id, limit=limit, offset=offset)
    except DocumentNotFound:
        raise HTTPException(status_code=404, detail="Document not found")
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_doc_chunks failed for document_id=%s", document_id)
        raise HTTPException(status_code=502, detail=f"Failed to list document chunks: {e}")


@app.put("/api/docs/{document_id}/content")
async def update_doc_content(
    document_id: str,
    reindex: bool = Query(False, description="Automatically reindex after content update"),
    file: Optional[UploadFile] = File(None, description="File upload (multipart/form-data)"),
    content: Optional[str] = Form(None, description="Text content (alternative to file upload)"),
):
    """Update document content with a new version.

    Accepts either:
    - multipart/form-data with a `file` field
    - multipart/form-data with a `content` text field
    - JSON body with a `content` field (via application/x-www-form-urlencoded)

    After update, call POST /api/docs/{document_id}/reindex to apply changes,
    or pass ?reindex=true to auto-reindex.
    """
    try:
        content_bytes: bytes | None = None
        filename: str | None = None

        if file is not None:
            content_bytes = await file.read()
            filename = file.filename
        elif content is not None:
            content_bytes = content.encode("utf-8")
            filename = "content.md"
        else:
            raise HTTPException(status_code=400, detail="Either 'file' or 'content' field is required")

        registry = _get_registry()
        result = registry.update_document_content(
            document_id,
            content_bytes,
            filename=filename,
        )

        if reindex and result.get("status") == "changed":
            try:
                index_result = PerDocumentIndexer().index_document(document_id)
                result["reindex"] = index_result
            except Exception as exc:
                logger.exception("Auto-reindex failed after content update for %s", document_id)
                result["reindex"] = {"error": str(exc)}

        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("update_doc_content failed for document_id=%s", document_id)
        raise HTTPException(status_code=500, detail=f"Failed to update document content: {e}")


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


@app.delete("/api/libraries/{library_id}")
def delete_library(library_id: str):
    """Soft-delete an imported library after removing its derived indexes."""
    try:
        registry = _get_registry()
        library = registry.get_library(library_id)
        if not library:
            raise HTTPException(status_code=404, detail="Library not found")
        if library["source_type"] != "archive":
            raise HTTPException(status_code=409, detail="Only imported archive libraries can be deleted")
        for document in library["documents"]:
            PerDocumentIndexer().delete_document_index(str(document["id"]))
        return registry.soft_delete_imported_library(library_id)
    except HTTPException:
        raise
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Library not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.exception("delete_library failed for library_id=%s", library_id)
        raise HTTPException(status_code=502, detail=f"Failed to delete imported library: {e}")


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


@app.get("/api/index/jobs")
def list_index_jobs(
    domain: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List latest per-document index states; this is not historical job logging."""
    try:
        return _get_registry().list_index_jobs(domain=domain, status=status, limit=limit, offset=offset)
    except RegistryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("list_index_jobs failed")
        raise HTTPException(status_code=500, detail=f"Failed to list index jobs: {e}")


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
    try:
        domain_cache.get_config(domain)
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown domain={domain!r}; available: {[item['domain_key'] for item in domain_cache.list_domains()]}",
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
            rerank=req.rerank,
            debug=req.debug,
        )
    except Exception as e:
        logger.exception("rag_query failed for domain=%s query=%r", domain, req.query)
        raise HTTPException(status_code=502, detail=f"RAG query failed: {e}")

    items = [RagResultItem(**r) for r in result["results"]]
    trace_obj = None
    if req.debug and "trace" in result:
        raw_trace = result["trace"]
        trace_obj = RetrievalTrace(
            query=raw_trace.get("query", ""),
            domain=raw_trace.get("domain", ""),
            method=raw_trace.get("method", ""),
            query_symbols=raw_trace.get("query_symbols", []),
            query_expansion=raw_trace.get("query_expansion", []),
            expanded_bm25_query=raw_trace.get("expanded_bm25_query", ""),
            semantic_candidates=_build_trace_stage(raw_trace.get("semantic_candidates")) if raw_trace.get("semantic_candidates") else None,
            bm25_candidates=_build_trace_stage(raw_trace.get("bm25_candidates")) if raw_trace.get("bm25_candidates") else None,
            fusion=_build_trace_stage(raw_trace.get("fusion")) if raw_trace.get("fusion") else None,
            boosts=_build_trace_stage(raw_trace.get("boosts")) if raw_trace.get("boosts") else None,
            rerank=_build_trace_stage(raw_trace.get("rerank")) if raw_trace.get("rerank") else None,
            final=_build_trace_stage(raw_trace.get("final")) if raw_trace.get("final") else None,
        )
    return RagQueryResponse(
        query=req.query,
        domain=domain,
        topK=req.topK,
        method=req.method,
        context=result["context"],
        results=items,
        trace=trace_obj,
    )


def _build_trace_stage(raw: dict) -> TraceStage:
    """Convert a raw trace stage dict to a TraceStage model."""
    entries = [
        TraceStageEntry(
            rank=e.get("rank", 0),
            score=e.get("score", 0.0),
            source_file=e.get("source_file", ""),
            context=e.get("context", ""),
            text_len=e.get("text_len", 0),
            symbol_matches=e.get("symbol_matches"),
            bm25_rank=e.get("bm25_rank"),
            bm25_score=e.get("bm25_score"),
        )
        for e in raw.get("top", [])
    ]
    return TraceStage(count=raw.get("count", 0), top=entries)


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
