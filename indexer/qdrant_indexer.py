"""Qdrant 索引模块：读取 chunks.jsonl → embedding → 写入 Qdrant"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

# Allow running as: python3 indexer/qdrant_indexer.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from tqdm import tqdm

from config import (
    ACTIVE_DOMAIN,
    CHUNKS_FILE,
    COLLECTION_NAME,
    DOMAIN_REGISTRY,
    EMBEDDING_API_BASE,
    EMBEDDING_DIM,
    EMBEDDING_MODEL_NAME,
    NOISE_PATTERNS as NOISE_PATTERN_STRINGS,
    OUTPUT_DIR,
    QDRANT_HOST,
    QDRANT_PORT,
    QDRANT_API_KEY,
    RERANK_MODEL_NAME,
)

logger = logging.getLogger(__name__)

# ── 噪声清洗 ──
NOISE_PATTERNS = [re.compile(pattern) for pattern in NOISE_PATTERN_STRINGS]


def clean_text(text: str) -> str:
    """清洗文档噪声。"""
    for pat in NOISE_PATTERNS:
        text = pat.sub("", text)
    # 合并连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_text(text: str, max_chars: int = 1500) -> str:
    """预截断 embedding 输入；payload 保留完整文本。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


# ── Embedding API ──
def embed_texts(texts: List[str], batch_size: int = 32, max_retries: int = 3) -> List[List[float]]:
    """调用 aimodels 服务批量生成 embedding，带重试和进度显示。"""
    import time as _time
    all_embeddings: List[List[float]] = []
    client = httpx.Client(timeout=900.0)

    total_batches = (len(texts) + batch_size - 1) // batch_size
    progress = tqdm(
        range(0, len(texts), batch_size),
        total=total_batches,
        desc="Embedding",
        unit="batch",
    )

    for batch_no, i in enumerate(progress, 1):
        batch = texts[i : i + batch_size]
        progress.set_postfix({"vectors": len(all_embeddings), "batch_size": len(batch)})
        for attempt in range(max_retries):
            try:
                resp = client.post(
                    f"{EMBEDDING_API_BASE}/api/v1/embeddings",
                    json={"input": batch, "model": EMBEDDING_MODEL_NAME},
                )
                resp.raise_for_status()
                data = resp.json()
                all_embeddings.extend([d["embedding"] for d in data["data"]])
                break
            except (httpx.ReadTimeout, httpx.ConnectError) as exc:
                if attempt < max_retries - 1:
                    wait = 5 * (attempt + 1)
                    logger.warning(
                        "embedding batch %d/%d attempt %d failed: %s, retrying in %ds",
                        batch_no,
                        total_batches,
                        attempt + 1,
                        exc,
                        wait,
                    )
                    _time.sleep(wait)
                else:
                    raise

    client.close()
    return all_embeddings


# ── Qdrant ──
def ensure_collection(client: httpx.Client, recreate: bool = False) -> None:
    """确保 Qdrant collection 存在。"""
    url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{COLLECTION_NAME}"

    if recreate:
        client.delete(url)
        logger.info("deleted existing collection: %s", COLLECTION_NAME)

    # 检查是否存在
    resp = client.get(url)
    if resp.status_code == 200:
        logger.info("collection already exists: %s", COLLECTION_NAME)
        return

    # 创建
    create_resp = client.put(
        url,
        json={
            "vectors": {
                "size": EMBEDDING_DIM,
                "distance": "Cosine",
            },
            "optimizers_config": {
                "indexing_threshold": 20000,
            },
        },
    )
    create_resp.raise_for_status()
    logger.info("created collection: %s (dim=%d)", COLLECTION_NAME, EMBEDDING_DIM)


def upsert_points(
    client: httpx.Client,
    points: List[dict],
    batch_size: int = 100,
) -> int:
    """批量写入 points 到 Qdrant。"""
    url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{COLLECTION_NAME}/points"
    total_written = 0

    total_batches = (len(points) + batch_size - 1) // batch_size
    progress = tqdm(
        range(0, len(points), batch_size),
        total=total_batches,
        desc="Qdrant upsert",
        unit="batch",
    )

    for i in progress:
        batch = points[i : i + batch_size]
        resp = client.put(
            url,
            json={"points": batch},
            params={"wait": "true"},
        )
        resp.raise_for_status()
        total_written += len(batch)
        progress.set_postfix({"written": total_written, "batch_size": len(batch)})

    return total_written


# ── 主流程 ──
def load_chunks(filepath: Path) -> List[dict]:
    """加载 chunks.jsonl。"""
    chunks = []
    with open(filepath) as f:
        for line in tqdm(f, desc="Loading chunks", unit="chunk"):
            chunks.append(json.loads(line))
    return chunks


def build_points(chunks: List[dict], embeddings: List[List[float]]) -> List[dict]:
    """构建 Qdrant point 列表。"""
    points = []
    for idx, (chunk, embedding) in enumerate(
        tqdm(zip(chunks, embeddings), total=len(chunks), desc="Building points", unit="point")
    ):
        meta = chunk["metadata"]
        payload = {
            "domain": ACTIVE_DOMAIN,
            "text": chunk["text"],
            "context": meta.get("context", ""),
            "source_file": meta.get("source_file", ""),
            "has_code": meta.get("has_code", False),
            "category": meta.get("category", ""),
            "chunk_index": meta.get("chunk_index", idx),
        }
        points.append({
            "id": idx,
            "vector": embedding,
            "payload": payload,
        })
    return points


