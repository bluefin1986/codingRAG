"""RAG 查询模块：语义检索 + LLM 回答"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

import httpx

from config import (
    COLLECTION_NAME,
    EMBEDDING_API_BASE,
    EMBEDDING_MODEL_NAME,
    QDRANT_HOST,
    QDRANT_PORT,
)

logger = logging.getLogger(__name__)


def embed_query(query: str) -> List[float]:
    """将查询文本转为 embedding 向量。"""
    client = httpx.Client(timeout=60.0)
    resp = client.post(
        f"{EMBEDDING_API_BASE}/api/v1/embeddings",
        json={"input": [query], "model": EMBEDDING_MODEL_NAME},
    )
    resp.raise_for_status()
    client.close()
    return resp.json()["data"][0]["embedding"]


def search(
    query: str,
    top_k: int = 5,
    category: Optional[str] = None,
    has_code: Optional[bool] = None,
) -> List[dict]:
    """语义检索，返回最相关的 chunks。"""
    query_vector = embed_query(query)

    # 构建过滤条件
    must_filters = []
    if category:
        must_filters.append({
            "key": "category",
            "match": {"value": category},
        })
    if has_code is not None:
        must_filters.append({
            "key": "has_code",
            "match": {"value": has_code},
        })

    search_body = {
        "vector": query_vector,
        "limit": top_k,
        "with_payload": True,
        "with_vector": False,
    }
    if must_filters:
        search_body["filter"] = {"must": must_filters}

    client = httpx.Client(timeout=30.0)
    resp = client.post(
        f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{COLLECTION_NAME}/points/search",
        json=search_body,
    )
    resp.raise_for_status()
    client.close()

    results = []
    for hit in resp.json()["result"]:
        results.append({
            "score": hit["score"],
            "text": hit["payload"]["text"],
            "context": hit["payload"].get("context", ""),
            "source_file": hit["payload"].get("source_file", ""),
            "has_code": hit["payload"].get("has_code", False),
        })

    return results


def format_context(results: List[dict]) -> str:
    """将检索结果格式化为 LLM 上下文。"""
    parts = []
    for i, r in enumerate(results, 1):
        source = r["source_file"]
        context = r["context"]
        parts.append(f"---\n[{i}] 来源: {source}\n分类: {context}\n\n{r['text']}")
    return "\n\n".join(parts)


def rag_query(
    question: str,
    top_k: int = 5,
    category: Optional[str] = None,
    has_code: Optional[bool] = None,
) -> dict:
    """RAG 查询：检索 + 构建 prompt。"""
    results = search(question, top_k=top_k, category=category, has_code=has_code)
    context = format_context(results)

    prompt = f"""你是鸿蒙开发专家。基于以下参考文档回答用户问题。

参考文档：
{context}

用户问题：{question}

请基于参考文档回答。如果文档中有代码示例，请一并给出。如果文档中没有相关信息，请说明。"""

    return {
        "question": question,
        "results": results,
        "context": context,
        "prompt": prompt,
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    question = sys.argv[1] if len(sys.argv) > 1 else "ArkTS 怎么创建一个按钮组件"
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    result = rag_query(question, top_k=top_k)

    print(f"\n问题: {result['question']}")
    print(f"检索到 {len(result['results'])} 个相关文档:\n")
    for i, r in enumerate(result["results"], 1):
        print(f"[{i}] score={r['score']:.4f} | {r['source_file']}")
        print(f"    context: {r['context']}")
        print(f"    has_code: {r['has_code']}")
        print(f"    text preview: {r['text'][:150]}...")
        print()
