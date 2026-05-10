"""HarmonyRAG 配置"""
from pathlib import Path

# ── 路径 ──
PROJECT_ROOT = Path(__file__).parent
DOCS_DIR = PROJECT_ROOT.parent / "harmonyos-docs-fetcher" / "harmonyos-docs-full"
GUIDES_DIR = DOCS_DIR / "guides"
REFERENCES_DIR = DOCS_DIR / "references"
OUTPUT_DIR = PROJECT_ROOT / "output"

# ── Qdrant ──
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "harmonyos_docs"

# ── Embedding ──
EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5"
EMBEDDING_DIM = 1024
EMBEDDING_API_BASE = "http://localhost:8030"  # aimodels 服务地址
EMBEDDING_MODEL_NAME = "bge-large-zh-v1.5"    # API 调用时的 model name

# ── Chunking ──
CHUNK_MAX_TOKENS = 800
CHUNK_MIN_TOKENS = 200
CHUNK_OVERLAP_TOKENS = 100

# ── 输出 ──
CHUNKS_FILE = OUTPUT_DIR / "chunks.jsonl"
