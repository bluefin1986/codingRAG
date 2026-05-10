"""
Markdown 语义块解析器

将 Markdown 文本解析为语义块（Block）列表。
每个 Block 包含 type、level、content、context 信息。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# ── 行级正则 ──
RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)")                    # # ~ ######
RE_CODE_FENCE = re.compile(r"^```(\w*)")                         # ``` 或 ```lang
RE_TABLE_ROW = re.compile(r"^\|(.+)\|$")                         # | ... |
RE_LIST_BULLET = re.compile(r"^(\s*[-*]\s+)")                    # - / *
RE_LIST_ORDERED = re.compile(r"^(\s*\d+\.\s+)")                  # 1.


@dataclass
class Block:
    """一个语义块"""
    type: str          # 'heading' | 'paragraph' | 'code' | 'table' | 'list'
    level: int         # 标题级别 1-6，非标题为 0
    content: str       # 原始 markdown 内容（含换行）
    context: str = ""  # 父级标题链，如 "应用框架 > ArkTS > 组件"

    def __repr__(self) -> str:
        preview = self.content[:60].replace("\n", "\\n")
        return f"Block({self.type}, lv={self.level}, ctx='{self.context}', '{preview}...')"


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _update_heading_stack(heading_stack: List[str], level: int, title: str) -> None:
    """维护标题层级栈：截断到对应层级并压入新标题"""
    del heading_stack[level - 1:]
    heading_stack.append(title)


def _context_str(heading_stack: List[str]) -> str:
    return " > ".join(heading_stack)


def parse_blocks(content: str) -> List[Block]:
    """
    将 Markdown 文本解析为语义块列表。

    解析规则：
    - 识别 # ~ ###### 标题行
    - 识别 ``` 代码块边界（保留语言标记）
    - 识别 | ... | 表格行（连续归为一个 table block）
    - 识别 - / * / 1. 列表行（连续归为一个 list block）
    - 其余归为 paragraph
    - 维护 heading_stack 追踪上下文
    """
    lines: list[str] = content.split("\n")
    blocks: List[Block] = []
    heading_stack: List[str] = []

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # ── 1) 代码块 ──
        m_code = RE_CODE_FENCE.match(line)
        if m_code:
            fence_line = line.rstrip()
            code_lines: list[str] = [fence_line]
            i += 1
            # 找到闭合的 ```
            while i < n:
                cur = lines[i].rstrip()
                code_lines.append(lines[i])
                i += 1
                # 闭合条件：单独的 ``` 行
                if cur.strip() == "```":
                    break
            block_content = "\n".join(code_lines)
            blocks.append(Block(
                type="code",
                level=0,
                content=block_content,
                context=_context_str(heading_stack),
            ))
            continue

        # ── 2) 标题行 ──
        m_heading = RE_HEADING.match(line)
        if m_heading:
            level = len(m_heading.group(1))
            title = m_heading.group(2).strip()
            _update_heading_stack(heading_stack, level, title)
            blocks.append(Block(
                type="heading",
                level=level,
                content=line,
                context=_context_str(heading_stack),
            ))
            i += 1
            continue

        # ── 3) 表格行 ──
        if RE_TABLE_ROW.match(line):
            table_lines: list[str] = []
            while i < n and RE_TABLE_ROW.match(lines[i]):
                table_lines.append(lines[i])
                i += 1
            # 对于超大表格，按行分组（每 TABLE_MAX_ROWS 行一个 block）
            TABLE_MAX_ROWS = 10
            if len(table_lines) > TABLE_MAX_ROWS + 2:  # +2 for header and separator
                # 保留表头（前两行：header + separator）
                header = table_lines[:2]
                data_rows = table_lines[2:]
                for start in range(0, len(data_rows), TABLE_MAX_ROWS):
                    chunk_rows = data_rows[start:start + TABLE_MAX_ROWS]
                    block_content = "\n".join(header + chunk_rows)
                    blocks.append(Block(
                        type="table",
                        level=0,
                        content=block_content,
                        context=_context_str(heading_stack),
                    ))
            else:
                blocks.append(Block(
                    type="table",
                    level=0,
                    content="\n".join(table_lines),
                    context=_context_str(heading_stack),
                ))
            continue

        # ── 4) 列表行 ──
        if RE_LIST_BULLET.match(line) or RE_LIST_ORDERED.match(line):
            list_lines: list[str] = []
            while i < n and (RE_LIST_BULLET.match(lines[i]) or RE_LIST_ORDERED.match(lines[i])):
                list_lines.append(lines[i])
                i += 1
            blocks.append(Block(
                type="list",
                level=0,
                content="\n".join(list_lines),
                context=_context_str(heading_stack),
            ))
            continue

        # ── 5) 空行 ──
        if _is_blank(line):
            i += 1
            continue

        # ── 6) 段落（连续非空、非特殊行归为一个段落）──
        para_lines: list[str] = []
        while i < n:
            l = lines[i]
            if _is_blank(l):
                break
            if RE_HEADING.match(l):
                break
            if RE_CODE_FENCE.match(l):
                break
            if RE_TABLE_ROW.match(l):
                break
            if RE_LIST_BULLET.match(l) or RE_LIST_ORDERED.match(l):
                break
            para_lines.append(l)
            i += 1
        if para_lines:
            blocks.append(Block(
                type="paragraph",
                level=0,
                content="\n".join(para_lines),
                context=_context_str(heading_stack),
            ))
            continue

        # fallback: skip
        i += 1

    return blocks
