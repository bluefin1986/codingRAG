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

# ── 领域/语言注册表 ──
# 新增语言/技术栈时，只需要在这里加一个 entry。
DOMAIN_REGISTRY: Dict[str, Dict[str, Any]] = {
    "harmonyos": {
        "display_name": "HarmonyOS / ArkTS",
        "language": "ArkTS",
        "docs_dir": PROJECT_ROOT.parent / "harmonyos-docs-fetcher" / "harmonyos-docs-full",
        "collection": "harmonyos_docs",
        "embedding_model": "BAAI/bge-large-zh-v1.5",
        "embedding_model_name": "bge-large-zh-v1.5",
        "embedding_dim": 1024,
        "rerank_model_name": "bge-reranker-base",
        "prompt_role": "鸿蒙开发专家",
        # HarmonyOS docs currently have ~90k chunks; loading a full in-memory BM25
        # index is expensive and has caused the API worker to be killed. Keep
        # semantic/rerank online first; re-enable BM25 after index optimization.
        "bm25_enabled": True,
        "bm25_weight": 0.7,
        "path_boost_per_match": 0.0,
        "noise_patterns": [
            r"收起自动换行深色代码主题复制\s*",
            r"\[外链图片[^\]]*\]",
            r"!\[image\]\([^\)]*\)",
            r"https://alliance-communityfile[^\s]*",
            r"https://developer\.huawei\.com/consumer/cn/doc/[^\s\)]*",
        ],
    },
    "ios": {
        "display_name": "iOS / UIKit / Objective-C",
        "language": "Objective-C",
        "docs_dir": PROJECT_ROOT.parent / "ios-docs" / "uikit",
        "collection": "ios_docs",
        "embedding_model": "BAAI/bge-m3",
        "embedding_model_name": "bge-m3",
        "embedding_dim": 1024,
        "rerank_model_name": "bge-reranker-base",
        "prompt_role": "iOS UIKit / Objective-C 开发专家",
        "bm25_enabled": True,
        "bm25_weight": 0.1,
        "path_boost_per_match": 0.0,
        "noise_patterns": [
            r'title: "This page requires JavaScript\."\n',
            r"(?m)^- \[Documentation\]\([^\)]*\)\s*$",
        ],
    },
    "redis62": {
        "display_name": "Redis 6.2",
        "language": "Redis",
        "docs_dir": PROJECT_ROOT.parent / "redis-docs-md" / "redis62",
        "collection": "redis62_docs",
        "embedding_model": "BAAI/bge-m3",
        "embedding_model_name": "bge-m3",
        "embedding_dim": 1024,
        "rerank_model_name": "bge-reranker-base",
        "prompt_role": "Redis 6.2 技术专家",
        "bm25_enabled": True,
        "bm25_weight": 0.3,
        "path_boost_per_match": 0.2,
        "noise_patterns": [],
    },
    "kafka28": {
        "display_name": "Apache Kafka 2.8",
        "language": "Kafka",
        "docs_dir": PROJECT_ROOT.parent / "kafka-docs-md" / "kafka28",
        "collection": "kafka28_docs",
        "embedding_model": "BAAI/bge-m3",
        "embedding_model_name": "bge-m3",
        "embedding_dim": 1024,
        "rerank_model_name": "bge-reranker-base",
        "prompt_role": "Apache Kafka 2.8 技术专家",
        "bm25_enabled": True,
        "bm25_weight": 0.3,
        "path_boost_per_match": 0.2,
        "noise_patterns": [],
    },
    "nginx": {
        "display_name": "NGINX official docs",
        "language": "NGINX",
        "docs_dir": PROJECT_ROOT.parent / "nginx-docs-md" / "nginx",
        "collection": "nginx_docs",
        "embedding_model": "BAAI/bge-m3",
        "embedding_model_name": "bge-m3",
        "embedding_dim": 1024,
        "rerank_model_name": "bge-reranker-base",
        "prompt_role": "NGINX 配置与模块专家",
        "bm25_enabled": True,
        "bm25_weight": 0.3,
        "path_boost_per_match": 0.2,
        "noise_patterns": [],
    },
}

DEFAULT_DOMAIN = "ios"
ACTIVE_DOMAIN = os.getenv("CODING_RAG_DOMAIN", DEFAULT_DOMAIN).strip().lower()
if ACTIVE_DOMAIN not in DOMAIN_REGISTRY:
    known = ", ".join(sorted(DOMAIN_REGISTRY))
    raise ValueError(f"Unknown CODING_RAG_DOMAIN={ACTIVE_DOMAIN!r}; known domains: {known}")


def get_domain_config(domain: str | None = None) -> Dict[str, Any]:
    """返回指定领域配置，环境变量可覆盖常用字段。"""
    name = (domain or ACTIVE_DOMAIN).strip().lower()
    if name not in DOMAIN_REGISTRY:
        known = ", ".join(sorted(DOMAIN_REGISTRY))
        raise ValueError(f"Unknown domain={name!r}; known domains: {known}")

    cfg = dict(DOMAIN_REGISTRY[name])
    prefix = f"CODING_RAG_{name.upper()}_"

    cfg["domain"] = name
    cfg["docs_dir"] = _path(os.getenv(prefix + "DOCS_DIR", os.getenv("CODING_RAG_DOCS_DIR", str(cfg["docs_dir"]))))
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


ACTIVE_DOMAIN_CONFIG = get_domain_config()

# ── 当前活动领域的兼容导出 ──
DOCS_DIR = ACTIVE_DOMAIN_CONFIG["docs_dir"]
GUIDES_DIR = DOCS_DIR / "guides"
REFERENCES_DIR = DOCS_DIR / "references"
OUTPUT_DIR = ACTIVE_DOMAIN_CONFIG["output_dir"]
CHUNKS_FILE = OUTPUT_DIR / "chunks.jsonl"

COLLECTION_NAME = ACTIVE_DOMAIN_CONFIG["collection"]
EMBEDDING_MODEL = ACTIVE_DOMAIN_CONFIG["embedding_model"]
EMBEDDING_MODEL_NAME = ACTIVE_DOMAIN_CONFIG["embedding_model_name"]
EMBEDDING_DIM = ACTIVE_DOMAIN_CONFIG["embedding_dim"]
RERANK_MODEL_NAME = ACTIVE_DOMAIN_CONFIG["rerank_model_name"]
PROMPT_ROLE = ACTIVE_DOMAIN_CONFIG["prompt_role"]
NOISE_PATTERNS = ACTIVE_DOMAIN_CONFIG.get("noise_patterns", [])

# ── Chunking ──
CHUNK_MAX_TOKENS = int(os.getenv("CODING_RAG_CHUNK_MAX_TOKENS", "800"))
CHUNK_MIN_TOKENS = int(os.getenv("CODING_RAG_CHUNK_MIN_TOKENS", "200"))
CHUNK_OVERLAP_TOKENS = int(os.getenv("CODING_RAG_CHUNK_OVERLAP_TOKENS", "100"))
