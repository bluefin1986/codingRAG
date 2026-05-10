"""
codingRAG Markdown 智能分块模块

导出：
- chunk_document(filepath, content) → List[Chunk]
- chunk_directory(dir_path, category) → List[Chunk]
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from .parser import parse_blocks, Block
from .splitter import Chunk, split_blocks, DEFAULT_MAX_TOKENS, DEFAULT_MIN_TOKENS, DEFAULT_OVERLAP_TOKENS


def chunk_document(filepath: str, content: str,
                   max_tokens: int = DEFAULT_MAX_TOKENS,
                   min_tokens: int = DEFAULT_MIN_TOKENS,
                   overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> List[Chunk]:
    """
    对单个 Markdown 文档进行语义分块。

    Args:
        filepath: 文件路径（用于 metadata.source_file）
        content:  Markdown 文本内容
        max_tokens:  单个 chunk 最大 token 数
        min_tokens:  单个 chunk 最小 token 数
        overlap_tokens: 相邻 chunk 重叠 token 数

    Returns:
        Chunk 列表
    """
    blocks = parse_blocks(content)
    return split_blocks(
        blocks,
        source_file=filepath,
        max_tokens=max_tokens,
        min_tokens=min_tokens,
        overlap_tokens=overlap_tokens,
    )


def chunk_directory(dir_path: str | Path, category: str = "",
                    max_tokens: int = DEFAULT_MAX_TOKENS,
                    min_tokens: int = DEFAULT_MIN_TOKENS,
                    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> List[Chunk]:
    """
    遍历目录下所有 .md 文件，返回所有 chunks。

    Args:
        dir_path:  目录路径
        category:  分类标签（如 "guides" / "references"），会写入 metadata
        max_tokens:  单个 chunk 最大 token 数
        min_tokens:  单个 chunk 最小 token 数
        overlap_tokens: 相邻 chunk 重叠 token 数

    Returns:
        所有文件的 Chunk 列表（metadata.source_file 为相对路径）
    """
    dir_path = Path(dir_path)
    all_chunks: List[Chunk] = []

    if not dir_path.is_dir():
        return all_chunks

    # 递归查找所有 .md 文件
    md_files = sorted(dir_path.rglob("*.md"))

    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # 计算相对于 dir_path 的路径
        rel_path = str(md_file.relative_to(dir_path))
        # 如果有 category，加到路径前缀
        if category:
            source_file = f"{category}/{rel_path}"
        else:
            source_file = rel_path

        chunks = chunk_document(
            filepath=source_file,
            content=content,
            max_tokens=max_tokens,
            min_tokens=min_tokens,
            overlap_tokens=overlap_tokens,
        )

        # 如果有 category，追加到 metadata
        if category:
            for c in chunks:
                c.metadata["category"] = category

        all_chunks.extend(chunks)

    return all_chunks
