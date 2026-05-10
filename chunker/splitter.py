"""
语义感知的 Markdown 切分器

将 parse_blocks 输出的 Block 列表切分为 chunk 列表。
核心规则：
1. 代码块是不可分割原子
2. 代码块向上合并（包含前面的标题和说明文字）
3. 按标题层级递归切分
4. 每个 chunk 开头注入上下文前缀
5. chunk 大小控制：500-800 tokens
6. 相邻 chunk 重叠约 100 tokens
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .parser import Block

# ── Token 估算 ──

# 中文字符正则
RE_CHINESE = re.compile(r"[\u4e00-\u9fff]")

# 尝试加载 tiktoken，回退到简单估算
try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")
    def estimate_tokens(text: str) -> int:
        return len(_ENCODER.encode(text))
except ImportError:
    _ENCODER = None
    def estimate_tokens(text: str) -> int:
        """
        简单 token 估算（无 tiktoken 时的回退）：
        - 中文字符：1字 ≈ 2 tokens
        - 英文/数字单词：1词 ≈ 1.3 tokens
        """
        chinese_chars = len(RE_CHINESE.findall(text))
        no_chinese = RE_CHINESE.sub(" ", text)
        words = no_chinese.split()
        return int(chinese_chars * 2 + len(words) * 1.3)


# ── 常量 ──

CONTEXT_PREFIX_TEMPLATE = "文档: {context}\n\n"
DEFAULT_MAX_TOKENS = 800
DEFAULT_MIN_TOKENS = 200
DEFAULT_OVERLAP_TOKENS = 100


# ── Chunk 数据结构 ──

@dataclass
class Chunk:
    """一个切分后的文本块"""
    text: str
    metadata: dict = field(default_factory=dict)


def _blocks_text(blocks: List[Block]) -> str:
    """将多个 block 的内容拼接为文本"""
    return "\n\n".join(b.content for b in blocks)


def _make_chunk(text: str, context: str, source_file: str,
                chunk_index: int, has_code: bool) -> Chunk:
    """构造一个 Chunk，自动注入上下文前缀"""
    prefix = CONTEXT_PREFIX_TEMPLATE.format(context=context)
    full_text = prefix + text
    return Chunk(
        text=full_text,
        metadata={
            "context": context,
            "has_code": has_code,
            "source_file": source_file,
            "chunk_index": chunk_index,
        },
    )


def _has_code_block(blocks: List[Block]) -> bool:
    return any(b.type == "code" for b in blocks)


def _group_by_heading(blocks: List[Block], level: int) -> List[List[Block]]:
    """
    按指定标题层级将 blocks 分组。
    返回的每个子列表以该层级标题（或更低层级）开头。
    """
    groups: List[List[Block]] = []
    current: List[Block] = []

    for b in blocks:
        if b.type == "heading" and b.level == level:
            if current:
                groups.append(current)
            current = [b]
        else:
            current.append(b)

    if current:
        groups.append(current)

    return groups


def _split_group_recursive(blocks: List[Block], max_tokens: int,
                           min_tokens: int) -> List[List[Block]]:
    """
    递归切分一组 blocks：
    1. 如果总 token 数 <= max_tokens，直接返回
    2. 否则尝试按 ## 切分，其次 ###，依此类推
    3. 如果所有标题层级都无法切分（单个 section 太大），
       则按段落边界强制切分
    """
    total = estimate_tokens(_blocks_text(blocks))
    if total <= max_tokens:
        return [blocks]

    # 尝试按标题层级切分（优先 ##，其次 ### ...）
    for level in range(2, 7):
        groups = _group_by_heading(blocks, level)
        if len(groups) > 1:
            result: List[List[Block]] = []
            for g in groups:
                result.extend(_split_group_recursive(g, max_tokens, min_tokens))
            return result

    # 无法按标题切分 → 按段落边界强制切分
    return _force_split_by_paragraphs(blocks, max_tokens, min_tokens)


def _force_split_by_paragraphs(blocks: List[Block], max_tokens: int,
                               min_tokens: int) -> List[List[Block]]:
    """
    按段落边界强制切分。
    核心保证：
    - 代码块/表格是不可分割原子
    - 代码块必须与前面的标题/段落在同一组（语义绑定）
    """
    result: List[List[Block]] = []
    current: List[Block] = []
    current_tokens = 0

    for b in blocks:
        block_tokens = estimate_tokens(b.content)

        # 代码块/表格：不可分割原子，必须与前面的内容绑定
        if b.type in ("code", "table"):
            # 如果当前组加上这个代码块会超限
            if current_tokens + block_tokens > max_tokens:
                # 如果当前组已有内容，先保存（代码块跟前面的内容在一起）
                if current:
                    current.append(b)
                    current_tokens += block_tokens
                    result.append(current)
                    current = []
                    current_tokens = 0
                    continue
                else:
                    # 当前组为空，代码块单独成组（超大代码块）
                    current.append(b)
                    result.append(current)
                    current = []
                    current_tokens = 0
                    continue
            else:
                # 未超限，直接加入当前组
                current.append(b)
                current_tokens += block_tokens
                continue

        # 标题块：如果当前组加上标题会超限
        if b.type == "heading":
            if current_tokens + block_tokens > max_tokens and current_tokens >= min_tokens:
                result.append(current)
                current = []
                current_tokens = 0
            current.append(b)
            current_tokens += block_tokens
            continue

        # 普通段落/列表：如果加上会超限，先保存当前组
        if current_tokens + block_tokens > max_tokens and current_tokens >= min_tokens:
            result.append(current)
            current = []
            current_tokens = 0

        current.append(b)
        current_tokens += block_tokens

    if current:
        result.append(current)

    return result


def _add_overlap(chunks_data: List[dict], overlap_tokens: int) -> List[dict]:
    """
    为相邻 chunk 添加重叠。
    从上一个 chunk 的末尾提取约 overlap_tokens 的文本，
    添加到下一个 chunk 的上下文前缀之后。
    """
    if len(chunks_data) <= 1:
        return chunks_data

    result = [chunks_data[0]]
    for i in range(1, len(chunks_data)):
        prev_text = chunks_data[i - 1]["text"]
        curr_data = chunks_data[i].copy()
        curr_text = curr_data["text"]

        # 从上一个 chunk 末尾提取重叠文本
        overlap_text = _extract_tail_overlap(prev_text, overlap_tokens)
        if overlap_text:
            # 在上下文前缀之后插入重叠
            # 找到第一个 \n\n 的位置（前缀后面）
            prefix_end = curr_text.find("\n\n\n")
            if prefix_end != -1:
                curr_data["text"] = (
                    curr_text[:prefix_end + 2]
                    + "\n[接上文]\n" + overlap_text + "\n\n"
                    + curr_text[prefix_end + 2:]
                )
        result.append(curr_data)

    return result


def _extract_tail_overlap(text: str, target_tokens: int) -> str:
    """从文本末尾提取约 target_tokens 的内容"""
    paragraphs = text.split("\n\n")
    collected: list[str] = []
    tokens = 0

    for p in reversed(paragraphs):
        p_tokens = estimate_tokens(p)
        if tokens + p_tokens > target_tokens and collected:
            break
        collected.append(p)
        tokens += p_tokens

    collected.reverse()
    return "\n\n".join(collected)


def split_blocks(blocks: List[Block], source_file: str = "",
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 min_tokens: int = DEFAULT_MIN_TOKENS,
                 overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> List[Chunk]:
    """
    将 Block 列表切分为 Chunk 列表。

    算法：
    1. 代码块向上合并（与前面的标题/说明文字绑定）
    2. 按标题层级递归切分
    3. 注入上下文前缀
    4. 相邻 chunk 重叠
    """
    if not blocks:
        return []

    # Step 1: 代码块向上合并
    merged = _merge_code_with_context(blocks)

    # Step 2: 递归切分
    groups = _split_group_recursive(merged, max_tokens, min_tokens)

    # Step 3: 构造 Chunk 列表
    context = ""
    if merged:
        # 取第一个 block 的 context 作为文档级 context
        for b in merged:
            if b.context:
                context = b.context
                break

    chunks_data: List[dict] = []
    for idx, group in enumerate(groups):
        group_text = _blocks_text(group)
        group_context = context
        # 尝试从组内获取更精确的 context
        for b in group:
            if b.context:
                group_context = b.context
                break

        chunk = _make_chunk(
            text=group_text,
            context=group_context,
            source_file=source_file,
            chunk_index=idx,
            has_code=_has_code_block(group),
        )
        chunks_data.append({"text": chunk.text, "metadata": chunk.metadata})

    # Step 4: 添加重叠
    chunks_data = _add_overlap(chunks_data, overlap_tokens)

    return [Chunk(text=d["text"], metadata=d["metadata"]) for d in chunks_data]


def _merge_code_with_context(blocks: List[Block]) -> List[Block]:
    """
    代码块向上合并：将代码块与它前面的标题和段落绑定为一个语义单元。
    具体做法：代码块前的标题和段落保持独立，但确保切分时它们不会被分开。
    通过在代码块的 content 中注入前面的上下文来实现。

    简化实现：不修改 Block 结构，而是在切分阶段确保代码块
    与其前面的标题/段落在同一个 group 中。
    这里通过添加标记来辅助切分器识别绑定关系。
    """
    # 找出所有代码块的位置
    code_indices = set()
    for i, b in enumerate(blocks):
        if b.type == "code":
            code_indices.add(i)

    # 标记代码块前面的标题/段落为 "应与代码块绑定"
    # 实际合并通过切分逻辑保证（代码块不可分割 + 向上合并）
    # 这里返回原始 blocks，合并逻辑在 _force_split_by_paragraphs 中处理
    return blocks
