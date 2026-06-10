"""DocReader gRPC 客户端 — 调用 WeKnora DocReader 服务将文档转为 Markdown。

用法：
    from api.docreader.client import convert_to_markdown

    md_text = convert_to_markdown("report.pdf", "pdf", file_bytes)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import grpc

from api.docreader import docreader_pb2 as pb2
from api.docreader import docreader_pb2_grpc as pb2_grpc

logger = logging.getLogger("codingrag.docreader_client")

# ── 连接配置 ──
_DOCREADER_ADDR = os.getenv("CODING_RAG_DOCREADER_ADDR", "").strip()
_DOCREADER_TIMEOUT = int(os.getenv("CODING_RAG_DOCREADER_TIMEOUT", "120"))

# ── 支持的非文本扩展名（需要 DocReader 转换的） ──
DOCREADER_EXTENSIONS = {
    ".doc", ".docx", ".pdf",
    ".xls", ".xlsx",
    ".pptx", ".ppt",
}

# ── 模块级连接缓存 ──
_channel: Optional[grpc.Channel] = None
_stub: Optional[pb2_grpc.DocReaderStub] = None


def is_available() -> bool:
    """检查 DocReader 服务地址是否已配置。"""
    return bool(_DOCREADER_ADDR)


def _get_stub() -> pb2_grpc.DocReaderStub:
    """获取或创建 gRPC stub（懒初始化，进程级复用）。"""
    global _channel, _stub
    if _stub is not None:
        return _stub
    if not _DOCREADER_ADDR:
        raise RuntimeError(
            "CODING_RAG_DOCREADER_ADDR 未配置，无法调用 DocReader 服务"
        )
    # 最大消息 100MB（与 DocReader 服务端 MAX_FILE_SIZE_MB 对齐）
    options = [
        ("grpc.max_send_message_length", 100 * 1024 * 1024),
        ("grpc.max_receive_message_length", 100 * 1024 * 1024),
    ]
    _channel = grpc.insecure_channel(_DOCREADER_ADDR, options=options)
    _stub = pb2_grpc.DocReaderStub(_channel)
    logger.info("DocReader gRPC 连接已建立: %s", _DOCREADER_ADDR)
    return _stub


def convert_to_markdown(
    file_name: str,
    file_type: str,
    file_content: bytes,
    *,
    parser_engine: str = "",
    timeout: int | None = None,
) -> str:
    """调用 DocReader 将文件内容转为 Markdown 文本。

    Args:
        file_name: 原始文件名（含扩展名）
        file_type: 文件扩展名，如 "pdf", "docx", "xlsx"
        file_content: 文件二进制内容
        parser_engine: 解析引擎名（空字符串 = builtin）
        timeout: 超时秒数（默认用环境变量配置）

    Returns:
        转换后的 Markdown 文本

    Raises:
        RuntimeError: DocReader 未配置或调用失败
        ValueError: DocReader 返回错误
    """
    stub = _get_stub()
    req = pb2.ReadRequest(
        file_content=file_content,
        file_name=file_name,
        file_type=file_type.lstrip("."),
    )
    if parser_engine:
        req.config.parser_engine = parser_engine

    effective_timeout = timeout or _DOCREADER_TIMEOUT
    logger.info(
        "调用 DocReader: file=%s type=%s size=%d bytes",
        file_name, file_type, len(file_content),
    )

    try:
        resp = stub.Read(req, timeout=effective_timeout)
    except grpc.RpcError as e:
        code = e.code()
        details = e.details()
        logger.error("DocReader gRPC 调用失败: %s — %s", code, details)
        raise RuntimeError(f"DocReader 调用失败 ({code}): {details}") from e

    if resp.error:
        logger.error("DocReader 返回错误: %s", resp.error)
        raise ValueError(f"DocReader 解析失败: {resp.error}")

    md = resp.markdown_content
    logger.info(
        "DocReader 转换完成: file=%s markdown_len=%d images=%d",
        file_name, len(md), len(resp.image_refs),
    )
    return md


def check_health() -> dict:
    """检查 DocReader 服务连接状态。"""
    if not _DOCREADER_ADDR:
        return {"available": False, "reason": "CODING_RAG_DOCREADER_ADDR 未配置"}
    try:
        stub = _get_stub()
        # 用 ListEngines 探活
        resp = stub.ListEngines(pb2.ListEnginesRequest(), timeout=5)
        engines = [
            {"name": e.name, "file_types": list(e.file_types), "available": e.available}
            for e in resp.engines
        ]
        return {"available": True, "addr": _DOCREADER_ADDR, "engines": engines}
    except Exception as e:
        return {"available": False, "addr": _DOCREADER_ADDR, "reason": str(e)}