def run_indexing(
    chunks_file: Optional[Path] = None,
    recreate: bool = False,
    embed_batch_size: int = 32,
    qdrant_batch_size: int = 100,
) -> dict:
    """完整流程：加载 → 清洗 → embedding → 写入 Qdrant。"""
    filepath = chunks_file or CHUNKS_FILE
    if not filepath.exists():
        raise FileNotFoundError(f"chunks file not found: {filepath}")

    # 1. 加载 chunks
    logger.info("loading chunks from %s ...", filepath)
    chunks = load_chunks(filepath)
    logger.info("loaded %d chunks", len(chunks))

    # 2. 清洗文本
    logger.info("cleaning text...")
    for chunk in tqdm(chunks, desc="Cleaning text", unit="chunk"):
        chunk["text"] = clean_text(chunk["text"])

    # 过滤掉清洗后为空的 chunk
    valid_chunks = [c for c in chunks if c["text"].strip()]
    logger.info("valid chunks after cleaning: %d (filtered %d)", len(valid_chunks), len(chunks) - len(valid_chunks))

    # 3. Embedding
    # 注意：只截断 embedding 输入，不截断 payload text。
    # payload 需要保留完整 chunk，避免长代码示例被写入 Qdrant 时切掉一半。
    logger.info("generating embeddings (batch_size=%d) ...", embed_batch_size)
    texts = [truncate_text(c["text"]) for c in valid_chunks]
    truncated_for_embedding = sum(1 for c in valid_chunks if len(c["text"]) > len(truncate_text(c["text"])))
    if truncated_for_embedding:
        logger.info("truncated %d chunks for embedding input only; payload text remains full", truncated_for_embedding)
    t0 = time.time()
    embeddings = embed_texts(texts, batch_size=embed_batch_size)
    embed_time = time.time() - t0
    logger.info("embedding done: %d vectors in %.1fs (%.1f vectors/s)", len(embeddings), embed_time, len(embeddings) / embed_time)

    # 4. 写入 Qdrant
    qdrant_headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else None
    qdrant_client = httpx.Client(base_url=f"http://{QDRANT_HOST}:{QDRANT_PORT}", headers=qdrant_headers)
    ensure_collection(qdrant_client, recreate=recreate)

    logger.info("building points...")
    points = build_points(valid_chunks, embeddings)

    logger.info("upserting %d points to Qdrant...", len(points))
    t0 = time.time()
    written = upsert_points(qdrant_client, points, batch_size=qdrant_batch_size)
    upsert_time = time.time() - t0
    logger.info("upsert done: %d points in %.1fs", written, upsert_time)

    qdrant_client.close()

    # 5. 统计
    stats = {
        "total_chunks": len(chunks),
        "valid_chunks": len(valid_chunks),
        "filtered_out": len(chunks) - len(valid_chunks),
        "truncated_for_embedding": truncated_for_embedding,
        "payload_text_truncated": False,
        "embed_time_seconds": round(embed_time, 1),
        "upsert_time_seconds": round(upsert_time, 1),
        "domain": ACTIVE_DOMAIN,
        "collection": COLLECTION_NAME,
        "embedding_dim": EMBEDDING_DIM,
        "embedding_model": EMBEDDING_MODEL_NAME,
        "rerank_model": RERANK_MODEL_NAME,
    }

    stats_path = OUTPUT_DIR / "index_stats.json"
    with open(stats_path, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    logger.info("stats saved to %s", stats_path)

    return stats


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Embed chunks and index into Qdrant")
    parser.add_argument("--domain", choices=sorted(DOMAIN_REGISTRY), default=ACTIVE_DOMAIN, help="Domain/language index to build")
    parser.add_argument("--recreate", action="store_true", help="Recreate the selected domain's Qdrant collection")
    parser.add_argument("--batch-size", type=int, default=32, help="Embedding batch size")
    parser.add_argument("--qdrant-batch", type=int, default=100, help="Qdrant upsert batch size")
    args = parser.parse_args()

    if args.domain != ACTIVE_DOMAIN:
        env = os.environ.copy()
        env["CODING_RAG_DOMAIN"] = args.domain
        os.execvpe(sys.executable, [sys.executable, *sys.argv], env)

    logging.info(
        "indexing domain=%s collection=%s embedding=%s rerank=%s chunks=%s",
        ACTIVE_DOMAIN,
        COLLECTION_NAME,
        EMBEDDING_MODEL_NAME,
        RERANK_MODEL_NAME,
        CHUNKS_FILE,
    )

    stats = run_indexing(
        recreate=args.recreate,
        embed_batch_size=args.batch_size,
        qdrant_batch_size=args.qdrant_batch,
    )
    print(json.dumps(stats, indent=2, ensure_ascii=False))
