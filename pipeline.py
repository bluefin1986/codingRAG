#!/usr/bin/env python3
"""
HarmonyRAG 分块流水线

扫描文档目录，对所有 .md 文件执行语义分块，输出 JSONL 和统计信息。

用法：
    python pipeline.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from tqdm import tqdm

from config import GUIDES_DIR, REFERENCES_DIR, OUTPUT_DIR, CHUNK_MAX_TOKENS, CHUNK_MIN_TOKENS, CHUNK_OVERLAP_TOKENS
from chunker import chunk_directory


def main() -> None:
    start_time = time.time()

    # ── 1. 输出目录 ──
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    chunks_path = OUTPUT_DIR / "chunks.jsonl"
    stats_path = OUTPUT_DIR / "stats.json"

    # ── 2. 扫描并分块 ──
    all_chunks = []
    file_count = 0

    scan_targets = [
        (GUIDES_DIR, "guides"),
        (REFERENCES_DIR, "references"),
    ]

    for dir_path, category in scan_targets:
        if not dir_path.is_dir():
            print(f"⚠️  目录不存在，跳过: {dir_path}")
            continue

        # 先统计文件数
        md_files = sorted(dir_path.rglob("*.md"))
        file_count += len(md_files)

        print(f"\n📂 扫描 {category}: {dir_path}")
        print(f"   找到 {len(md_files)} 个 .md 文件")

        # 分块（带进度条）
        chunks = chunk_directory(
            dir_path=dir_path,
            category=category,
            max_tokens=CHUNK_MAX_TOKENS,
            min_tokens=CHUNK_MIN_TOKENS,
            overlap_tokens=CHUNK_OVERLAP_TOKENS,
        )
        all_chunks.extend(chunks)
        print(f"   生成 {len(chunks)} 个 chunks")

    if not all_chunks:
        print("\n❌ 没有生成任何 chunks，请检查文档目录路径。")
        sys.exit(1)

    # ── 3. 写入 chunks.jsonl ──
    print(f"\n📝 写入 {chunks_path}")
    with open(chunks_path, "w", encoding="utf-8") as f:
        for chunk in tqdm(all_chunks, desc="写入 chunks.jsonl", unit="chunk"):
            record = {
                "text": chunk.text,
                "metadata": chunk.metadata,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ── 4. 统计信息 ──
    token_sizes = []
    for chunk in all_chunks:
        # 用 splitter 的估算函数
        from chunker.splitter import estimate_tokens
        token_sizes.append(estimate_tokens(chunk.text))

    code_chunks = sum(1 for c in all_chunks if c.metadata.get("has_code"))
    categories = defaultdict(int)
    source_files = set()
    for c in all_chunks:
        cat = c.metadata.get("category", "unknown")
        categories[cat] += 1
        source_files.add(c.metadata.get("source_file", ""))

    elapsed = time.time() - start_time

    stats = {
        "total_files": file_count,
        "total_chunks": len(all_chunks),
        "chunks_with_code": code_chunks,
        "chunks_without_code": len(all_chunks) - code_chunks,
        "avg_chunk_tokens": round(sum(token_sizes) / len(token_sizes), 1) if token_sizes else 0,
        "min_chunk_tokens": min(token_sizes) if token_sizes else 0,
        "max_chunk_tokens": max(token_sizes) if token_sizes else 0,
        "median_chunk_tokens": sorted(token_sizes)[len(token_sizes) // 2] if token_sizes else 0,
        "total_tokens": sum(token_sizes),
        "unique_source_files": len(source_files),
        "by_category": dict(categories),
        "config": {
            "max_tokens": CHUNK_MAX_TOKENS,
            "min_tokens": CHUNK_MIN_TOKENS,
            "overlap_tokens": CHUNK_OVERLAP_TOKENS,
        },
        "elapsed_seconds": round(elapsed, 2),
    }

    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    # ── 5. 打印摘要 ──
    print(f"\n✅ 完成！耗时 {elapsed:.1f}s")
    print(f"   文件数:      {stats['total_files']}")
    print(f"   Chunk 数:    {stats['total_chunks']}")
    print(f"   含代码 Chunk: {stats['chunks_with_code']}")
    print(f"   平均 Token:   {stats['avg_chunk_tokens']}")
    print(f"   最小 Token:   {stats['min_chunk_tokens']}")
    print(f"   最大 Token:   {stats['max_chunk_tokens']}")
    print(f"   总 Token:     {stats['total_tokens']:,}")
    print(f"   分类分布:     {dict(categories)}")
    print(f"\n📄 输出:")
    print(f"   {chunks_path}")
    print(f"   {stats_path}")


if __name__ == "__main__":
    main()
