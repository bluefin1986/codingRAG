"""Object storage abstraction for codingRAG original document registry.

The registry keeps PostgreSQL metadata, while original document bytes can stay local or
be uploaded to SeaweedFS through the filer HTTP API. The SeaweedFS backend only returns
``storage_status='active'`` after the object can be read back from SeaweedFS.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredObject:
    storage_backend: str
    storage_key: str
    storage_path: str
    storage_size: int
    storage_status: str = "active"
    storage_bucket: str | None = None
    storage_etag: str | None = None


class ObjectStorage:
    def put_existing_file(self, path: Path, *, relative_path: str) -> StoredObject:
        raise NotImplementedError

    def read_text(self, storage_path: str, *, storage_key: str | None = None, encoding: str = "utf-8") -> str:
        return Path(storage_path).read_text(encoding=encoding, errors="replace")


class LocalObjectStorage(ObjectStorage):
    backend = "local"

    def __init__(self, root_dir: str = "") -> None:
        default_root = Path(__file__).resolve().parents[1] / "data" / "originals"
        self.root_dir = Path(root_dir or os.getenv("CODING_RAG_LOCAL_STORAGE_DIR", str(default_root))).expanduser().resolve()

    def put_existing_file(self, path: Path, *, relative_path: str) -> StoredObject:
        source = path.expanduser().resolve()
        digest = _sha256_file(source)
        normalized_relative = "/".join(_safe_path_segment(part) for part in Path(relative_path).as_posix().split("/") if part)
        key = "/".join(part for part in (digest[:2], digest[:12], normalized_relative) if part)
        resolved = self.root_dir / key
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if source != resolved:
            shutil.copyfile(source, resolved)
        return StoredObject(
            storage_backend=self.backend,
            storage_key=key,
            storage_path=str(resolved),
            storage_size=resolved.stat().st_size,
            storage_etag=digest,
        )


class SeaweedFSObjectStorage(ObjectStorage):
    """SeaweedFS filer HTTP storage adapter.

    This uses SeaweedFS' filer HTTP API because it is small, deterministic, and works
    locally without a separate upload service. Compose still exposes the S3-compatible
    port so the deployment can later switch adapters without changing the registry.
    """

    backend = "seaweedfs"

    def __init__(
        self,
        filer_url: str = "",
        *,
        bucket: str = "codingrag-originals",
        public_base_url: str = "",
        key_prefix: str = "libraries",
        timeout_seconds: float = 120.0,
    ) -> None:
        self.filer_url = filer_url.rstrip("/")
        self.public_base_url = (public_base_url or filer_url).rstrip("/")
        self.bucket = _safe_path_segment(bucket.strip() or "codingrag-originals")
        self.key_prefix = _clean_key_prefix(key_prefix or "libraries")
        self.timeout_seconds = timeout_seconds
        self._local = LocalObjectStorage()

    def put_existing_file(self, path: Path, *, relative_path: str) -> StoredObject:
        source = path.expanduser().resolve()
        if not self.filer_url:
            local = self._local.put_existing_file(path, relative_path=relative_path)
            reason = "seaweedfs-filer-url-unconfigured"
            logger.warning("SeaweedFS upload skipped for %s: filer URL is not configured", relative_path)
            return self._fallback_object(local, relative_path, reason)

        digest = self._sha256(source)
        source_size = source.stat().st_size
        key = self._object_key(relative_path=relative_path, digest=digest)
        url = self._object_url(self.filer_url, key)
        storage_path = self._object_url(self.public_base_url, key)

        try:
            with source.open("rb") as f, httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, trust_env=False) as client:
                put_response = client.put(url, content=f)
                put_response.raise_for_status()
                # Verify by reading the object back. SeaweedFS filer supports HEAD in
                # common deployments, but GET is more reliable across versions.
                get_response = client.get(url)
                get_response.raise_for_status()
                if int(get_response.headers.get("content-length") or len(get_response.content)) != source_size:
                    raise RuntimeError("SeaweedFS read-back size mismatch")

            return StoredObject(
                storage_backend=self.backend,
                storage_bucket=self.bucket,
                storage_key=key,
                storage_path=storage_path,
                storage_size=source_size,
                storage_status="active",
                storage_etag=digest,
            )
        except Exception as exc:
            local = self._local.put_existing_file(path, relative_path=relative_path)
            reason = self._error_reason("seaweedfs-upload-failed", exc)
            logger.warning("SeaweedFS upload failed for %s: %s; using local fallback", relative_path, reason)
            return self._fallback_object(local, relative_path, reason)

    def read_text(self, storage_path: str, *, storage_key: str | None = None, encoding: str = "utf-8") -> str:
        local_path = Path(storage_path).expanduser() if storage_path and not urlparse(storage_path).scheme else None
        remote_url = self._remote_url(storage_path, storage_key)
        if remote_url:
            try:
                with httpx.Client(timeout=30.0, follow_redirects=True, trust_env=False) as client:
                    response = client.get(remote_url)
                    response.raise_for_status()
                    response.encoding = response.encoding or encoding
                    return response.text
            except Exception as exc:
                logger.warning("SeaweedFS read failed for %s: %s", remote_url, exc)

        if local_path and local_path.exists():
            return local_path.read_text(encoding=encoding, errors="replace")

        if remote_url:
            with httpx.Client(timeout=30.0, follow_redirects=True, trust_env=False) as client:
                response = client.get(remote_url)
                response.raise_for_status()
                response.encoding = response.encoding or encoding
                return response.text

        raise FileNotFoundError(storage_path or storage_key or "<empty storage reference>")

    def _object_key(self, *, relative_path: str, digest: str) -> str:
        normalized_relative = "/".join(_safe_path_segment(part) for part in Path(relative_path).as_posix().split("/") if part)
        return "/".join(part for part in (self.bucket, self.key_prefix, digest[:2], digest[:12], normalized_relative) if part)

    def _remote_url(self, storage_path: str, storage_key: str | None) -> str | None:
        if storage_path and urlparse(storage_path).scheme in {"http", "https"}:
            return storage_path
        if storage_key and self.filer_url:
            return self._object_url(self.filer_url, storage_key)
        return None

    @staticmethod
    def _object_url(base_url: str, key: str) -> str:
        quoted_key = quote(key.lstrip("/"), safe="/")
        return urljoin(f"{base_url.rstrip('/')}/", quoted_key)

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _error_reason(prefix: str, exc: Exception) -> str:
        text = str(exc).strip()
        detail = type(exc).__name__ if not text else f"{type(exc).__name__}:{text[:220]}"
        return f"{prefix}:{detail}"

    def _fallback_object(self, local: StoredObject, relative_path: str, reason: str) -> StoredObject:
        return StoredObject(
            storage_backend=self.backend,
            storage_key=relative_path,
            storage_path=local.storage_path,
            storage_size=local.storage_size,
            storage_status="missing",
            storage_bucket=self.bucket,
            storage_etag=reason[:300],
        )


def _clean_key_prefix(value: str) -> str:
    return "/".join(_safe_path_segment(part) for part in value.strip("/").split("/") if part)


def _safe_path_segment(value: str) -> str:
    value = value.strip().replace("\\", "/").strip("/")
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^0-9A-Za-z._=-]+", "-", value)
    return value.strip(".-_") or "_"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_storage(
    backend: str,
    *,
    seaweedfs_filer_url: str = "",
    seaweedfs_public_base_url: str = "",
    seaweedfs_bucket: str = "",
    seaweedfs_key_prefix: str = "",
) -> ObjectStorage:
    normalized = (backend or "local").strip().lower()
    if normalized in {"seaweedfs", "seaweed", "weed"}:
        return SeaweedFSObjectStorage(
            seaweedfs_filer_url or os.getenv("CODING_RAG_SEAWEEDFS_FILER_URL", ""),
            public_base_url=seaweedfs_public_base_url or os.getenv("CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL", ""),
            bucket=seaweedfs_bucket or os.getenv("CODING_RAG_SEAWEEDFS_BUCKET", "codingrag-originals"),
            key_prefix=seaweedfs_key_prefix or os.getenv("CODING_RAG_SEAWEEDFS_KEY_PREFIX", "libraries"),
        )
    return LocalObjectStorage()
