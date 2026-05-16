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
from typing import Dict

from fastapi import FastAPI, HTTPException
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
from api.schemas import RagQueryRequest, RagQueryResponse, RagResultItem  # noqa: E402

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


# ── Endpoints ──

@app.get("/health")
def health():
    return {
        "status": "ok",
        "default_domain": ACTIVE_DOMAIN,
        "available_domains": sorted(DOMAIN_REGISTRY.keys()),
    }


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
