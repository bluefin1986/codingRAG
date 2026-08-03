"""Resolve local Markdown image links into public asset URLs during ingest."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

_IMAGE_LINK_RE = re.compile(r"(!\[[^\]]*\]\()(?P<target><[^>]+>|[^\s)]+)(?P<suffix>(?:\s+[^)]*)?\))")
_EXTERNAL_SCHEMES = {"http", "https", "data"}
_ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class MarkdownAssetError(ValueError):
    """A local asset reference cannot safely be converted during ingest."""


@dataclass(frozen=True)
class MarkdownAssetRewrite:
    content: str
    stats: dict[str, int]


def asset_storage_relative_path(path: Path) -> str:
    """Return a content-addressed asset name without exposing the source path."""
    suffix = path.suffix.lower()
    if suffix not in _ALLOWED_IMAGE_SUFFIXES:
        raise MarkdownAssetError(f"unsupported local image type: {path.name}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{digest}{suffix}"


def rewrite_local_markdown_images(
    markdown_path: Path,
    staging_root: Path,
    upload: Callable[[Path], str],
) -> MarkdownAssetRewrite:
    """Replace local image URLs in one Markdown file, uploading each asset once."""
    resolved_markdown = markdown_path.resolve()
    resolved_root = staging_root.resolve()
    if resolved_root not in resolved_markdown.parents:
        raise MarkdownAssetError(f"Markdown path escapes staging root: {markdown_path}")

    content = resolved_markdown.read_text(encoding="utf-8", errors="replace")
    cached_urls: dict[Path, str] = {}
    stats = {"referenced": 0, "uploaded": 0, "reused": 0, "external": 0}

    def replace(match: re.Match[str]) -> str:
        raw_target = match.group("target")
        target = raw_target[1:-1] if raw_target.startswith("<") and raw_target.endswith(">") else raw_target
        if urlparse(target).scheme.lower() in _EXTERNAL_SCHEMES:
            stats["external"] += 1
            return match.group(0)

        stats["referenced"] += 1
        candidate = (resolved_markdown.parent / target).resolve()
        if resolved_root not in candidate.parents:
            raise MarkdownAssetError(f"image reference escapes staging root: {target}")
        if candidate.suffix.lower() not in _ALLOWED_IMAGE_SUFFIXES:
            raise MarkdownAssetError(f"unsupported local image type: {target}")
        if not candidate.is_file():
            raise MarkdownAssetError(f"local image is missing: {target}")

        public_url = cached_urls.get(candidate)
        if public_url is None:
            public_url = upload(candidate)
            if not public_url:
                raise MarkdownAssetError(f"asset upload returned no URL: {target}")
            cached_urls[candidate] = public_url
            stats["uploaded"] += 1
        else:
            stats["reused"] += 1
        return f"{match.group(1)}{public_url}{match.group('suffix')}"

    return MarkdownAssetRewrite(content=_IMAGE_LINK_RE.sub(replace, content), stats=stats)
