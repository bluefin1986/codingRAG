"""codingRAG 配置：按领域/语言注册 docs、collection、embedding、rerank。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency may be absent in legacy installs
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(PROJECT_ROOT / ".env")


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


# ── 服务 ──
QDRANT_HOST = os.getenv("CODING_RAG_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("CODING_RAG_QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.getenv("CODING_RAG_QDRANT_API_KEY", os.getenv("QDRANT_API_KEY", ""))
AIMODELS_API_BASE = os.getenv("CODING_RAG_AIMODELS_API_BASE", "http://localhost:8030")
EMBEDDING_API_BASE = os.getenv("CODING_RAG_EMBEDDING_API_BASE", AIMODELS_API_BASE)
RERANK_API_BASE = os.getenv("CODING_RAG_RERANK_API_BASE", AIMODELS_API_BASE)

# ── Document Registry / 原文存储 ──
CODING_RAG_DATABASE_URL = os.getenv("CODING_RAG_DATABASE_URL", "").strip()
CODING_RAG_STORAGE_BACKEND = os.getenv("CODING_RAG_STORAGE_BACKEND", "local").strip().lower()
CODING_RAG_SEAWEEDFS_FILER_URL = os.getenv("CODING_RAG_SEAWEEDFS_FILER_URL", "").strip().rstrip("/")
CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL = os.getenv("CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL", CODING_RAG_SEAWEEDFS_FILER_URL).strip().rstrip("/")
CODING_RAG_SEAWEEDFS_BUCKET = os.getenv("CODING_RAG_SEAWEEDFS_BUCKET", "codingrag-originals").strip()
CODING_RAG_SEAWEEDFS_KEY_PREFIX = os.getenv("CODING_RAG_SEAWEEDFS_KEY_PREFIX", "libraries").strip().strip("/")
CODING_RAG_SEAWEEDFS_S3_ENDPOINT = os.getenv("CODING_RAG_SEAWEEDFS_S3_ENDPOINT", "").strip().rstrip("/")
CODING_RAG_IMPORT_BATCH_SIZE = int(os.getenv("CODING_RAG_IMPORT_BATCH_SIZE", "100"))

# ── Elasticsearch / OpenSearch ──
CODING_RAG_ES_URL = os.getenv("CODING_RAG_ES_URL", "").strip().rstrip("/")
CODING_RAG_ES_API_KEY = os.getenv("CODING_RAG_ES_API_KEY", "").strip()

# ── BM25 内存安全阈值 ──
# 当 chunk 数量超过此阈值时，仅索引前 N 条以避免 OOM。
# HarmonyOS 约 90k chunks，全量加载会导致 API worker 被 OOM killer 终止。
BM25_MAX_CHUNKS = int(os.getenv("CODING_RAG_BM25_MAX_CHUNKS", "50000"))

DEFAULT_DOMAIN = "ios"
ACTIVE_DOMAIN = os.getenv("CODING_RAG_DOMAIN", DEFAULT_DOMAIN).strip().lower()


def get_domain_config(domain: str | None = None) -> Dict[str, Any]:
    """Return one PostgreSQL-backed domain config with environment overrides."""
    name = (domain or ACTIVE_DOMAIN).strip().lower()
    from api.registry import domain_cache

    cfg = domain_cache.get_config(name)
    prefix = f"CODING_RAG_{name.upper()}_"

    cfg["domain"] = name
    docs_dir_override = os.getenv(prefix + "DOCS_DIR", os.getenv("CODING_RAG_DOCS_DIR", "")).strip()
    if docs_dir_override:
        cfg["docs_dir"] = _path(docs_dir_override)
    elif cfg.get("docs_dir") is not None:
        cfg["docs_dir"] = _path(cfg["docs_dir"])
    cfg["collection"] = os.getenv(prefix + "COLLECTION", os.getenv("CODING_RAG_COLLECTION_NAME", cfg["collection"]))
    cfg["embedding_model_name"] = os.getenv(prefix + "EMBEDDING_MODEL_NAME", cfg["embedding_model_name"])
    cfg["embedding_model"] = os.getenv(prefix + "EMBEDDING_MODEL", cfg["embedding_model"])
    cfg["embedding_dim"] = int(os.getenv(prefix + "EMBEDDING_DIM", str(cfg["embedding_dim"])))
    cfg["rerank_model_name"] = os.getenv(prefix + "RERANK_MODEL_NAME", cfg["rerank_model_name"])
    cfg["prompt_role"] = os.getenv(prefix + "PROMPT_ROLE", cfg["prompt_role"])
    cfg["bm25_enabled"] = os.getenv(prefix + "BM25_ENABLED", str(cfg.get("bm25_enabled", True))).lower() in ("1", "true", "yes", "on")
    cfg["bm25_weight"] = float(os.getenv(prefix + "BM25_WEIGHT", str(cfg.get("bm25_weight", 0.7))))
    cfg["path_boost_per_match"] = float(os.getenv(prefix + "PATH_BOOST_PER_MATCH", str(cfg.get("path_boost_per_match", 0.2))))
    cfg["noise_patterns"] = cfg.get("noise_patterns", [])
    cfg["output_dir"] = _path(os.getenv(prefix + "OUTPUT_DIR", os.getenv("CODING_RAG_OUTPUT_DIR", str(PROJECT_ROOT / "output" / name))))
    return cfg


# ── Chunking ──
CHUNK_MAX_TOKENS = int(os.getenv("CODING_RAG_CHUNK_MAX_TOKENS", "800"))
CHUNK_MIN_TOKENS = int(os.getenv("CODING_RAG_CHUNK_MIN_TOKENS", "200"))
CHUNK_OVERLAP_TOKENS = int(os.getenv("CODING_RAG_CHUNK_OVERLAP_TOKENS", "100"))
